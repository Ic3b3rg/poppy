import asyncio
import hashlib
import hmac
import json
import logging
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.responses import StreamingResponse

import poppy.private_state as private_state_module
import poppy.service as service_module
from poppy.adapters.codex_onboarding import CodexDeviceAuthorization
from poppy.adapters.github_onboarding import GitHubAppCredentials, GitHubInstallation
from poppy.application.review_cycle import (
    ChangedFile,
    PullRequestRef,
    PullRequestSnapshot,
    ReviewRequest,
)
from poppy.domain.review import GateConclusion, ReviewEvent, ReviewResult, Severity
from poppy.github_setup import GitHubSetupStore
from poppy.runtime import ReadinessStore, ReviewerAdapters, ReviewerConnection
from poppy.service import RuntimeConfigurationError, create_service
from tests.onboarding_support import (
    establish_setup_session,
    github_installation_payload,
    github_private_key,
    manifest_state_from,
    setup_client,
    setup_settings,
    setup_url_from,
    wait_for_mode,
    wait_for_setup_text,
)


@pytest.mark.asyncio
async def test_setup_promotes_in_process_and_restart_reviews_a_signed_pull_request(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    provider = github_provider(private_key=private_key, webhook_secret=webhook_secret)
    codex_onboarding = FakeCodexOnboarding()
    first_reviewer = FakeReviewerAdapterFactory()
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.WARNING, logger="poppy.service")

    first_app = create_service(
        configured,
        github_onboarding_client=provider,
        codex_onboarding=codex_onboarding,
        reviewer_adapter_factory=first_reviewer,
    )
    setup_url = setup_url_from(caplog)

    async with first_app.router.lifespan_context(first_app), setup_client(first_app) as client:
        unavailable = await client.post(
            "/webhooks/github",
            content=b"{}",
            headers={"content-type": "application/json"},
        )
        csrf_token = await establish_setup_session(client, setup_url)

        installation_callback = await connect_github(client, csrf_token=csrf_token)

        github_only = await client.get("/healthz")
        chatgpt_start = await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        codex_onboarding.complete()
        promoted = await wait_for_mode(client, "reviewer")
        closed_setup = await client.get("/setup")
        closed_manifest_callback = await client.get(
            "/setup/github/manifest/callback",
            params={"code": "another-code", "state": "a" * 43},
        )
        closed_installation_callback = await client.get(
            "/setup/github/installation",
            params={"installation_id": "456", "state": "b" * 43},
        )
        closed_mutation = await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        replayed_bootstrap = await client.get(setup_url)

    assert unavailable.status_code == 503
    assert installation_callback.status_code == 303
    assert github_only.json() == {"status": "ok", "mode": "setup"}
    assert chatgpt_start.status_code == 303
    assert promoted.json() == {"status": "ok", "mode": "reviewer"}
    assert first_reviewer.connections == [
        ReviewerConnection(
            app_id=123,
            app_slug="poppy-example",
            installation_id=456,
            private_key=private_key,
            webhook_secret=webhook_secret,
        )
    ]
    assert [
        closed_setup.status_code,
        closed_manifest_callback.status_code,
        closed_installation_callback.status_code,
        closed_mutation.status_code,
        replayed_bootstrap.status_code,
    ] == [404, 404, 404, 404, 404]

    caplog.clear()
    restarted_reviewer = FakeReviewerAdapterFactory()
    restarted_app = create_service(
        configured,
        github_onboarding_client=provider,
        codex_onboarding=FakeCodexOnboarding(),
        reviewer_adapter_factory=restarted_reviewer,
    )

    async with (
        restarted_app.router.lifespan_context(restarted_app),
        setup_client(restarted_app) as client,
    ):
        health = await client.get("/healthz")
        setup_after_restart = await client.get("/setup")
        webhook = await post_signed_webhook(client, secret=webhook_secret)
        await restarted_reviewer.github.review_finished.wait()

    assert not any(
        getattr(record, "event", None) == "setup_bootstrap_created" for record in caplog.records
    )
    assert health.json() == {"status": "ok", "mode": "reviewer"}
    assert setup_after_restart.status_code == 404
    assert webhook.status_code == 202
    assert webhook.json() == {"status": "scheduled"}
    assert restarted_reviewer.codex.snapshots == [pull_request_snapshot()]
    assert (
        "replace_poppy_labels",
        ("poppy:size/s", "poppy:passed"),
    ) in restarted_reviewer.github.events
    assert any(
        event[:3] == ("submit_review", "head-sha", ReviewEvent.APPROVE)
        for event in restarted_reviewer.github.events
    )
    assert restarted_reviewer.github.events[-1][:3] == (
        "finish_check",
        77,
        GateConclusion.SUCCESS,
    )
    readiness_file = configured.data_dir / "readiness.json"
    assert stat.S_IMODE(readiness_file.stat().st_mode) == 0o600
    assert not (configured.data_dir / "setup-state.json").exists()
    persisted_readiness = readiness_file.read_text(encoding="utf-8")
    assert private_key not in persisted_readiness
    assert webhook_secret not in persisted_readiness
    await provider.aclose()


