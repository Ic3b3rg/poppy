import argparse
import asyncio
from collections.abc import Sequence

import uvicorn
from openai_codex import AsyncCodex

from poppy.codex_runtime import build_codex_config, login_with_device_code
from poppy.config import CodexHomeSettings, PoppySettings
from poppy.service import RuntimeConfigurationError, create_service


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="poppy",
        description="Personal automatic GitHub pull-request reviewer.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Authenticate the private Codex runtime with ChatGPT.")
    commands.add_parser("serve", help="Run the GitHub webhook service.")
    args = parser.parse_args(argv)

    if args.command == "login":
        asyncio.run(_login())
        return
    if args.command == "serve":
        settings = PoppySettings()  # type: ignore[call-arg]
        app = create_service(settings)
        uvicorn.run(
            app,
            host=settings.listen_host,
            port=settings.listen_port,
            log_level=settings.log_level,
            workers=1,
        )
        return
    raise RuntimeError("unreachable command")


async def _login() -> None:
    settings = CodexHomeSettings()
    codex_home = settings.codex_home.expanduser().resolve()
    try:
        codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        codex_home.chmod(0o700)
    except OSError as error:
        raise RuntimeConfigurationError("Codex home is not usable") from error

    async with AsyncCodex(build_codex_config(codex_home)) as client:
        await login_with_device_code(client)
