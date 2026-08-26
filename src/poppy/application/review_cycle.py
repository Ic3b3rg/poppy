import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from poppy.domain.review import (
    GateConclusion,
    ReviewEvent,
    ReviewResult,
    Severity,
    classify_change_size,
    decide_gate,
    render_review,
)


@dataclass(frozen=True, slots=True)
class PullRequestRef:
    installation_id: int
    repository_id: int
    owner: str
    repository: str
    number: int


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    delivery_id: str
    ref: PullRequestRef
    head_sha: str
    title: str
    body: str
    base_ref: str
    is_draft: bool

    @property
    def dedupe_key(self) -> str:
        normalized = json.dumps(
            {
                "installation": self.ref.installation_id,
                "repository": self.ref.repository_id,
                "pull_request": self.ref.number,
                "head_sha": self.head_sha,
                "title": self.title,
                "body": self.body,
                "base_ref": self.base_ref,
                "is_draft": self.is_draft,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(normalized.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    additions: int
    deletions: int
    patch: str | None


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    ref: PullRequestRef
    head_sha: str
    title: str
    body: str
    base_ref: str
    is_draft: bool
    is_open: bool
    additions: int
    deletions: int
    files: tuple[ChangedFile, ...]

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    @property
    def review_fingerprint(self) -> tuple[object, ...]:
        return (
            self.head_sha,
            self.title,
            self.body,
            self.base_ref,
            self.is_draft,
            self.is_open,
        )


class GitHubPort(Protocol):
    async def start_check(self, request: ReviewRequest) -> int: ...

    async def fetch_pull_request(self, ref: PullRequestRef) -> PullRequestSnapshot: ...

    async def replace_poppy_labels(
        self,
        ref: PullRequestRef,
        labels: tuple[str, str],
    ) -> None: ...

    async def submit_review(
        self,
        ref: PullRequestRef,
        *,
        head_sha: str,
        event: ReviewEvent,
        body: str,
    ) -> None: ...

    async def finish_check(
        self,
        ref: PullRequestRef,
        *,
        check_run_id: int,
        conclusion: GateConclusion,
        summary: str,
    ) -> None: ...


class CodexPort(Protocol):
    async def review(self, snapshot: PullRequestSnapshot) -> ReviewResult: ...


class ReviewCycle:
    def __init__(
        self,
        *,
        github: GitHubPort,
        codex: CodexPort,
        blocking_severity: Severity,
    ) -> None:
        self._github = github
        self._codex = codex
        self._blocking_severity = blocking_severity

    async def run(self, request: ReviewRequest) -> None:
        check_run_id = await self._github.start_check(request)
        try:
            snapshot = await self._github.fetch_pull_request(request.ref)
            if not _matches_request(snapshot, request):
                await self._finish_superseded(request.ref, check_run_id)
                return

            result = await self._codex.review(snapshot)
            current = await self._github.fetch_pull_request(request.ref)
            if current.review_fingerprint != snapshot.review_fingerprint:
                await self._finish_superseded(request.ref, check_run_id)
                return

            await self._publish(request.ref, check_run_id, snapshot, result)
        except Exception:
            await self._github.finish_check(
                request.ref,
                check_run_id=check_run_id,
                conclusion=GateConclusion.ACTION_REQUIRED,
                summary="Poppy could not complete this review. Retry the webhook delivery.",
            )

    async def _publish(
        self,
        ref: PullRequestRef,
        check_run_id: int,
        snapshot: PullRequestSnapshot,
        result: ReviewResult,
    ) -> None:
        decision = decide_gate(
            result.findings,
            blocking_severity=self._blocking_severity,
            is_draft=snapshot.is_draft,
            is_complete=True,
        )
        change_size = classify_change_size(snapshot.changed_lines)
        body = render_review(
            result=result,
            decision=decision,
            change_size=change_size,
            changed_lines=snapshot.changed_lines,
            head_sha=snapshot.head_sha,
        )
        await self._github.replace_poppy_labels(
            ref,
            (change_size.label, decision.conclusion.result_label),
        )
        await self._github.submit_review(
            ref,
            head_sha=snapshot.head_sha,
            event=decision.review_event,
            body=body,
        )
        await self._github.finish_check(
            ref,
            check_run_id=check_run_id,
            conclusion=decision.conclusion,
            summary=body,
        )

    async def _finish_superseded(self, ref: PullRequestRef, check_run_id: int) -> None:
        await self._github.finish_check(
            ref,
            check_run_id=check_run_id,
            conclusion=GateConclusion.ACTION_REQUIRED,
            summary="Review superseded by a newer pull-request state.",
        )


def _matches_request(snapshot: PullRequestSnapshot, request: ReviewRequest) -> bool:
    return (
        snapshot.is_open
        and snapshot.head_sha == request.head_sha
        and snapshot.title == request.title
        and snapshot.body == request.body
        and snapshot.base_ref == request.base_ref
        and snapshot.is_draft == request.is_draft
    )