@pytest.mark.asyncio
async def test_readiness_is_independent_of_integration_order(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    provider = github_provider(private_key=private_key, webhook_secret=webhook_secret)
    codex_onboarding = FakeCodexOnboarding()
    reviewer = FakeReviewerAdapterFactory()
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        setup_settings(tmp_path),
        github_onboarding_client=provider,
        codex_onboarding=codex_onboarding,
        reviewer_adapter_factory=reviewer,
    )

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, setup_url_from(caplog))
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        codex_onboarding.complete()
        chatgpt_only = await wait_for_setup_text(client, "Success. Connected")

        installed = await connect_github(client, csrf_token=csrf_token)
        health = await client.get("/healthz")
        closed_setup = await client.get("/setup")

    assert chatgpt_only.status_code == 200
    assert "Connect GitHub" in chatgpt_only.text
    assert installed.status_code == 303
    assert health.json() == {"status": "ok", "mode": "reviewer"}
    assert closed_setup.status_code == 404
    assert len(reviewer.connections) == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_promotion_waits_for_an_admitted_setup_response_to_finish(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    provider = github_provider(private_key=private_key, webhook_secret=webhook_secret)
    codex_onboarding = FakeCodexOnboarding()
    reviewer = FakeReviewerAdapterFactory()
    configured = setup_settings(tmp_path)
    setup_request_started = asyncio.Event()
    release_setup_request = asyncio.Event()
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        configured,
        github_onboarding_client=provider,
        codex_onboarding=codex_onboarding,
        reviewer_adapter_factory=reviewer,
    )

    @app.get("/setup/in-flight")
    async def in_flight_setup_request() -> StreamingResponse:
        async def stream_setup_response() -> AsyncIterator[bytes]:
            setup_request_started.set()
            yield b"setup response started\n"
            await release_setup_request.wait()
            yield b"setup response finished\n"

        return StreamingResponse(stream_setup_response(), media_type="text/plain")

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, setup_url_from(caplog))
        await connect_github(client, csrf_token=csrf_token)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        admitted_setup = asyncio.create_task(client.get("/setup/in-flight"))
        await setup_request_started.wait()

        codex_onboarding.complete()
        await codex_onboarding.completion_returned.wait()

        assert not (configured.data_dir / "readiness.json").exists()
        blocked_webhook = await post_signed_webhook(client, secret=webhook_secret)
        assert blocked_webhook.status_code == 503

        release_setup_request.set()
        completed_setup = await admitted_setup
        promoted = await wait_for_mode(client, "reviewer")

    assert completed_setup.status_code == 200
    assert completed_setup.text.endswith("setup response finished\n")
    assert promoted.json() == {"status": "ok", "mode": "reviewer"}
    assert len(reviewer.connections) == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_setup_gate_releases_after_a_downstream_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        setup_settings(tmp_path),
        codex_onboarding=FakeCodexOnboarding(),
        reviewer_adapter_factory=FakeReviewerAdapterFactory(),
    )

    @app.get("/setup/fails")
    async def failing_setup_request() -> None:
        raise RuntimeError("synthetic downstream failure")

    async with app.router.lifespan_context(app), setup_client(app) as client:
        await establish_setup_session(client, setup_url_from(caplog))
        with pytest.raises(RuntimeError, match="synthetic downstream failure"):
            await client.get("/setup/fails")
        setup_after_failure = await client.get("/setup")

    assert setup_after_failure.status_code == 200


