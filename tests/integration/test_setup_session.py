import logging
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import poppy.setup_session as setup_session_module
from poppy.config import PoppySettings
from poppy.service import create_service


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

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://poppy.example",
        ) as client:
            response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "setup"}


@pytest.mark.asyncio
async def test_first_boot_link_establishes_a_secure_one_time_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))
    setup_url = next(
        record.setup_url
        for record in caplog.records
        if getattr(record, "event", None) == "setup_bootstrap_created"
    )
    parsed_url = urlparse(setup_url)
    token = parse_qs(parsed_url.query)["token"][0]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as client:
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


@pytest.mark.asyncio
async def test_setup_session_renders_minimal_server_page_without_the_bootstrap_token(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))
    setup_url = next(
        record.setup_url
        for record in caplog.records
        if getattr(record, "event", None) == "setup_bootstrap_created"
    )
    token = parse_qs(urlparse(setup_url).query)["token"][0]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as client:
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
    configured.setup_session_ttl_seconds = 300
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(configured, clock=clock)
    setup_url = next(
        record.setup_url
        for record in caplog.records
        if getattr(record, "event", None) == "setup_bootstrap_created"
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as client:
        await client.get(setup_url)
        clock.advance(301)
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
    setup_url = next(
        record.setup_url
        for record in caplog.records
        if getattr(record, "event", None) == "setup_bootstrap_created"
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as client:
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
async def test_missing_invalid_and_replayed_bootstrap_credentials_reveal_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))
    setup_url = next(
        record.setup_url
        for record in caplog.records
        if getattr(record, "event", None) == "setup_bootstrap_created"
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as unauthenticated_client:
        missing = await unauthenticated_client.get("/setup")
        invalid = await unauthenticated_client.get("/setup?token=invalid")

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as operator_client:
        await operator_client.get(setup_url)
        replayed = await operator_client.get(setup_url)
        existing_session = await operator_client.get("/setup")

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
    configured.setup_bootstrap_ttl_seconds = 60
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(configured, clock=clock)
    setup_url = next(
        record.setup_url
        for record in caplog.records
        if getattr(record, "event", None) == "setup_bootstrap_created"
    )
    clock.advance(61)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get(setup_url)

    assert response.status_code == 404
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_setup_mode_rejects_review_webhooks_as_unavailable(tmp_path: Path) -> None:
    app = create_service(setup_settings(tmp_path))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://poppy.example") as client:
        response = await client.post(
            "/webhooks/github",
            content=b"{}",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "reviewer unavailable"}


@pytest.mark.asyncio
async def test_interrupted_session_exchange_keeps_the_bootstrap_link_recoverable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.INFO, logger="poppy.service")
    app = create_service(configured)
    setup_url = next(
        record.setup_url
        for record in caplog.records
        if getattr(record, "event", None) == "setup_bootstrap_created"
    )
    replace = setup_session_module.os.replace

    def interrupt_replace(_source: object, _destination: object) -> None:
        raise OSError("simulated interruption")

    monkeypatch.setattr(setup_session_module.os, "replace", interrupt_replace)
    interrupted_transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=interrupted_transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as client:
        interrupted = await client.get(setup_url)

    monkeypatch.setattr(setup_session_module.os, "replace", replace)
    restarted_app = create_service(configured)
    restarted_transport = httpx.ASGITransport(app=restarted_app)
    async with httpx.AsyncClient(
        transport=restarted_transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as client:
        recovered = await client.get(setup_url)

    assert interrupted.status_code == 503
    assert interrupted.json() == {"error": "setup unavailable"}
    assert setup_url not in interrupted.text
    assert recovered.status_code == 303


@pytest.mark.asyncio
async def test_setup_session_and_restrictive_state_survive_restart(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configured = setup_settings(tmp_path)
    caplog.set_level(logging.INFO, logger="poppy.service")
    first_app = create_service(configured)
    setup_url = next(
        record.setup_url
        for record in caplog.records
        if getattr(record, "event", None) == "setup_bootstrap_created"
    )
    bootstrap_token = parse_qs(urlparse(setup_url).query)["token"][0]
    first_transport = httpx.ASGITransport(app=first_app)
    async with httpx.AsyncClient(
        transport=first_transport,
        base_url="https://poppy.example",
        follow_redirects=False,
    ) as client:
        await client.get(setup_url)
        session_cookie = client.cookies.get("poppy_setup_session")

    state_file = configured.data_dir / "setup-state.json"
    assert stat.S_IMODE(configured.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
    assert bootstrap_token not in state_file.read_text(encoding="utf-8")
    assert session_cookie not in state_file.read_text(encoding="utf-8")

    caplog.clear()
    restarted_app = create_service(configured)
    assert not any(
        getattr(record, "event", None) == "setup_bootstrap_created" for record in caplog.records
    )
    restarted_transport = httpx.ASGITransport(app=restarted_app)
    async with httpx.AsyncClient(
        transport=restarted_transport,
        base_url="https://poppy.example",
    ) as client:
        client.cookies.set(
            "poppy_setup_session",
            session_cookie,
            domain="poppy.example",
            path="/setup",
        )
        page = await client.get("/setup")

    assert page.status_code == 200


def setup_settings(tmp_path: Path) -> PoppySettings:
    return PoppySettings(
        public_base_url="https://poppy.example",
        data_dir=tmp_path / "data",
        codex_home=tmp_path / "data" / "codex",
        codex_workspace=tmp_path / "workspace",
        _env_file=None,
    )


@dataclass
class MutableClock:
    current: float

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds
