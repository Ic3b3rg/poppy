import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai_codex import ApprovalMode, AsyncCodex, Sandbox

from poppy.adapters.codex import MAX_CODEX_OUTPUT_CHARACTERS, CodexAdapter
from poppy.application.review_cycle import (
    ChangedFile,
    PullRequestRef,
    PullRequestSnapshot,
    ReviewUnavailable,
)
from poppy.domain.review import Severity


@pytest.mark.asyncio
async def test_review_uses_isolated_read_only_thread_and_maps_valid_json(
    tmp_path: Path,
) -> None:
    response = {
        "summary": "One concrete authorization problem was found.",
        "risk": "high",
        "findings": [
            {
                "severity": "high",
                "title": "Authorization is missing",
                "explanation": "The new handler accepts every caller.",
                "evidence": "The added route invokes the service without an ownership check.",
                "suggested_response": "Require the repository owner role before invoking it.",
                "file_path": "src/handler.py",
                "line": 18,
            }
        ],
    }
    turn = MagicMock()
    turn.run = AsyncMock(return_value=SimpleNamespace(final_response=json.dumps(response)))
    thread = MagicMock()
    thread.turn = AsyncMock(return_value=turn)
    client = MagicMock(spec=AsyncCodex)
    client.thread_start = AsyncMock(return_value=thread)
    adapter = CodexAdapter(
        client=cast(AsyncCodex, client),
        workspace=tmp_path,
        model="gpt-5.6-luna",
        timeout_seconds=5,
    )

    result = await adapter.review(pull_request_snapshot())

    assert result.summary == response["summary"]
    assert result.risk is Severity.HIGH
    assert len(result.findings) == 1
    assert result.findings[0].file_path == "src/handler.py"
    assert result.findings[0].line == 18

    start_kwargs = client.thread_start.await_args.kwargs
    assert start_kwargs["model"] == "gpt-5.6-luna"
    assert start_kwargs["approval_mode"] is ApprovalMode.deny_all
    assert start_kwargs["sandbox"] is Sandbox.read_only
    assert start_kwargs["ephemeral"] is True
    assert start_kwargs["cwd"] == str(tmp_path)
    assert start_kwargs["config"] == {"web_search": "disabled"}
    assert "untrusted" in start_kwargs["base_instructions"].lower()

    turn_args = thread.turn.await_args
    assert "src/handler.py" in turn_args.args[0]
    assert "@@ -1 +1 @@" in turn_args.args[0]
    assert turn_args.kwargs["model"] == "gpt-5.6-luna"
    assert turn_args.kwargs["approval_mode"] is ApprovalMode.deny_all
    assert turn_args.kwargs["sandbox"] is Sandbox.read_only
    assert turn_args.kwargs["output_schema"]["additionalProperties"] is False
    turn.run.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_incomplete_pull_request_never_reaches_codex(tmp_path: Path) -> None:
    client = MagicMock(spec=AsyncCodex)
    adapter = CodexAdapter(client=cast(AsyncCodex, client), workspace=tmp_path)
    snapshot = pull_request_snapshot(is_complete=False)

    with pytest.raises(ReviewUnavailable, match="incomplete"):
        await adapter.review(snapshot)

    client.thread_start.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "not json",
        json.dumps({"summary": "Missing fields"}),
        json.dumps(
            {
                "summary": "Unexpected field.",
                "risk": "low",
                "findings": [],
                "verdict": "approve",
            }
        ),
    ],
)
async def test_invalid_codex_output_becomes_review_unavailable(
    tmp_path: Path,
    response: str,
) -> None:
    turn = MagicMock()
    turn.run = AsyncMock(return_value=SimpleNamespace(final_response=response))
    thread = MagicMock()
    thread.turn = AsyncMock(return_value=turn)
    client = MagicMock(spec=AsyncCodex)
    client.thread_start = AsyncMock(return_value=thread)
    adapter = CodexAdapter(client=cast(AsyncCodex, client), workspace=tmp_path)

    with pytest.raises(ReviewUnavailable, match="invalid"):
        await adapter.review(pull_request_snapshot())


@pytest.mark.asyncio
async def test_codex_timeout_becomes_review_unavailable(tmp_path: Path) -> None:
    async def never_finishes(*_args: object, **_kwargs: object) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    turn = MagicMock()
    turn.run = AsyncMock(side_effect=never_finishes)
    turn.interrupt = AsyncMock()
    thread = MagicMock()
    thread.turn = AsyncMock(return_value=turn)
    client = MagicMock(spec=AsyncCodex)
    client.thread_start = AsyncMock(return_value=thread)
    adapter = CodexAdapter(
        client=cast(AsyncCodex, client),
        workspace=tmp_path,
        timeout_seconds=0.01,
    )

    with pytest.raises(ReviewUnavailable, match="timed out"):
        await adapter.review(pull_request_snapshot())

    turn.interrupt.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_oversized_input_never_reaches_codex(tmp_path: Path) -> None:
    client = MagicMock(spec=AsyncCodex)
    adapter = CodexAdapter(
        client=cast(AsyncCodex, client),
        workspace=tmp_path,
        max_input_characters=100,
    )

    with pytest.raises(ReviewUnavailable, match="configured limit"):
        await adapter.review(pull_request_snapshot())

    client.thread_start.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_codex_output_is_rejected(tmp_path: Path) -> None:
    response = json.dumps(
        {
            "summary": "Review summary.",
            "risk": "low",
            "findings": [
                {
                    "severity": "low",
                    "title": f"Finding {index}",
                    "explanation": "x" * 2_000,
                    "evidence": "y" * 2_000,
                    "suggested_response": "z" * 2_000,
                    "file_path": "src/file.py",
                    "line": 1,
                }
                for index in range(20)
            ],
        }
    )
    assert len(response) > MAX_CODEX_OUTPUT_CHARACTERS
    turn = MagicMock()
    turn.run = AsyncMock(return_value=SimpleNamespace(final_response=response))
    thread = MagicMock()
    thread.turn = AsyncMock(return_value=turn)
    client = MagicMock(spec=AsyncCodex)
    client.thread_start = AsyncMock(return_value=thread)
    adapter = CodexAdapter(client=cast(AsyncCodex, client), workspace=tmp_path)

    with pytest.raises(ReviewUnavailable, match="invalid"):
        await adapter.review(pull_request_snapshot())


def pull_request_snapshot(*, is_complete: bool = True) -> PullRequestSnapshot:
    return PullRequestSnapshot(
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
        is_complete=is_complete,
    )