@pytest.mark.asyncio
async def test_transient_runtime_failure_retries_from_the_live_setup_page(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    provider = github_provider(private_key=private_key, webhook_secret=webhook_secret)
    codex_onboarding = FakeCodexOnboarding()
    provider_detail = "provider-secret-that-must-not-escape"
    reviewer = FlakyReviewerAdapterFactory(provider_detail)
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        setup_settings(tmp_path),
        github_onboarding_client=provider,
        codex_onboarding=codex_onboarding,
        reviewer_adapter_factory=reviewer,
    )

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, setup_url_from(caplog))
        await connect_github(client, csrf_token=csrf_token)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        codex_onboarding.complete()
        await reviewer.first_failure.wait()

        retry = await client.get("/setup")
        health = await client.get("/healthz")

    assert retry.status_code == 200
    assert health.json() == {"status": "ok", "mode": "reviewer"}
    assert reviewer.attempts == 2
    assert provider_detail not in caplog.text
    await provider.aclose()


@pytest.mark.asyncio
async def test_failed_session_removal_closes_setup_without_claiming_reviewer_mode(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    provider = github_provider(private_key=private_key, webhook_secret=webhook_secret)
    codex_onboarding = FakeCodexOnboarding()
    reviewer = FakeReviewerAdapterFactory()
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        configured,
        github_onboarding_client=provider,
        codex_onboarding=codex_onboarding,
        reviewer_adapter_factory=reviewer,
    )

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, setup_url_from(caplog))
        await connect_github(client, csrf_token=csrf_token)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )

        setup_state = configured.data_dir / "setup-state.json"
        setup_state.unlink()
        setup_state.mkdir()
        codex_onboarding.complete()
        unavailable = await wait_for_mode(client, "unavailable")
        closed_setup = await client.get("/setup")
        webhook = await post_signed_webhook(client, secret=webhook_secret)

    assert unavailable.json() == {"status": "ok", "mode": "unavailable"}
    assert closed_setup.status_code == 404
    assert webhook.status_code == 503
    assert len(reviewer.connections) == 1
    await provider.aclose()


@pytest.mark.asyncio
async def test_persisted_ready_marker_seals_setup_when_directory_sync_fails(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    provider = github_provider(private_key=private_key, webhook_secret=webhook_secret)
    codex_onboarding = FakeCodexOnboarding()
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        configured,
        github_onboarding_client=provider,
        codex_onboarding=codex_onboarding,
        reviewer_adapter_factory=FakeReviewerAdapterFactory(),
    )

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, setup_url_from(caplog))
        await connect_github(client, csrf_token=csrf_token)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )

        original_replace = private_state_module.os.replace
        original_fsync = private_state_module.os.fsync
        readiness_replaced = False
        directory_sync_failed = False

        def track_readiness_replace(source: object, destination: object) -> None:
            nonlocal readiness_replaced
            original_replace(source, destination)
            if Path(destination).name == "readiness.json":
                readiness_replaced = True

        def fail_sync_after_readiness_replace(file_descriptor: int) -> None:
            nonlocal directory_sync_failed
            if (
                readiness_replaced
                and not directory_sync_failed
                and stat.S_ISDIR(private_state_module.os.fstat(file_descriptor).st_mode)
            ):
                directory_sync_failed = True
                raise OSError("synthetic readiness directory sync failure")
            original_fsync(file_descriptor)

        monkeypatch.setattr(private_state_module.os, "replace", track_readiness_replace)
        monkeypatch.setattr(private_state_module.os, "fsync", fail_sync_after_readiness_replace)

        codex_onboarding.complete()
        unavailable = await wait_for_mode(client, "unavailable")
        closed_setup = await client.get("/setup")
        webhook = await post_signed_webhook(client, secret=webhook_secret)

    persisted_readiness = json.loads(
        (configured.data_dir / "readiness.json").read_text(encoding="utf-8")
    )
    assert directory_sync_failed
    assert persisted_readiness == {"version": 1, "status": "ready"}
    assert not (configured.data_dir / "setup-state.json").exists()
    assert unavailable.json() == {"status": "ok", "mode": "unavailable"}
    assert closed_setup.status_code == 404
    assert webhook.status_code == 503
    assert "synthetic readiness directory sync failure" not in caplog.text
    await provider.aclose()


