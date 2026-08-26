import asyncio
import hashlib
import hmac
import json
from dataclasses import replace

import httpx
import pytest

from poppy.application.review_cycle import (
    ChangedFile,
    PullRequestRef,
    PullRequestSnapshot,
    ReviewCycle,
    ReviewRequest,
)
from poppy.application.scheduler import InMemoryScheduler
from poppy.domain.review import Finding, GateConclusion, ReviewEvent, ReviewResult, Severity
from poppy.http import create_app


class FakeScheduler:
    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.requests: list[ReviewRequest] = []

    async def schedule(self, request: ReviewRequest) -> bool:
        self.requests.append(request)
        return self.accepts


class FakeGitHub:
    def __init__(self, snapshots: list[PullRequestSnapshot]) -> None:
        self.snapshots = snapshots
        self.events: list[tuple[object, ...]] = []

    async def start_check(self, request: ReviewRequest) -> int:
        self.events.append(("start_check", request.head_sha))
        return 77

    async def fetch_pull_request(self, ref: PullRequestRef) -> PullRequestSnapshot:
        self.events.append(("fetch_pull_request", ref.number))
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    async def replace_poppy_labels(
        self,
        ref: PullRequestRef,
        labels: tuple[str, str],
    ) -> None:
        self.events.append(("replace_poppy_labels", labels))

    async def submit_review(
        self,
        ref: PullRequestRef,
        *,
        head_sha: str,
        event: ReviewEvent,
        body: str,
    ) -> None:
        self.events.append(("submit_review", head_sha, event, body))

    async def finish_check(
        self,
        ref: PullRequestRef,
        *,
        check_run_id: int,
        conclusion: GateConclusion,
        summary: str,
    ) -> None:
        self.events.append(("finish_check", check_run_id, conclusion, summary))


class FakeCodex:
    def __init__(self, result: ReviewResult) -> None:
        self.result = result
        self.snapshots: list[PullRequestSnapshot] = []

    async def review(self, snapshot: PullRequestSnapshot) -> ReviewResult:
        self.snapshots.append(snapshot)
        return self.result


@pytest.mark.asyncio
async def test_valid_webhook_schedules_normalized_review() -> None:
    secret = b"test-webhook-secret"
    scheduler = FakeScheduler()
    app = create_app(webhook_secret=secret, scheduler=scheduler)
    payload = webhook_payload()
    body = json.dumps(payload, separators=(",", ":")).encode()

    response = await post_webhook(app, body, secret=secret)

    assert response.status_code == 202
    assert response.json() == {"status": "scheduled"}
    assert scheduler.requests == [review_request()]


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected_before_scheduling() -> None:
    scheduler = FakeScheduler()
    app = create_app(webhook_secret=b"correct-secret", scheduler=scheduler)
    body = json.dumps(webhook_payload()).encode()

    response = await post_webhook(app, body, secret=b"wrong-secret")

    assert response.status_code == 401
    assert scheduler.requests == []


@pytest.mark.asyncio
async def test_irrelevant_event_is_ignored() -> None:
    secret = b"test-webhook-secret"
    scheduler = FakeScheduler()
    app = create_app(webhook_secret=secret, scheduler=scheduler)
    body = json.dumps(webhook_payload(action="closed")).encode()

    response = await post_webhook(app, body, secret=secret)

    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}
    assert scheduler.requests == []


@pytest.mark.asyncio
async def test_duplicate_review_is_reported_without_a_second_task() -> None:
    secret = b"test-webhook-secret"
    scheduler = FakeScheduler(accepts=False)
    app = create_app(webhook_secret=secret, scheduler=scheduler)
    body = json.dumps(webhook_payload()).encode()

    response = await post_webhook(app, body, secret=secret)

    assert response.status_code == 202
    assert response.json() == {"status": "duplicate"}


