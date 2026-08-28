import asyncio
import json
import logging
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from poppy.adapters.codex_onboarding import (
    CodexDeviceAuthorization,
    CodexOnboardingError,
    CodexUnsupportedAccountError,
)
from poppy.adapters.github_onboarding import GitHubAppCredentials, GitHubInstallation
from poppy.config import PoppySettings
from poppy.github_setup import GitHubSetupStore
from poppy.service import RuntimeConfigurationError, create_service


@pytest.mark.asyncio
async def test_connect_chatgpt_starts_device_authorization_and_shows_only_activation_data(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codex = FakeCodexOnboarding()
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        started = await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        page = await client.get("/setup")

    assert started.status_code == 303
    assert started.headers["location"] == "/setup"
    assert codex.start_count == 1
    assert "Pending" in page.text
    assert '<meta http-equiv="refresh" content="2; url=/setup">' in page.text
    assert 'href="https://auth.openai.com/codex/device"' in page.text
    assert "ABCD-1234" in page.text
    assert "login-1" not in page.text
    assert "API keys are not supported" in page.text


@pytest.mark.asyncio
async def test_success_is_connected_without_github_and_survives_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    credential = "codex-refresh-token-never-returned"
    codex = FakeCodexOnboarding(
        credential_file=configured.codex_home / "auth.json",
        credential=credential,
    )
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(configured, codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        codex.complete()
        connected_page = await wait_for_page_text(client, "Connected")
        github_after_chatgpt = await client.post(
            "/setup/github/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        session_cookie = client.cookies.get("poppy_setup_session")

    assert "Connect GitHub" in connected_page.text
    assert "Success. Connected" in connected_page.text
    assert github_after_chatgpt.status_code == 200
    assert "Register Poppy on GitHub" in github_after_chatgpt.text
    assert credential not in connected_page.text
    assert credential not in caplog.text
    assert configured.codex_home.is_relative_to(configured.data_dir)
    assert codex.credential_file is not None
    assert codex.credential_file.read_text(encoding="utf-8") == credential
    state_file = configured.data_dir / "chatgpt-setup.json"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert credential not in state_file.read_text(encoding="utf-8")

    restarted = create_service(configured, codex_onboarding=FakeCodexOnboarding())
    async with restarted.router.lifespan_context(restarted), setup_client(restarted) as client:
        assert session_cookie is not None
        client.cookies.set(
            "poppy_setup_session",
            session_cookie,
            domain="poppy.example",
            path="/setup",
        )
        restarted_page = await client.get("/setup")

    assert "ChatGPT" in restarted_page.text
    assert "Connected" in restarted_page.text


@pytest.mark.asyncio
async def test_chatgpt_can_connect_after_github_is_already_connected(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    seed_connected_github(configured.data_dir)
    codex = FakeCodexOnboarding()
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(configured, codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        github_first_page = await client.get("/setup")
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        codex.complete()
        both_connected_page = await wait_for_page_text(client, "Success. Connected")

    assert re.search(r"<dt>GitHub</dt>\s*<dd>Connected</dd>", github_first_page.text)
    assert "Connect ChatGPT" in github_first_page.text
    assert re.search(r"<dt>GitHub</dt>\s*<dd>Connected</dd>", both_connected_page.text)
    assert "Success. Connected" in both_connected_page.text


@pytest.mark.asyncio
async def test_failed_authorization_is_not_connected_and_can_be_retried_safely(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider_detail = "provider-error-containing-sensitive-detail"
    codex = FakeCodexOnboarding(
        start_failures=[CodexOnboardingError(provider_detail)],
    )
    caplog.set_level(logging.WARNING)
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        failed = await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        failed_page = await client.get("/setup")
        retried = await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        pending_page = await client.get("/setup")
        codex.complete()
        connected_page = await wait_for_page_text(client, "Connected")

    assert failed.status_code == 303
    assert "Failed" in failed_page.text
    assert "Retry ChatGPT" in failed_page.text
    assert "Connected" not in failed_page.text
    assert provider_detail not in failed.text
    assert provider_detail not in failed_page.text
    assert provider_detail not in caplog.text
    assert retried.status_code == 303
    assert "Pending" in pending_page.text
    assert codex.start_count == 2
    assert "Connected" in connected_page.text


@pytest.mark.asyncio
async def test_pending_authorization_can_be_cancelled_and_retried(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codex = FakeCodexOnboarding()
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        cancelled = await client.post(
            "/setup/chatgpt/cancel",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        cancelled_page = await client.get("/setup")
        retried = await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        pending_page = await client.get("/setup")

    assert cancelled.status_code == 303
    assert codex.cancelled_login_ids == ["login-1"]
    assert "Cancelled" in cancelled_page.text
    assert "Retry ChatGPT" in cancelled_page.text
    assert "Connected" not in cancelled_page.text
    assert retried.status_code == 303
    assert "Pending" in pending_page.text
    assert codex.start_count == 2


@pytest.mark.asyncio
async def test_cancel_failure_is_sanitized_and_leaves_authorization_pending(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider_detail = "cancel response containing sensitive provider detail"
    codex = FakeCodexOnboarding(
        cancel_failures=[CodexOnboardingError(provider_detail)],
    )
    caplog.set_level(logging.WARNING)
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        cancelled = await client.post(
            "/setup/chatgpt/cancel",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        page = await client.get("/setup")

    assert cancelled.status_code == 502
    assert cancelled.json() == {
        "error": "ChatGPT authorization could not be cancelled; retry from setup"
    }
    assert "Pending" in page.text
    assert "Cancelled" not in page.text
    assert provider_detail not in cancelled.text
    assert provider_detail not in page.text
    assert provider_detail not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "provider_detail"),
    [
        (CodexOnboardingError("device authorization expired"), "device authorization expired"),
        (
            CodexUnsupportedAccountError("API-key account identifier"),
            "API-key account identifier",
        ),
    ],
)
async def test_failed_and_non_chatgpt_completions_never_become_connected(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failure: CodexOnboardingError,
    provider_detail: str,
) -> None:
    codex = FakeCodexOnboarding()
    caplog.set_level(logging.WARNING)
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        codex.complete(failure)
        failed_page = await wait_for_page_text(client, "Failed")

    assert "Connected" not in failed_page.text
    assert "Retry ChatGPT" in failed_page.text
    assert provider_detail not in failed_page.text
    assert provider_detail not in caplog.text


@pytest.mark.asyncio
async def test_interrupted_or_partial_authorization_is_failed_after_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.WARNING, logger="poppy.service")
    first_app = create_service(configured, codex_onboarding=FakeCodexOnboarding())

    async with first_app.router.lifespan_context(first_app), setup_client(first_app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        session_cookie = client.cookies.get("poppy_setup_session")

    partial_credential = "partial-auth-data-that-is-not-a-valid-login"
    (configured.codex_home / "auth.json").write_text(partial_credential, encoding="utf-8")
    restarted_codex = FakeCodexOnboarding()
    restarted = create_service(configured, codex_onboarding=restarted_codex)
    async with restarted.router.lifespan_context(restarted), setup_client(restarted) as client:
        assert session_cookie is not None
        client.cookies.set(
            "poppy_setup_session",
            session_cookie,
            domain="poppy.example",
            path="/setup",
        )
        page = await client.get("/setup")

    assert "Failed" in page.text
    assert "Retry ChatGPT" in page.text
    assert "Connected" not in page.text
    assert "ABCD-1234" not in page.text
    assert partial_credential not in page.text
    assert restarted_codex.start_count == 0


@pytest.mark.asyncio
async def test_repeated_connect_keeps_one_active_device_authorization(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codex = FakeCodexOnboarding()
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        first = await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        repeated = await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )

    assert first.status_code == repeated.status_code == 303
    assert codex.start_count == 1


@pytest.mark.asyncio
async def test_success_winning_a_cancel_race_cannot_be_overwritten(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cancel_release = asyncio.Event()
    codex = FakeCodexOnboarding(cancel_release=cancel_release)
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        cancel_request = asyncio.create_task(
            client.post(
                "/setup/chatgpt/cancel",
                data={"csrf_token": csrf_token},
                headers={"origin": "https://poppy.example"},
            )
        )
        await codex.cancel_started.wait()
        codex.complete()
        await wait_for_page_text(client, "Connected")
        cancel_release.set()
        cancelled = await cancel_request
        final_page = await client.get("/setup")

    assert cancelled.status_code == 409
    assert "Connected" in final_page.text
    assert "Cancelled" not in final_page.text


@pytest.mark.asyncio
async def test_activation_data_is_html_escaped(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    codex = FakeCodexOnboarding(
        verification_url='https://auth.openai.com/codex/device?next="><script>x</script>',
        user_code="<img/src=x/onerror=x>",
    )
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        await client.post(
            "/setup/chatgpt/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        page = await client.get("/setup")

    assert "<script>" not in page.text
    assert "<img" not in page.text
    assert "&quot;&gt;&lt;script&gt;x&lt;/script&gt;" in page.text
    assert "&lt;img/src=x/onerror=x&gt;" in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/setup/chatgpt/connect", "/setup/chatgpt/cancel"])
async def test_chatgpt_mutations_require_the_protected_setup_session(
    tmp_path: Path,
    path: str,
) -> None:
    codex = FakeCodexOnboarding()
    app = create_service(setup_settings(tmp_path), codex_onboarding=codex)

    async with app.router.lifespan_context(app), setup_client(app) as client:
        response = await client.post(
            path,
            data={"csrf_token": "invalid"},
            headers={"origin": "https://poppy.example"},
        )

    assert response.status_code == 404
    assert codex.start_count == 0
    assert codex.cancelled_login_ids == []


@pytest.mark.parametrize(
    "persisted_state",
    [
        {"version": True, "status": "connected"},
        {"version": 1, "status": "connected", "refresh_token": "must-not-be-here"},
        {
            "version": 1,
            "status": "pending",
            "login_id": "login-1",
            "verification_url": "javascript:alert(1)",
            "user_code": "ABCD-1234",
        },
    ],
)
def test_malformed_or_credential_bearing_chatgpt_state_fails_closed(
    tmp_path: Path,
    persisted_state: dict[str, object],
) -> None:
    configured = setup_settings(tmp_path)
    configured.data_dir.mkdir(mode=0o700)
    (configured.data_dir / "chatgpt-setup.json").write_text(
        json.dumps(persisted_state),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeConfigurationError, match="setup state"):
        create_service(configured, codex_onboarding=FakeCodexOnboarding())


@dataclass
class FakeCodexOnboarding:
    start_count: int = 0
    credential_file: Path | None = None
    credential: str = ""
    start_failures: list[CodexOnboardingError] = field(default_factory=list)
    completions: dict[str, asyncio.Event] = field(default_factory=dict)
    completion_failures: dict[str, CodexOnboardingError] = field(default_factory=dict)
    cancelled_login_ids: list[str] = field(default_factory=list)
    verification_url: str = "https://auth.openai.com/codex/device"
    user_code: str = "ABCD-1234"
    cancel_release: asyncio.Event | None = None
    cancel_started: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_failures: list[CodexOnboardingError] = field(default_factory=list)

    async def start_device_authorization(self) -> CodexDeviceAuthorization:
        self.start_count += 1
        if self.start_failures:
            raise self.start_failures.pop(0)
        login_id = f"login-{self.start_count}"
        self.completions[login_id] = asyncio.Event()
        return CodexDeviceAuthorization(
            login_id=login_id,
            verification_url=self.verification_url,
            user_code=self.user_code,
        )

    async def wait_for_completion(self, login_id: str) -> None:
        await self.completions[login_id].wait()
        failure = self.completion_failures.get(login_id)
        if failure is not None:
            raise failure
        if self.credential_file is not None:
            self.credential_file.write_text(self.credential, encoding="utf-8")

    async def cancel(self, login_id: str) -> None:
        self.cancelled_login_ids.append(login_id)
        self.cancel_started.set()
        if self.cancel_failures:
            raise self.cancel_failures.pop(0)
        if self.cancel_release is not None:
            await self.cancel_release.wait()

    def complete(self, failure: CodexOnboardingError | None = None) -> None:
        login_id = f"login-{self.start_count}"
        if failure is not None:
            self.completion_failures[login_id] = failure
        self.completions[login_id].set()


def setup_settings(tmp_path: Path) -> PoppySettings:
    return PoppySettings(
        public_base_url="https://poppy.example",
        data_dir=tmp_path / "data",
        codex_home=tmp_path / "data" / "codex",
        codex_workspace=tmp_path / "workspace",
        _env_file=None,
    )


def seed_connected_github(data_dir: Path) -> None:
    data_dir.mkdir(mode=0o700)
    store = GitHubSetupStore(data_dir=data_dir)
    registration = store.begin_manifest(
        organization=None,
        public_origin="https://poppy.example",
    )
    assert registration is not None
    assert store.consume_manifest(registration.correlation_state)
    pending = store.await_installation(
        GitHubAppCredentials(
            app_id=123,
            slug="poppy-example",
            private_key=github_private_key(),
            webhook_secret="generated-webhook-secret",
        )
    )
    consumed = store.consume_installation(pending.correlation_state)
    assert consumed is not None
    assert store.connect(
        pending=consumed,
        installation=GitHubInstallation(
            installation_id=456,
            account_id=789,
            account_login="operator",
            target_type="User",
        ),
    )


def github_private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


async def establish_setup_session(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> str:
    match = re.search(r"Poppy Setup Session: (https:\/\/\S+)", caplog.text)
    assert match is not None
    exchange = await client.get(match.group(1))
    page = await client.get(exchange.headers["location"])
    csrf_token = re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)
    assert csrf_token is not None
    return csrf_token.group(1)


def setup_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://poppy.example",
        follow_redirects=False,
    )


async def wait_for_page_text(client: httpx.AsyncClient, expected: str) -> httpx.Response:
    for _ in range(20):
        page = await client.get("/setup")
        if expected in page.text:
            return page
        await asyncio.sleep(0)
    raise AssertionError(f"setup page did not contain {expected!r}")