@pytest.mark.asyncio
async def test_ready_instance_with_invalid_runtime_fails_closed_without_secret_chain(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    seed_ready_state(
        configured.data_dir,
        private_key=private_key,
        webhook_secret=webhook_secret,
    )
    provider_detail = "provider-secret-that-must-not-escape"
    provider = github_provider(private_key=private_key, webhook_secret=webhook_secret)
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        configured,
        github_onboarding_client=provider,
        codex_onboarding=FakeCodexOnboarding(),
        reviewer_adapter_factory=RejectingReviewerAdapterFactory(provider_detail),
    )

    async with app.router.lifespan_context(app), setup_client(app) as client:
        setup = await client.get("/setup")
        health = await client.get("/healthz")
        webhook = await post_signed_webhook(client, secret=webhook_secret)

    assert setup.status_code == 404
    assert health.json() == {"status": "ok", "mode": "unavailable"}
    assert webhook.status_code == 503
    assert "Poppy Setup Session" not in caplog.text
    assert provider_detail not in caplog.text
    await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("state_file", ["github-setup.json", "chatgpt-setup.json"])
async def test_ready_marker_with_corrupt_integration_state_serves_unavailable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    state_file: str,
) -> None:
    configured = setup_settings(tmp_path)
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    seed_ready_state(
        configured.data_dir,
        private_key=private_key,
        webhook_secret=webhook_secret,
    )
    provider_detail = "persisted-secret-that-must-not-escape"
    (configured.data_dir / state_file).write_text(
        json.dumps({"unexpected": provider_detail}),
        encoding="utf-8",
    )
    provider = github_provider(private_key=private_key, webhook_secret=webhook_secret)
    caplog.set_level(logging.WARNING, logger="poppy.service")

    async with provider:
        app = create_service(
            configured,
            github_onboarding_client=provider,
            codex_onboarding=FakeCodexOnboarding(),
            reviewer_adapter_factory=FakeReviewerAdapterFactory(),
        )
        async with app.router.lifespan_context(app), setup_client(app) as client:
            health = await client.get("/healthz")
            setup = await client.get("/setup")
            webhook = await post_signed_webhook(client, secret=webhook_secret)

    assert health.json() == {"status": "ok", "mode": "unavailable"}
    assert setup.status_code == 404
    assert webhook.status_code == 503
    assert "Poppy Setup Session" not in caplog.text
    assert provider_detail not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_secret",
    [["x"] * 16, "\ud800" * 16],
    ids=["wrong-type", "not-utf8-encodable"],
)
async def test_ready_marker_with_parseable_corrupt_secret_serves_unavailable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    corrupt_secret: object,
) -> None:
    configured = setup_settings(tmp_path)
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    seed_ready_state(
        configured.data_dir,
        private_key=private_key,
        webhook_secret=webhook_secret,
    )
    state_file = configured.data_dir / "github-setup.json"
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    persisted["app"]["webhook_secret"] = corrupt_secret
    state_file.write_text(json.dumps(persisted), encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="poppy.service")

    app = create_service(
        configured,
        codex_onboarding=FakeCodexOnboarding(),
        reviewer_adapter_factory=FakeReviewerAdapterFactory(),
    )
    async with app.router.lifespan_context(app), setup_client(app) as client:
        health = await client.get("/healthz")
        setup = await client.get("/setup")
        webhook = await post_signed_webhook(client, secret=webhook_secret)

    assert health.json() == {"status": "ok", "mode": "unavailable"}
    assert setup.status_code == 404
    assert webhook.status_code == 503
    assert "Poppy Setup Session" not in caplog.text