@pytest.mark.asyncio
async def test_review_cycle_publishes_labels_review_and_check() -> None:
    snapshot = pull_request_snapshot()
    github = FakeGitHub([snapshot, snapshot])
    finding = Finding(
        severity=Severity.HIGH,
        title="Missing authorization",
        explanation="The new endpoint does not check the caller.",
        evidence="The handler calls the service directly.",
        suggested_response="Require the repository owner role.",
        file_path="src/handler.py",
        line=18,
    )
    codex = FakeCodex(
        ReviewResult(
            summary="One blocking issue was found.", risk=Severity.HIGH, findings=(finding,)
        )
    )
    cycle = ReviewCycle(github=github, codex=codex, blocking_severity=Severity.HIGH)

    await cycle.run(review_request())

    assert codex.snapshots == [snapshot]
    assert github.events[0] == ("start_check", "head-sha")
    assert ("replace_poppy_labels", ("poppy:size/s", "poppy:blocked")) in github.events
    review_event = next(event for event in github.events if event[0] == "submit_review")
    assert review_event[1:3] == ("head-sha", ReviewEvent.REQUEST_CHANGES)
    assert "Missing authorization" in str(review_event[3])
    assert github.events[-1][0:3] == ("finish_check", 77, GateConclusion.FAILURE)


@pytest.mark.asyncio
async def test_stale_review_never_publishes_labels_or_formal_review() -> None:
    initial = pull_request_snapshot()
    newer = replace(initial, head_sha="new-head-sha")
    github = FakeGitHub([initial, newer])
    codex = FakeCodex(ReviewResult(summary="Looks good.", risk=Severity.LOW, findings=()))
    cycle = ReviewCycle(github=github, codex=codex, blocking_severity=Severity.HIGH)

    await cycle.run(review_request())

    event_names = [event[0] for event in github.events]
    assert "replace_poppy_labels" not in event_names
    assert "submit_review" not in event_names
    assert github.events[-1][0:3] == ("finish_check", 77, GateConclusion.ACTION_REQUIRED)


@pytest.mark.asyncio
async def test_scheduler_deduplicates_and_limits_parallel_reviews() -> None:
    release = asyncio.Event()
    started: asyncio.Queue[str] = asyncio.Queue()
    active = 0
    max_active = 0

    async def handler(request: ReviewRequest) -> None:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await started.put(request.head_sha)
        await release.wait()
        active -= 1

    scheduler = InMemoryScheduler(handler=handler, concurrency=2, history_size=10)
    first = review_request()

    assert await scheduler.schedule(first) is True
    assert await scheduler.schedule(first) is False
    assert await scheduler.schedule(replace(first, head_sha="head-2")) is True
    assert await scheduler.schedule(replace(first, head_sha="head-3")) is True
    await started.get()
    await started.get()

    assert started.empty()
    assert max_active == 2

    release.set()
    await scheduler.join()


async def post_webhook(
    app: object,
    body: bytes,
    *,
    secret: bytes,
) -> httpx.Response:
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/webhooks/github",
            content=body,
            headers={
                "content-type": "application/json",
                "x-github-delivery": "delivery-1",
                "x-github-event": "pull_request",
                "x-hub-signature-256": signature,
            },
        )


def review_request() -> ReviewRequest:
    return ReviewRequest(
        delivery_id="delivery-1",
        ref=PullRequestRef(
            installation_id=123,
            repository_id=456,
            owner="Ic3b3rg",
            repository="example",
            number=7,
        ),
        head_sha="head-sha",
        title="Add review gate",
        body="Implements the first vertical slice.",
        base_ref="main",
        is_draft=False,
    )


def pull_request_snapshot() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        ref=review_request().ref,
        head_sha="head-sha",
        title="Add review gate",
        body="Implements the first vertical slice.",
        base_ref="main",
        is_draft=False,
        is_open=True,
        additions=120,
        deletions=12,
        files=(
            ChangedFile(
                path="src/handler.py",
                additions=120,
                deletions=12,
                patch="@@ -1 +1 @@\n-old\n+new",
            ),
        ),
    )


def webhook_payload(*, action: str = "opened") -> dict[str, object]:
    return {
        "action": action,
        "number": 7,
        "installation": {"id": 123},
        "repository": {
            "id": 456,
            "name": "example",
            "full_name": "Ic3b3rg/example",
            "owner": {"login": "Ic3b3rg"},
        },
        "pull_request": {
            "title": "Add review gate",
            "body": "Implements the first vertical slice.",
            "draft": False,
            "head": {"sha": "head-sha"},
            "base": {"ref": "main"},
        },
    }
