import json
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from poppy.adapters.github import GitHubAdapter
from poppy.application.review_cycle import (
    GitHubPort,
    InlineReviewComment,
    PullRequestRef,
    ReviewRequest,
)
from poppy.domain.review import GateConclusion, ReviewEvent

RequestHandler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


def test_public_github_boundary_has_no_merge_or_repository_write_operation() -> None:
    forbidden = {
        "merge",
        "merge_pull_request",
        "close_pull_request",
        "update_branch",
        "push",
        "write_contents",
    }

    assert forbidden.isdisjoint(dir(GitHubPort))
    assert forbidden.isdisjoint(dir(GitHubAdapter))


@pytest.mark.asyncio
async def test_start_check_reuses_valid_installation_token() -> None:
    requests: list[httpx.Request] = []
    check_ids = iter((71, 72))

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/installations/123/access_tokens":
            return json_response(
                201,
                {
                    "token": "installation-token",
                    "expires_at": "2026-08-26T18:00:00Z",
                },
            )
        if request.url.path == "/repos/Ic3b3rg/example/check-runs":
            assert request.headers["authorization"] == "Bearer installation-token"
            payload = json.loads(request.content)
            assert payload == {
                "name": "Poppy / PR Review",
                "head_sha": "head-sha",
                "status": "in_progress",
                "external_id": "456:7:head-sha",
            }
            return json_response(201, {"id": next(check_ids)})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with github_adapter(handler) as adapter:
        assert await adapter.start_check(review_request()) == 71
        assert await adapter.start_check(review_request()) == 72

    token_requests = [
        request for request in requests if request.url.path.endswith("/access_tokens")
    ]
    assert len(token_requests) == 1