@pytest.mark.asyncio
async def test_ready_instance_canonicalizes_relative_codex_home_without_preparing_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = setup_settings(tmp_path)
    private_key = github_private_key()
    seed_ready_state(
        configured.data_dir,
        private_key=private_key,
        webhook_secret="generated-webhook-secret",
    )
    monkeypatch.chdir(tmp_path)
    configured.codex_home = Path("relative-codex-home")
    codex_factory = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(service_module, "AsyncCodex", codex_factory)
    caplog.set_level(logging.WARNING, logger="poppy.service")

    async with github_provider(
        private_key=private_key,
        webhook_secret="generated-webhook-secret",
    ) as provider:
        create_service(
            configured,
            github_onboarding_client=provider,
            reviewer_adapter_factory=FakeReviewerAdapterFactory(),
        )

    codex_config = codex_factory.call_args.args[0]
    assert codex_config.cwd == str(tmp_path / "relative-codex-home")
    assert not (tmp_path / "relative-codex-home").exists()
    assert "Poppy Setup Session" not in caplog.text


def test_symlinked_readiness_state_fails_closed_without_touching_target(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    configured.data_dir.mkdir(mode=0o700)
    unrelated = tmp_path / "unrelated.json"
    unrelated.write_text('{"do_not":"touch"}', encoding="utf-8")
    (configured.data_dir / "readiness.json").symlink_to(unrelated)
    caplog.set_level(logging.WARNING, logger="poppy.service")

    with pytest.raises(RuntimeConfigurationError, match="setup state"):
        create_service(configured)

    assert unrelated.read_text(encoding="utf-8") == '{"do_not":"touch"}'
    assert "Poppy Setup Session" not in caplog.text


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        '{"version":true,"status":"ready"}',
        '{"version":1,"status":"ready","unexpected":true}',
    ],
)
def test_corrupt_readiness_state_never_creates_a_setup_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    payload: str,
) -> None:
    configured = setup_settings(tmp_path)
    configured.data_dir.mkdir(mode=0o700)
    (configured.data_dir / "readiness.json").write_text(payload, encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="poppy.service")

    with pytest.raises(RuntimeConfigurationError, match="setup state"):
        create_service(configured)

    assert "Poppy Setup Session" not in caplog.text


@pytest.mark.asyncio
async def test_ready_restart_reuses_one_runtime_client_pair_and_closes_each_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = setup_settings(tmp_path)
    private_key = github_private_key()
    seed_ready_state(
        configured.data_dir,
        private_key=private_key,
        webhook_secret="generated-webhook-secret",
    )
    codex_client = MagicMock()
    codex_client.close = AsyncMock()
    github_client = MagicMock(spec=httpx.AsyncClient)
    github_client.aclose = AsyncMock()
    github_client.get = AsyncMock(return_value=validated_installation_response())
    account_metadata = "private-plan-metadata"
    require_account = AsyncMock(return_value=account_metadata)
    codex_factory = MagicMock(return_value=codex_client)
    github_factory = MagicMock(return_value=github_client)
    monkeypatch.setattr(service_module, "AsyncCodex", codex_factory)
    monkeypatch.setattr(service_module.httpx, "AsyncClient", github_factory)
    monkeypatch.setattr(service_module, "require_chatgpt_account", require_account)
    caplog.set_level(logging.INFO, logger="poppy.service")

    app = create_service(configured)
    async with app.router.lifespan_context(app), setup_client(app) as client:
        health = await client.get("/healthz")

    assert health.json() == {"status": "ok", "mode": "reviewer"}
    assert codex_factory.call_count == 1
    assert github_factory.call_count == 1
    require_account.assert_awaited_once_with(codex_client)
    github_client.get.assert_awaited_once()
    codex_client.close.assert_awaited_once()
    github_client.aclose.assert_awaited_once()
    assert "Poppy Setup Session" not in caplog.text
    assert account_metadata not in caplog.text
    assert not any(hasattr(record, "chatgpt_plan") for record in caplog.records)


@pytest.mark.asyncio
async def test_ready_restart_with_revoked_github_installation_stays_unavailable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = setup_settings(tmp_path)
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"
    seed_ready_state(
        configured.data_dir,
        private_key=private_key,
        webhook_secret=webhook_secret,
    )
    provider_detail = "provider-secret-that-must-not-escape"
    codex_client = MagicMock()
    codex_client.close = AsyncMock()
    github_client = MagicMock(spec=httpx.AsyncClient)
    github_client.aclose = AsyncMock()
    github_client.get = AsyncMock(
        return_value=httpx.Response(
            404,
            headers={"x-github-request-id": provider_detail},
        )
    )
    monkeypatch.setattr(service_module, "AsyncCodex", MagicMock(return_value=codex_client))
    monkeypatch.setattr(service_module.httpx, "AsyncClient", MagicMock(return_value=github_client))
    monkeypatch.setattr(
        service_module,
        "require_chatgpt_account",
        AsyncMock(return_value="plus"),
    )
    caplog.set_level(logging.WARNING, logger="poppy.service")

    app = create_service(configured)
    async with app.router.lifespan_context(app), setup_client(app) as client:
        health = await client.get("/healthz")
        setup = await client.get("/setup")
        webhook = await post_signed_webhook(client, secret=webhook_secret)

    assert health.json() == {"status": "ok", "mode": "unavailable"}
    assert setup.status_code == 404
    assert webhook.status_code == 503
    assert provider_detail not in caplog.text
    codex_client.close.assert_awaited_once()
    github_client.aclose.assert_awaited_once()


@dataclass
class FakeCodexOnboarding:
    completion: asyncio.Event = field(default_factory=asyncio.Event)
    completion_returned: asyncio.Event = field(default_factory=asyncio.Event)

    async def start_device_authorization(self) -> CodexDeviceAuthorization:
        return CodexDeviceAuthorization(
            login_id="login-1",
            verification_url="https://auth.openai.com/codex/device",
            user_code="ABCD-1234",
        )

    async def wait_for_completion(self, login_id: str) -> None:
        assert login_id == "login-1"
        await self.completion.wait()
        self.completion_returned.set()

    async def cancel(self, login_id: str) -> None:
        raise AssertionError(f"unexpected cancellation for {login_id}")

    async def verify_chatgpt_account(self) -> None:
        return None

    def complete(self) -> None:
        self.completion.set()


class FakeReviewerAdapterFactory:
    def __init__(self) -> None:
        self.github = FakeGitHub()
        self.codex = FakeCodex()
        self.connections: list[ReviewerConnection] = []

    async def create(self, connection: ReviewerConnection) -> ReviewerAdapters:
        self.connections.append(connection)
        return ReviewerAdapters(github=self.github, codex=self.codex)


class RejectingReviewerAdapterFactory:
    def __init__(self, detail: str) -> None:
        self._detail = detail

    async def create(self, connection: ReviewerConnection) -> ReviewerAdapters:
        raise RuntimeError(self._detail)


class FlakyReviewerAdapterFactory:
    def __init__(self, detail: str) -> None:
        self._detail = detail
        self.attempts = 0
        self.first_failure = asyncio.Event()
        self.github = FakeGitHub()
        self.codex = FakeCodex()

    async def create(self, connection: ReviewerConnection) -> ReviewerAdapters:
        self.attempts += 1
        if self.attempts == 1:
            self.first_failure.set()
            raise RuntimeError(self._detail)
        return ReviewerAdapters(github=self.github, codex=self.codex)


class FakeGitHub:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.review_finished = asyncio.Event()

    async def start_check(self, request: ReviewRequest) -> int:
        self.events.append(("start_check", request.head_sha))
        return 77

    async def fetch_pull_request(self, ref: PullRequestRef) -> PullRequestSnapshot:
        self.events.append(("fetch_pull_request", ref.number))
        return pull_request_snapshot()

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
        self.review_finished.set()


