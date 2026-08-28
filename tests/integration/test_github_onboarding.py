import html
import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI

from poppy.config import PoppySettings
from poppy.service import create_service


@pytest.mark.asyncio
async def test_connect_github_starts_the_exact_private_app_manifest(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(setup_settings(tmp_path))

    async with setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        response = await client.post(
            "/setup/github/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )

    assert response.status_code == 200
    action = re.search(r'<form action="([^"]+)" method="post">', response.text)
    manifest_input = re.search(
        r'<input type="hidden" name="manifest" value="([^"]+)">',
        response.text,
    )
    assert action is not None
    assert manifest_input is not None
    manifest_url = urlparse(html.unescape(action.group(1)))
    manifest = json.loads(html.unescape(manifest_input.group(1)))

    assert manifest_url.scheme == "https"
    assert manifest_url.netloc == "github.com"
    assert manifest_url.path == "/settings/apps/new"
    assert len(parse_qs(manifest_url.query)["state"][0]) >= 43
    assert manifest == {
        "name": "Poppy",
        "url": "https://poppy.example",
        "hook_attributes": {
            "url": "https://poppy.example/webhooks/github",
            "active": True,
        },
        "redirect_url": "https://poppy.example/setup/github/manifest/callback",
        "setup_url": "https://poppy.example/setup/github/installation",
        "public": False,
        "default_permissions": {
            "checks": "write",
            "pull_requests": "write",
            "issues": "write",
            "metadata": "read",
        },
        "default_events": ["pull_request"],
    }
    assert "https://github.com" in response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_manifest_callback_exchanges_once_and_redirects_without_exposing_credentials(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests: list[httpx.Request] = []
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"

    async def github(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/app-manifests/manifest-code/conversions"
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        return httpx.Response(
            201,
            json={
                "id": 123,
                "slug": "poppy-example",
                "pem": private_key,
                "webhook_secret": webhook_secret,
            },
        )

    provider_client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(github),
    )
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        setup_settings(tmp_path),
        github_onboarding_client=provider_client,
    )

    async with provider_client, setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        start = await client.post(
            "/setup/github/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        state = manifest_state_from(start)
        callback = await client.get(
            "/setup/github/manifest/callback",
            params={"code": "manifest-code", "state": state},
        )
        replay = await client.get(
            "/setup/github/manifest/callback",
            params={"code": "manifest-code", "state": state},
        )

    assert callback.status_code == 303
    installation_url = urlparse(callback.headers["location"])
    assert installation_url.scheme == "https"
    assert installation_url.netloc == "github.com"
    assert installation_url.path == "/apps/poppy-example/installations/new"
    assert len(parse_qs(installation_url.query)["state"][0]) >= 43
    assert replay.status_code == 404
    assert len(requests) == 1
    assert private_key not in callback.text
    assert webhook_secret not in callback.text

    state_file = tmp_path / "data" / "github-setup.json"
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_file.stat().st_mode & 0o777 == 0o600
    assert persisted["app"] == {
        "id": 123,
        "slug": "poppy-example",
        "private_key": private_key,
        "webhook_secret": webhook_secret,
    }


@pytest.mark.asyncio
async def test_manifest_callback_rejects_missing_invalid_and_ambiguous_correlation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def unexpected_github_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected GitHub request: {request.method} {request.url}")

    provider_client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(unexpected_github_request),
    )
    caplog.set_level(logging.WARNING, logger="poppy.service")
    app = create_service(
        setup_settings(tmp_path),
        github_onboarding_client=provider_client,
    )

    async with provider_client, setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        start = await client.post(
            "/setup/github/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        state = manifest_state_from(start)
        missing_code = await client.get(
            "/setup/github/manifest/callback",
            params={"state": state},
        )
        missing_state = await client.get(
            "/setup/github/manifest/callback",
            params={"code": "manifest-code"},
        )
        invalid_state = await client.get(
            "/setup/github/manifest/callback",
            params={"code": "manifest-code", "state": "invalid"},
        )
        ambiguous_state = await client.get(
            "/setup/github/manifest/callback",
            params=[
                ("code", "manifest-code"),
                ("state", "invalid"),
                ("state", state),
            ],
        )

    assert [
        missing_code.status_code,
        missing_state.status_code,
        invalid_state.status_code,
        ambiguous_state.status_code,
    ] == [404, 404, 404, 404]


@pytest.mark.asyncio
async def test_malformed_manifest_exchange_is_sanitized_and_retryable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_key = github_private_key()
    webhook_secret = "generated-webhook-secret"

    async def github(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "id": True,
                "slug": "poppy-example",
                "pem": private_key,
                "webhook_secret": webhook_secret,
            },
        )

    provider_client = httpx.AsyncClient(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(github),
    )
    caplog.set_level(logging.WARNING)
    app = create_service(
        setup_settings(tmp_path),
        github_onboarding_client=provider_client,
    )

    async with provider_client, setup_client(app) as client:
        csrf_token = await establish_setup_session(client, caplog)
        first_start = await client.post(
            "/setup/github/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )
        first_state = manifest_state_from(first_start)
        callback = await client.get(
            "/setup/github/manifest/callback",
            params={"code": "one-time-code", "state": first_state},
        )
        retry = await client.post(
            "/setup/github/connect",
            data={"csrf_token": csrf_token},
            headers={"origin": "https://poppy.example"},
        )

    assert callback.status_code == 502
    assert callback.json() == {"error": "GitHub App registration failed; return to setup and retry"}
    assert retry.status_code == 200
    assert manifest_state_from(retry) != first_state
    persisted = json.loads((tmp_path / "data" / "github-setup.json").read_text(encoding="utf-8"))
    assert persisted["phase"] == "manifest"
    assert "app" not in persisted
    assert private_key not in callback.text
    assert webhook_secret not in callback.text
    assert "one-time-code" not in caplog.text
    assert private_key not in caplog.text
    assert webhook_secret not in caplog.text


def setup_settings(tmp_path: Path) -> PoppySettings:
    return PoppySettings(
        public_base_url="https://poppy.example",
        data_dir=tmp_path / "data",
        codex_home=tmp_path / "data" / "codex",
        codex_workspace=tmp_path / "workspace",
        _env_file=None,
    )


async def establish_setup_session(
    client: httpx.AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> str:
    match = re.search(r"Poppy Setup Session: (https://\S+)", caplog.text)
    assert match is not None
    exchange = await client.get(match.group(1))
    page = await client.get(exchange.headers["location"])
    csrf_token = re.search(r'<meta name="csrf-token" content="([^"]+)">', page.text)
    assert csrf_token is not None
    return csrf_token.group(1)


def manifest_state_from(response: httpx.Response) -> str:
    action = re.search(r'<form action="([^"]+)" method="post">', response.text)
    assert action is not None
    return parse_qs(urlparse(html.unescape(action.group(1))).query)["state"][0]


def github_private_key() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def setup_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://poppy.example",
        follow_redirects=False,
    )
