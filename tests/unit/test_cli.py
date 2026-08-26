from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import poppy.cli as cli_module


def test_login_command_uses_dedicated_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex"
    session = MagicMock()
    client_context = MagicMock()
    client_context.__aenter__ = AsyncMock(return_value=session)
    client_context.__aexit__ = AsyncMock(return_value=None)
    client_factory = MagicMock(return_value=client_context)
    login = AsyncMock()
    monkeypatch.setenv("POPPY_CODEX_HOME", str(codex_home))
    monkeypatch.setattr(cli_module, "AsyncCodex", client_factory)
    monkeypatch.setattr(cli_module, "login_with_device_code", login)

    cli_module.main(["login"])

    assert codex_home.is_dir()
    login.assert_awaited_once_with(session)
    config = client_factory.call_args.args[0]
    assert config.env["CODEX_HOME"] == str(codex_home)
    assert config.env["OPENAI_API_KEY"] == ""


def test_serve_command_runs_single_service(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        listen_host="127.0.0.1",
        listen_port=8_123,
        log_level="warning",
    )
    app = object()
    monkeypatch.setattr(cli_module, "PoppySettings", MagicMock(return_value=settings))
    create_service = MagicMock(return_value=app)
    monkeypatch.setattr(cli_module, "create_service", create_service)
    run = MagicMock()
    monkeypatch.setattr(cli_module.uvicorn, "run", run)

    cli_module.main(["serve"])

    create_service.assert_called_once_with(settings)
    run.assert_called_once_with(
        app,
        host="127.0.0.1",
        port=8_123,
        log_level="warning",
        workers=1,
    )
