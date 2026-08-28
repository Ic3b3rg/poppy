import json
import logging
import re
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from poppy.config import PoppySettings
from poppy.service import RuntimeConfigurationError, create_service
from tests.onboarding_support import setup_client, setup_settings, setup_url_from


@pytest.mark.asyncio
async def test_fresh_instance_starts_in_setup_mode_without_integration_credentials(
    tmp_path: Path,
) -> None:
    app = create_service(
        PoppySettings(
            public_base_url="https://poppy.example",
            data_dir=tmp_path / "data",
            codex_home=tmp_path / "data" / "codex",
            codex_workspace=tmp_path / "workspace",
            _env_file=None,
        )
    )

    async with app.router.lifespan_context(app), setup_client(app) as client:
        response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "setup"}


def test_unusable_setup_data_path_is_reported_as_runtime_configuration_error(
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("occupied", encoding="utf-8")
    configured = setup_settings(tmp_path)
    configured.data_dir = blocked_parent / "data"

    with pytest.raises(RuntimeConfigurationError, match="setup state"):
        create_service(configured)


@pytest.mark.asyncio
async def test_first_boot_link_establishes_a_secure_one_time_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))
    setup_url = setup_url_from(caplog)
    parsed_url = urlparse(setup_url)
    token = parse_qs(parsed_url.query)["token"][0]

    async with setup_client(app) as client:
        response = await client.get(setup_url)

    assert parsed_url.scheme == "https"
    assert parsed_url.netloc == "poppy.example"
    assert parsed_url.path == "/setup"
    assert len(token) >= 43
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"
    set_cookie = response.headers["set-cookie"]
    assert "poppy_setup_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "Path=/setup" in set_cookie


def test_bootstrap_link_is_emitted_at_the_default_logging_threshold(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="poppy.service")

    create_service(setup_settings(tmp_path))

    assert setup_url_from(caplog).startswith("https://poppy.example/setup?token=")


@pytest.mark.asyncio
async def test_unconfirmed_session_exchange_is_recoverable_after_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.WARNING, logger="poppy.service")
    first_app = create_service(configured)
    setup_url = setup_url_from(caplog)
    async with setup_client(first_app) as first_client:
        first_response = await first_client.get(setup_url)
        first_session = first_client.cookies.get("poppy_setup_session")

    caplog.clear()
    restarted_app = create_service(configured)
    recovery_url = setup_url_from(caplog)
    async with setup_client(restarted_app) as recovered_client:
        original_replay = await recovered_client.get(setup_url)
        recovered_response = await recovered_client.get(recovery_url)
        recovered_session = recovered_client.cookies.get("poppy_setup_session")
        page = await recovered_client.get(recovered_response.headers["location"])
        recovery_replay = await recovered_client.get(recovery_url)

    assert first_response.status_code == 303
    assert recovery_url != setup_url
    assert original_replay.status_code == 404
    assert recovered_response.status_code == 303
    assert recovered_session == first_session
    assert page.status_code == 200
    assert recovery_replay.status_code == 404


@pytest.mark.asyncio
async def test_setup_session_renders_minimal_server_page_without_the_bootstrap_token(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))
    setup_url = setup_url_from(caplog)
    token = parse_qs(urlparse(setup_url).query)["token"][0]

    async with setup_client(app) as client:
        exchange = await client.get(setup_url)
        page = await client.get(exchange.headers["location"])

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["content-security-policy"].startswith("default-src 'none'")
    assert "<h1>Set up Poppy</h1>" in page.text
    assert "GitHub" in page.text
    assert "ChatGPT" in page.text
    assert token not in page.text


@pytest.mark.asyncio
async def test_expired_session_cannot_read_setup_state_and_is_removed_from_browser(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = MutableClock(2_000_000_000)
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(configured, clock=clock)
    setup_url = setup_url_from(caplog)

    async with setup_client(app) as client:
        await client.get(setup_url)
        clock.advance(7 * 24 * 60 * 60 + 1)
        response = await client.get("/setup")

    assert response.status_code == 404
    assert 'poppy_setup_session=""' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_setup_mutations_require_same_origin_and_csrf_token(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))
    setup_url = setup_url_from(caplog)

    async with setup_client(app) as client:
        await client.get(setup_url)
        page = await client.get("/setup")
        csrf_token = re.search(
            r'<meta name="csrf-token" content="([^"]+)">',
            page.text,
        )
        assert csrf_token is not None
        form = {"csrf_token": csrf_token.group(1)}

        missing_csrf = await client.post(
            "/setup",
            data={},
            headers={"origin": "https://poppy.example"},
        )
        cross_site = await client.post(
            "/setup",
            data=form,
            headers={"origin": "https://attacker.example"},
        )
        authorized = await client.post(
            "/setup",
            data=form,
            headers={"origin": "https://poppy.example"},
        )

    assert missing_csrf.status_code == 404
    assert cross_site.status_code == 404
    assert authorized.status_code == 405


