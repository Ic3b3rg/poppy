import asyncio
import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import AsyncClient as ASGIClient

from poppy.config import PoppySettings


def setup_settings(tmp_path: Path) -> PoppySettings:
    return PoppySettings(
        public_base_url="https://poppy.example",
        data_dir=tmp_path / "data",
        codex_home=tmp_path / "data" / "codex",
        codex_workspace=tmp_path / "workspace",
        _env_file=None,
    )


def setup_url_from(caplog: pytest.LogCaptureFixture) -> str:
    match = re.search(r"Poppy Setup Session: (https://\S+)", caplog.text)
    assert match is not None
    return match.group(1)


def setup_client(app: FastAPI) -> httpx.AsyncClient:
    return ASGIClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://poppy.example",
        follow_redirects=False,
    )


async def establish_setup_session(
    client: httpx.AsyncClient,
    setup_source: str | pytest.LogCaptureFixture,
) -> str:
    setup_url = setup_source if isinstance(setup_source, str) else setup_url_from(setup_source)
    exchange = await client.get(setup_url)
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


def github_installation_payload() -> dict[str, object]:
    return {
        "id": 456,
        "app_id": 123,
        "app_slug": "poppy-example",
        "account": {"id": 789, "login": "operator", "type": "User"},
        "target_type": "User",
        "repository_selection": "selected",
        "suspended_at": None,
    }


async def wait_for_mode(
    client: httpx.AsyncClient,
    mode: str,
    *,
    attempts: int = 50,
) -> httpx.Response:
    for _ in range(attempts):
        response = await client.get("/healthz")
        if response.json().get("mode") == mode:
            return response
        await asyncio.sleep(0)
    raise AssertionError(f"Poppy did not enter {mode!r} mode")


async def wait_for_setup_text(
    client: httpx.AsyncClient,
    expected: str,
    *,
    attempts: int = 50,
) -> httpx.Response:
    for _ in range(attempts):
        response = await client.get("/setup")
        if expected in response.text:
            return response
        await asyncio.sleep(0)
    raise AssertionError(f"setup page did not contain {expected!r}")
