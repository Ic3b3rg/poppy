import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from openai_codex import AsyncCodex

from poppy.adapters.codex import CodexAdapter
from poppy.adapters.github import GitHubAdapter
from poppy.application.review_cycle import ReviewCycle
from poppy.application.scheduler import InMemoryScheduler
from poppy.codex_runtime import build_codex_config, require_chatgpt_account
from poppy.config import CONTEXT_OVERHEAD_RESERVE, PoppySettings, read_github_private_key
from poppy.http import create_app

logger = logging.getLogger(__name__)


class RuntimeConfigurationError(RuntimeError):
    """The local runtime cannot enforce Poppy's isolation settings."""


def create_service(settings: PoppySettings) -> FastAPI:
    codex_home, workspace = _prepare_directories(
        codex_home=settings.codex_home,
        workspace=settings.codex_workspace,
    )
    private_key = read_github_private_key(settings.github_private_key_file)
    github_client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    codex_client = AsyncCodex(build_codex_config(codex_home))
    github = GitHubAdapter(
        app_id=str(settings.github_app_id),
        private_key=private_key,
        client=github_client,
        max_patch_characters=(settings.context_limit_characters - CONTEXT_OVERHEAD_RESERVE),
    )
    codex = CodexAdapter(
        client=codex_client,
        workspace=workspace,
        model=settings.model,
        timeout_seconds=settings.review_timeout_seconds,
        max_input_characters=settings.context_limit_characters,
        language=settings.review_language,
        tone=settings.review_tone,
    )
    cycle = ReviewCycle(
        github=github,
        codex=codex,
        blocking_severity=settings.blocking_severity,
    )
    scheduler = InMemoryScheduler(
        handler=cycle.run,
        concurrency=settings.concurrency,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            plan = await require_chatgpt_account(codex_client)
            logger.info("Codex authenticated with ChatGPT", extra={"chatgpt_plan": plan})
            yield
        finally:
            await scheduler.close()
            await codex_client.close()
            await github_client.aclose()

    return create_app(
        webhook_secret=settings.github_webhook_secret.get_secret_value().encode(),
        scheduler=scheduler,
        lifespan=lifespan,
    )


def _prepare_directories(*, codex_home: Path, workspace: Path) -> tuple[Path, Path]:
    resolved_home = codex_home.expanduser().resolve()
    resolved_workspace = workspace.expanduser().resolve()
    if (
        resolved_home == resolved_workspace
        or resolved_home.is_relative_to(resolved_workspace)
        or resolved_workspace.is_relative_to(resolved_home)
    ):
        raise RuntimeConfigurationError("Codex home and review workspace must be separate")

    try:
        resolved_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_home.chmod(0o700)
        resolved_workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved_workspace.chmod(0o700)
        if any(resolved_workspace.iterdir()):
            raise RuntimeConfigurationError("Codex review workspace must remain empty")
    except OSError as error:
        raise RuntimeConfigurationError("Poppy runtime directories are not usable") from error
    return resolved_home, resolved_workspace