@pytest.mark.asyncio
async def test_setup_form_stream_is_bounded_without_a_content_length(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))
    setup_url = setup_url_from(caplog)
    consumed_chunks = 0

    async def oversized_form() -> AsyncIterator[bytes]:
        nonlocal consumed_chunks
        for _ in range(100):
            consumed_chunks += 1
            yield b"padding=" + b"x" * 4_096

    async with setup_client(app) as client:
        await client.get(setup_url)
        await client.get("/setup")
        response = await client.post(
            "/setup",
            content=oversized_form(),
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "origin": "https://poppy.example",
            },
        )

    assert response.status_code == 404
    assert consumed_chunks <= 5


@pytest.mark.asyncio
async def test_missing_invalid_and_replayed_bootstrap_credentials_reveal_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))
    setup_url = setup_url_from(caplog)

    async with setup_client(app) as unauthenticated_client:
        missing = await unauthenticated_client.get("/setup")
        invalid = await unauthenticated_client.get("/setup?token=invalid")

    async with setup_client(app) as operator_client:
        await operator_client.get(setup_url)
        existing_session = await operator_client.get("/setup")
        replayed = await operator_client.get(setup_url)

    assert [missing.status_code, invalid.status_code, replayed.status_code] == [404, 404, 404]
    assert missing.json() == invalid.json() == replayed.json() == {"error": "not found"}
    assert all("set-cookie" not in response.headers for response in (missing, invalid, replayed))
    assert existing_session.status_code == 200


@pytest.mark.asyncio
async def test_expired_bootstrap_link_cannot_establish_a_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = MutableClock(2_000_000_000)
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(configured, clock=clock)
    setup_url = setup_url_from(caplog)
    clock.advance(24 * 60 * 60 + 1)

    async with setup_client(app) as client:
        response = await client.get(setup_url)

    assert response.status_code == 404
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_setup_mode_rejects_review_webhooks_as_unavailable(tmp_path: Path) -> None:
    app = create_service(setup_settings(tmp_path))

    async with setup_client(app) as client:
        response = await client.post(
            "/webhooks/github",
            content=b"{}",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "reviewer unavailable"}


@pytest.mark.asyncio
async def test_incomplete_sibling_write_keeps_the_last_valid_setup_state(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.INFO, logger="poppy.service")
    create_service(configured)
    setup_url = setup_url_from(caplog)
    incomplete_write = configured.data_dir / ".setup-state.json.incomplete"
    incomplete_write.write_text('{"version":', encoding="utf-8")
    incomplete_write.chmod(0o600)

    restarted_app = create_service(configured)
    async with setup_client(restarted_app) as client:
        recovered = await client.get(setup_url)

    assert recovered.status_code == 303


@pytest.mark.asyncio
async def test_setup_session_and_restrictive_state_survive_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.INFO, logger="poppy.service")
    first_app = create_service(configured)
    setup_url = setup_url_from(caplog)
    bootstrap_token = parse_qs(urlparse(setup_url).query)["token"][0]
    async with setup_client(first_app) as client:
        await client.get(setup_url)
        session_cookie = client.cookies.get("poppy_setup_session")
        activated_page = await client.get("/setup")

    state_file = configured.data_dir / "setup-state.json"
    assert activated_page.status_code == 200
    assert stat.S_IMODE(configured.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert bootstrap_token not in state_file.read_text(encoding="utf-8")
    assert session_cookie not in state_file.read_text(encoding="utf-8")

    caplog.clear()
    restarted_app = create_service(configured)
    assert not any(
        getattr(record, "event", None) == "setup_bootstrap_created" for record in caplog.records
    )
    async with setup_client(restarted_app) as client:
        client.cookies.set(
            "poppy_setup_session",
            session_cookie,
            domain="poppy.example",
            path="/setup",
        )
        page = await client.get("/setup")

    assert page.status_code == 200


@pytest.mark.parametrize(
    "persisted_state",
    [
        {
            "version": True,
            "bootstrap": None,
            "pending_session": None,
            "session": {"digest": "a" * 64, "expires_at": 2_000_000_000},
        },
        {
            "version": 1,
            "bootstrap": {"token": "a" * 43, "expires_at": 2_000_000_000},
            "pending_session": None,
            "session": {"digest": "b" * 64, "expires_at": 2_000_000_000},
        },
        {
            "version": 1,
            "bootstrap": None,
            "pending_session": None,
            "session": None,
        },
        {
            "version": 1,
            "bootstrap": None,
            "pending_session": None,
            "session": {"digest": "not-a-digest".ljust(64, "x"), "expires_at": 2_000_000_000},
        },
        {
            "version": 1,
            "bootstrap": {"token": "a" * 43, "expires_at": 2_000_000_000},
            "pending_session": {"token": "b" * 43, "expires_at": 2_000_000_001},
            "session": None,
        },
    ],
)
def test_malformed_or_impossible_persisted_state_fails_closed(
    tmp_path: Path,
    persisted_state: dict[str, object],
) -> None:
    configured = setup_settings(tmp_path)
    configured.data_dir.mkdir(mode=0o700)
    (configured.data_dir / "setup-state.json").write_text(json.dumps(persisted_state))

    with pytest.raises(RuntimeConfigurationError, match="setup state"):
        create_service(configured)


@dataclass
class MutableClock:
    current: float

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds
