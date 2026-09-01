import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import quote

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from poppy.application.review_cycle import (
    ChangedFile,
    InlineReviewComment,
    PullRequestRef,
    PullRequestSnapshot,
    ReviewRequest,
)
from poppy.domain.review import RERUN_LABEL, GateConclusion, ReviewEvent

GITHUB_API_VERSION = "2026-03-10"
CHECK_NAME = "Poppy / PR Review"
MAX_FILE_PAGES = 30


class GitHubError(RuntimeError):
    """A sanitized GitHub boundary failure."""


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    token: str = Field(min_length=1)
    expires_at: datetime


class _CheckRunResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(gt=0)


class _Head(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sha: str = Field(min_length=1)


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ref: str = Field(min_length=1)


class _PullResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    state: str
    title: str
    body: str | None = None
    draft: bool
    head: _Head
    base: _Base
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    changed_files: int = Field(ge=0)


class _FileResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    filename: str = Field(min_length=1)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    patch: str | None = None


class _LabelResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = Field(min_length=1)


_FILE_LIST = TypeAdapter(list[_FileResponse])
_LABEL_LIST = TypeAdapter(list[_LabelResponse])
ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class _InstallationToken:
    value: str
    expires_at: datetime


class GitHubAdapter:
    def __init__(
        self,
        *,
        app_id: str,
        private_key: str,
        client: httpx.AsyncClient,
        api_url: str = "https://api.github.com",
        max_patch_characters: int = 120_000,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_patch_characters < 1:
            raise ValueError("max patch characters must be positive")
        self._app_id = app_id
        self._private_key = private_key
        self._client = client
        self._api_url = api_url.rstrip("/")
        self._max_patch_characters = max_patch_characters
        self._now = now or (lambda: datetime.now(UTC))
        self._tokens: dict[int, _InstallationToken] = {}
        self._token_lock = asyncio.Lock()

    async def start_check(self, request: ReviewRequest) -> int:
        response = await self._installation_request(
            request.ref.installation_id,
            "POST",
            f"/repos/{request.ref.owner}/{request.ref.repository}/check-runs",
            json={
                "name": CHECK_NAME,
                "head_sha": request.head_sha,
                "status": "in_progress",
                "external_id": (
                    f"{request.ref.repository_id}:{request.ref.number}:{request.head_sha}"
                ),
            },
            expected_statuses=(201,),
        )
        return self._validate(_CheckRunResponse, response).id

    async def fetch_pull_request(self, ref: PullRequestRef) -> PullRequestSnapshot:
        pull_response = await self._installation_request(
            ref.installation_id,
            "GET",
            f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}",
            expected_statuses=(200,),
        )
        pull = self._validate(_PullResponse, pull_response)
        raw_files = await self._fetch_files(ref, pull.changed_files)
        files, patches_complete = self._bound_patches(raw_files)
        return PullRequestSnapshot(
            ref=ref,
            head_sha=pull.head.sha,
            title=pull.title,
            body=pull.body or "",
            base_ref=pull.base.ref,
            is_draft=pull.draft,
            is_open=pull.state == "open",
            additions=pull.additions,
            deletions=pull.deletions,
            files=files,
            is_complete=len(raw_files) == pull.changed_files and patches_complete,
        )

    async def replace_poppy_labels(
        self,
        ref: PullRequestRef,
        labels: tuple[str, str],
    ) -> None:
        if any(not label.startswith("poppy:") for label in labels):
            raise ValueError("Poppy may only write labels prefixed with 'poppy:'")

        current_response = await self._installation_request(
            ref.installation_id,
            "GET",
            f"/repos/{ref.owner}/{ref.repository}/issues/{ref.number}/labels",
            expected_statuses=(200,),
        )
        current = self._validate_list(_LABEL_LIST, current_response)
        desired = set(labels)
        stale = {
            label.name
            for label in current
            if label.name.startswith("poppy:") and label.name not in desired
        }

        for label in (*labels, RERUN_LABEL):
            await self._ensure_label(ref, label)
        for label in stale:
            encoded = quote(label, safe="")
            await self._installation_request(
                ref.installation_id,
                "DELETE",
                f"/repos/{ref.owner}/{ref.repository}/issues/{ref.number}/labels/{encoded}",
                expected_statuses=(200, 204, 404),
            )
        await self._installation_request(
            ref.installation_id,
            "POST",
            f"/repos/{ref.owner}/{ref.repository}/issues/{ref.number}/labels",
            json={"labels": list(labels)},
            expected_statuses=(200,),
        )

    async def submit_review(
        self,
        ref: PullRequestRef,
        *,
        head_sha: str,
        event: ReviewEvent,
        body: str,
        comments: tuple[InlineReviewComment, ...] = (),
    ) -> None:
        payload: dict[str, Any] = {
            "commit_id": head_sha,
            "event": event.value,
            "body": body,
        }
        if comments:
            payload["comments"] = [
                {
                    "path": comment.path,
                    "line": comment.line,
                    "side": "RIGHT",
                    "body": comment.body,
                }
                for comment in comments
            ]

        response = await self._installation_request(
            ref.installation_id,
            "POST",
            f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}/reviews",
            json=payload,
            expected_statuses=(200, 422) if comments else (200,),
        )
        if response.status_code == 422:
            payload.pop("comments")
            await self._installation_request(
                ref.installation_id,
                "POST",
                f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}/reviews",
                json=payload,
                expected_statuses=(200,),
            )

    async def finish_check(
        self,
        ref: PullRequestRef,
        *,
        check_run_id: int,
        conclusion: GateConclusion,
        summary: str,
    ) -> None:
        title = {
            GateConclusion.SUCCESS: "Poppy review passed",
            GateConclusion.FAILURE: "Poppy review blocked",
            GateConclusion.ACTION_REQUIRED: "Poppy review needs attention",
        }[conclusion]
        await self._installation_request(
            ref.installation_id,
            "PATCH",
            f"/repos/{ref.owner}/{ref.repository}/check-runs/{check_run_id}",
            json={
                "status": "completed",
                "conclusion": conclusion.value,
                "output": {"title": title, "summary": summary[:65_535]},
            },
            expected_statuses=(200,),
        )

    async def _fetch_files(
        self,
        ref: PullRequestRef,
        expected_count: int,
    ) -> list[_FileResponse]:
        files: list[_FileResponse] = []
        page = 1
        while len(files) < expected_count and page <= MAX_FILE_PAGES:
            response = await self._installation_request(
                ref.installation_id,
                "GET",
                f"/repos/{ref.owner}/{ref.repository}/pulls/{ref.number}/files",
                params={"per_page": 100, "page": page},
                expected_statuses=(200,),
            )
            batch = self._validate_list(_FILE_LIST, response)
            if not batch:
                break
            files.extend(batch)
            page += 1
        return files

    def _bound_patches(
        self,
        raw_files: list[_FileResponse],
    ) -> tuple[tuple[ChangedFile, ...], bool]:
        remaining = self._max_patch_characters
        complete = True
        files: list[ChangedFile] = []
        for raw_file in raw_files:
            patch = raw_file.patch
            if patch is None:
                complete = False
            elif len(patch) > remaining:
                patch = patch[:remaining] or None
                remaining = 0
                complete = False
            else:
                remaining -= len(patch)
            files.append(
                ChangedFile(
                    path=raw_file.filename,
                    additions=raw_file.additions,
                    deletions=raw_file.deletions,
                    patch=patch,
                )
            )
        return tuple(files), complete

    async def _ensure_label(self, ref: PullRequestRef, label: str) -> None:
        encoded = quote(label, safe="")
        response = await self._installation_request(
            ref.installation_id,
            "GET",
            f"/repos/{ref.owner}/{ref.repository}/labels/{encoded}",
            expected_statuses=(200, 404),
        )
        if response.status_code == 200:
            return
        await self._installation_request(
            ref.installation_id,
            "POST",
            f"/repos/{ref.owner}/{ref.repository}/labels",
            json={
                "name": label,
                "color": _label_color(label),
                "description": _label_description(label),
            },
            expected_statuses=(201,),
        )

    async def _installation_request(
        self,
        installation_id: int,
        method: str,
        path: str,
        *,
        expected_statuses: tuple[int, ...],
        **kwargs: Any,
    ) -> httpx.Response:
        token = await self._installation_token(installation_id)
        headers = {
            "accept": "application/vnd.github+json",
            "authorization": f"Bearer {token}",
            "x-github-api-version": GITHUB_API_VERSION,
            "user-agent": "poppy-reviewer/0.1",
        }
        response = await self._client.request(
            method,
            f"{self._api_url}{path}",
            headers=headers,
            **kwargs,
        )
        self._require_status(response, expected_statuses)
        return response

    async def _installation_token(self, installation_id: int) -> str:
        cached = self._tokens.get(installation_id)
        if cached and cached.expires_at - self._now() > timedelta(minutes=1):
            return cached.value

        async with self._token_lock:
            cached = self._tokens.get(installation_id)
            if cached and cached.expires_at - self._now() > timedelta(minutes=1):
                return cached.value

            now = self._now()
            app_jwt = jwt.encode(
                {
                    "iat": int((now - timedelta(seconds=60)).timestamp()),
                    "exp": int((now + timedelta(minutes=9)).timestamp()),
                    "iss": self._app_id,
                },
                self._private_key,
                algorithm="RS256",
            )
            response = await self._client.post(
                f"{self._api_url}/app/installations/{installation_id}/access_tokens",
                headers={
                    "accept": "application/vnd.github+json",
                    "authorization": f"Bearer {app_jwt}",
                    "x-github-api-version": GITHUB_API_VERSION,
                    "user-agent": "poppy-reviewer/0.1",
                },
            )
            self._require_status(response, (201,))
            parsed = self._validate(_TokenResponse, response)
            self._tokens[installation_id] = _InstallationToken(
                value=parsed.token,
                expires_at=parsed.expires_at,
            )
            return parsed.token

    @staticmethod
    def _require_status(response: httpx.Response, expected: tuple[int, ...]) -> None:
        if response.status_code in expected:
            return
        request_id = response.headers.get("x-github-request-id", "unknown")
        raise GitHubError(f"GitHub API returned {response.status_code} (request_id={request_id})")

    @staticmethod
    def _validate(model: type[ModelT], response: httpx.Response) -> ModelT:
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise GitHubError("GitHub API returned an invalid response") from error

    @staticmethod
    def _validate_list(
        adapter: TypeAdapter[list[ModelT]],
        response: httpx.Response,
    ) -> list[ModelT]:
        try:
            return adapter.validate_python(response.json())
        except (ValueError, ValidationError) as error:
            raise GitHubError("GitHub API returned an invalid response") from error


def _label_color(label: str) -> str:
    if label == "poppy:passed":
        return "0E8A16"
    if label == "poppy:blocked":
        return "D73A4A"
    if label == "poppy:action-required":
        return "FBCA04"
    if label == RERUN_LABEL:
        return "1D76DB"
    return "5319E7"


def _label_description(label: str) -> str:
    if label == RERUN_LABEL:
        return "Apply this label to rerun Poppy on the latest commit."
    return "Managed automatically by Poppy."
