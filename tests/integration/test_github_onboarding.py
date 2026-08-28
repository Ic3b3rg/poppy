import html
import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
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


def setup_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://poppy.example",
        follow_redirects=False,
    )