@pytest.mark.asyncio
async def test_fetch_pull_request_paginates_and_bounds_patches() -> None:
    requests: list[httpx.Request] = []
    first_page = [
        {
            "filename": f"src/file_{index}.py",
            "additions": 1,
            "deletions": 0,
            "patch": "+x",
        }
        for index in range(100)
    ]
    second_page = [
        {
            "filename": "src/last.py",
            "additions": 1,
            "deletions": 0,
            "patch": "+last",
        }
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/access_tokens"):
            return installation_token_response()
        if request.url.path == "/repos/Ic3b3rg/example/pulls/7":
            return json_response(
                200,
                {
                    "state": "open",
                    "title": "Add review gate",
                    "body": "Implements the first vertical slice.",
                    "draft": False,
                    "head": {"sha": "head-sha"},
                    "base": {"ref": "main"},
                    "additions": 101,
                    "deletions": 0,
                    "changed_files": 101,
                },
            )
        if request.url.path == "/repos/Ic3b3rg/example/pulls/7/files":
            page = request.url.params["page"]
            return json_response(200, first_page if page == "1" else second_page)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with github_adapter(handler, max_patch_characters=20) as adapter:
        snapshot = await adapter.fetch_pull_request(pull_request_ref())

    assert len(snapshot.files) == 101
    assert sum(len(file.patch or "") for file in snapshot.files) <= 20
    assert snapshot.files[-1].patch is None
    assert snapshot.is_complete is False
    file_requests = [request for request in requests if request.url.path.endswith("/files")]
    assert [request.url.params["page"] for request in file_requests] == ["1", "2"]


@pytest.mark.asyncio
async def test_replaces_only_poppy_labels_and_creates_missing_label() -> None:
    requests: list[httpx.Request] = []
    created_labels: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/access_tokens"):
            return installation_token_response()
        if path == "/repos/Ic3b3rg/example/issues/7/labels" and request.method == "GET":
            return json_response(
                200,
                [
                    {"name": "bug"},
                    {"name": "poppy:size/xs"},
                    {"name": "poppy:passed"},
                ],
            )
        if path == "/repos/Ic3b3rg/example/labels/poppy:size/s":
            return json_response(404, {"message": "Not Found"})
        if path == "/repos/Ic3b3rg/example/labels/poppy:blocked":
            return json_response(200, {"name": "poppy:blocked"})
        if path == "/repos/Ic3b3rg/example/labels/poppy:rerun":
            return json_response(404, {"message": "Not Found"})
        if path == "/repos/Ic3b3rg/example/labels" and request.method == "POST":
            payload = json.loads(request.content)
            created_labels.append(payload)
            return json_response(201, payload)
        if path in {
            "/repos/Ic3b3rg/example/issues/7/labels/poppy:size/xs",
            "/repos/Ic3b3rg/example/issues/7/labels/poppy:passed",
        }:
            return httpx.Response(204)
        if path == "/repos/Ic3b3rg/example/issues/7/labels" and request.method == "POST":
            assert json.loads(request.content) == {"labels": ["poppy:size/s", "poppy:blocked"]}
            return json_response(
                200,
                [{"name": "bug"}, {"name": "poppy:size/s"}, {"name": "poppy:blocked"}],
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with github_adapter(handler) as adapter:
        await adapter.replace_poppy_labels(
            pull_request_ref(),
            ("poppy:size/s", "poppy:blocked"),
        )

    deleted_paths = {request.url.path for request in requests if request.method == "DELETE"}
    assert deleted_paths == {
        "/repos/Ic3b3rg/example/issues/7/labels/poppy:size/xs",
        "/repos/Ic3b3rg/example/issues/7/labels/poppy:passed",
    }
    assert all(not path.endswith("/bug") for path in deleted_paths)
    assert created_labels == [
        {
            "name": "poppy:size/s",
            "color": "5319E7",
            "description": "Managed automatically by Poppy.",
        },
        {
            "name": "poppy:rerun",
            "color": "1D76DB",
            "description": "Apply this label to rerun Poppy on the latest commit.",
        },
    ]


@pytest.mark.asyncio
async def test_completed_review_removes_applied_rerun_label() -> None:
    deleted_labels: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/access_tokens"):
            return installation_token_response()
        if path == "/repos/Ic3b3rg/example/issues/7/labels" and request.method == "GET":
            return json_response(
                200,
                [
                    {"name": "poppy:size/s"},
                    {"name": "poppy:blocked"},
                    {"name": "poppy:rerun"},
                ],
            )
        if path.startswith("/repos/Ic3b3rg/example/labels/"):
            return json_response(200, {"name": path.rsplit("/", 1)[-1]})
        if request.method == "DELETE":
            deleted_labels.append(path)
            return httpx.Response(204)
        if path == "/repos/Ic3b3rg/example/issues/7/labels" and request.method == "POST":
            assert json.loads(request.content) == {
                "labels": ["poppy:size/s", "poppy:blocked"]
            }
            return json_response(200, [])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with github_adapter(handler) as adapter:
        await adapter.replace_poppy_labels(
            pull_request_ref(),
            ("poppy:size/s", "poppy:blocked"),
        )

    assert deleted_labels == [
        "/repos/Ic3b3rg/example/issues/7/labels/poppy:rerun"
    ]


@pytest.mark.asyncio
async def test_submits_exact_commit_review_and_completes_check() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/access_tokens"):
            return installation_token_response()
        if request.url.path == "/repos/Ic3b3rg/example/pulls/7/reviews":
            assert json.loads(request.content) == {
                "commit_id": "head-sha",
                "event": "APPROVE",
                "body": "Looks good.",
                "comments": [
                    {
                        "path": "src/example.py",
                        "line": 12,
                        "side": "RIGHT",
                        "body": "### 🟡 MEDIUM · Handle the edge case",
                    }
                ],
            }
            return json_response(200, {"id": 88})
        if request.url.path == "/repos/Ic3b3rg/example/check-runs/77":
            assert json.loads(request.content) == {
                "status": "completed",
                "conclusion": "success",
                "output": {"title": "Poppy review passed", "summary": "Looks good."},
            }
            return json_response(200, {"id": 77})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with github_adapter(handler) as adapter:
        await adapter.submit_review(
            pull_request_ref(),
            head_sha="head-sha",
            event=ReviewEvent.APPROVE,
            body="Looks good.",
            comments=(
                InlineReviewComment(
                    path="src/example.py",
                    line=12,
                    body="### 🟡 MEDIUM · Handle the edge case",
                ),
            ),
        )
        await adapter.finish_check(
            pull_request_ref(),
            check_run_id=77,
            conclusion=GateConclusion.SUCCESS,
            summary="Looks good.",
        )

    assert [request.method for request in requests] == ["POST", "POST", "PATCH"]


@pytest.mark.asyncio
async def test_retries_review_without_inline_comments_when_github_rejects_them() -> None:
    review_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return installation_token_response()
        if request.url.path == "/repos/Ic3b3rg/example/pulls/7/reviews":
            review_payloads.append(json.loads(request.content))
            if len(review_payloads) == 1:
                return json_response(422, {"message": "Validation Failed"})
            return json_response(200, {"id": 89})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with github_adapter(handler) as adapter:
        await adapter.submit_review(
            pull_request_ref(),
            head_sha="head-sha",
            event=ReviewEvent.REQUEST_CHANGES,
            body="The full finding remains here.",
            comments=(
                InlineReviewComment(
                    path="src/example.py",
                    line=12,
                    body="Inline finding.",
                ),
            ),
        )

    assert len(review_payloads) == 2
    assert review_payloads[0]["comments"] == [
        {
            "path": "src/example.py",
            "line": 12,
            "side": "RIGHT",
            "body": "Inline finding.",
        }
    ]
    assert "comments" not in review_payloads[1]


class AdapterContext:
    def __init__(
        self,
        handler: RequestHandler,
        *,
        max_patch_characters: int,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(handler),
        )
        self._adapter = GitHubAdapter(
            app_id="999",
            private_key=private_key(),
            client=self._client,
            api_url="https://api.github.test",
            max_patch_characters=max_patch_characters,
            now=lambda: datetime(2026, 8, 26, 16, 0, tzinfo=UTC),
        )

    async def __aenter__(self) -> GitHubAdapter:
        return self._adapter

    async def __aexit__(self, *_args: object) -> None:
        await self._client.aclose()


def github_adapter(
    handler: RequestHandler,
    *,
    max_patch_characters: int = 120_000,
) -> AdapterContext:
    return AdapterContext(handler, max_patch_characters=max_patch_characters)


def private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def review_request() -> ReviewRequest:
    return ReviewRequest(
        delivery_id="delivery-1",
        ref=pull_request_ref(),
        head_sha="head-sha",
        title="Add review gate",
        body="Implements the first vertical slice.",
        base_ref="main",
        is_draft=False,
    )


def pull_request_ref() -> PullRequestRef:
    return PullRequestRef(
        installation_id=123,
        repository_id=456,
        owner="Ic3b3rg",
        repository="example",
        number=7,
    )


def installation_token_response() -> httpx.Response:
    return json_response(
        201,
        {"token": "installation-token", "expires_at": "2026-08-26T18:00:00Z"},
    )


def json_response(status_code: int, payload: object) -> httpx.Response:
    return httpx.Response(status_code, json=payload)