class FakeCodex:
    def __init__(self) -> None:
        self.snapshots: list[PullRequestSnapshot] = []

    async def review(self, snapshot: PullRequestSnapshot) -> ReviewResult:
        self.snapshots.append(snapshot)
        return ReviewResult(summary="No concrete defects.", risk=Severity.LOW, findings=())


def github_provider(*, private_key: str, webhook_secret: str) -> httpx.AsyncClient:
    async def github(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app-manifests/manifest-code/conversions":
            return httpx.Response(
                201,
                json={
                    "id": 123,
                    "slug": "poppy-example",
                    "pem": private_key,
                    "webhook_secret": webhook_secret,
                },
            )
        if request.url.path == "/app/installations/456":
            return httpx.Response(200, json=github_installation_payload())
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    return httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(github),
    )


def validated_installation_response() -> httpx.Response:
    return httpx.Response(200, json=github_installation_payload())


async def connect_github(client: httpx.AsyncClient, *, csrf_token: str) -> httpx.Response:
    manifest = await client.post(
        "/setup/github/connect",
        data={"csrf_token": csrf_token},
        headers={"origin": "https://poppy.example"},
    )
    manifest_callback = await client.get(
        "/setup/github/manifest/callback",
        params={"code": "manifest-code", "state": manifest_state_from(manifest)},
    )
    installation_state = parse_qs(urlparse(manifest_callback.headers["location"]).query)["state"][0]
    return await client.get(
        "/setup/github/installation",
        params={"installation_id": "456", "state": installation_state},
    )


async def post_signed_webhook(client: httpx.AsyncClient, *, secret: str) -> httpx.Response:
    body = json.dumps(webhook_payload(), separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
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


def webhook_payload() -> dict[str, object]:
    return {
        "action": "opened",
        "number": 7,
        "installation": {"id": 456},
        "repository": {
            "id": 999,
            "name": "example",
            "owner": {"login": "operator"},
        },
        "pull_request": {
            "title": "Add review gate",
            "body": "Implements the onboarding runtime.",
            "draft": False,
            "head": {"sha": "head-sha"},
            "base": {"ref": "main"},
        },
    }


def pull_request_snapshot() -> PullRequestSnapshot:
    return PullRequestSnapshot(
        ref=PullRequestRef(
            installation_id=456,
            repository_id=999,
            owner="operator",
            repository="example",
            number=7,
        ),
        head_sha="head-sha",
        title="Add review gate",
        body="Implements the onboarding runtime.",
        base_ref="main",
        is_draft=False,
        is_open=True,
        additions=120,
        deletions=12,
        files=(
            ChangedFile(
                path="src/poppy/runtime.py",
                additions=120,
                deletions=12,
                patch="@@ -1 +1 @@\n-old\n+new",
            ),
        ),
    )


def seed_ready_state(
    data_dir: Path,
    *,
    private_key: str,
    webhook_secret: str,
) -> None:
    data_dir.mkdir(mode=0o700)
    github = GitHubSetupStore(data_dir=data_dir)
    registration = github.begin_manifest(
        organization=None,
        public_origin="https://poppy.example",
    )
    assert registration is not None
    assert github.consume_manifest(registration.correlation_state)
    pending = github.await_installation(
        GitHubAppCredentials(
            app_id=123,
            slug="poppy-example",
            private_key=private_key,
            webhook_secret=webhook_secret,
        )
    )
    consumed = github.consume_installation(pending.correlation_state)
    assert consumed is not None
    assert github.connect(
        pending=consumed,
        installation=GitHubInstallation(
            installation_id=456,
            account_id=789,
            account_login="operator",
            target_type="User",
        ),
    )
    chatgpt_state = data_dir / "chatgpt-setup.json"
    chatgpt_state.write_text('{"version":1,"status":"connected"}', encoding="utf-8")
    chatgpt_state.chmod(0o600)
    ReadinessStore(data_dir=data_dir).mark_ready()
