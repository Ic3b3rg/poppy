from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import AsyncClient as ASGIClient
from pydantic import SecretStr

import poppy.service as service_module
from poppy.config import PoppySettings
from poppy.service import RuntimeConfigurationError, create_service


@pytest.mark.asyncio
async def test_service_starts_authenticated_codex_and_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_client = MagicMock()
    codex_client.close = AsyncMock()
    github_client = MagicMock(spec=httpx.AsyncClient)
    github_client.aclose = AsyncMock()
    require_account = AsyncMock(return_value="plus")
    monkeypatch.setattr(service_module, "AsyncCodex", MagicMock(return_value=codex_client))
    monkeypatch.setattr(
        service_module.httpx,
        "AsyncClient",
        MagicMock(return_value=github_client),
    )
    monkeypatch.setattr(service_module, "require_chatgpt_account", require_account)

    app = create_service(settings(tmp_path))

    async with app.router.lifespan_context(app):
        require_account.assert_awaited_once_with(codex_client)
        transport = httpx.ASGITransport(app=app)
        async with ASGIClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/healthz")

    assert health.json() == {"status": "ok", "mode": "reviewer"}
    codex_client.close.assert_awaited_once()
    github_client.aclose.assert_awaited_once()


def test_service_refuses_nonempty_codex_workspace(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    configured.codex_workspace.mkdir(parents=True)
    (configured.codex_workspace / "unexpected").write_text("data")

    with pytest.raises(RuntimeConfigurationError, match="empty"):
        create_service(configured)


def settings(tmp_path: Path) -> PoppySettings:
    key_file = tmp_path / "github-app.pem"
    key_file.write_text("test-private-key")
    return PoppySettings(
        github_app_id=123,
        github_private_key_file=key_file,
        github_webhook_secret=SecretStr("a-secret-that-is-long-enough"),
        codex_home=tmp_path / "codex-home",
        codex_workspace=tmp_path / "empty-workspace",
        _env_file=None,
    )
