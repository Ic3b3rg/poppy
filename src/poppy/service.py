import logging
import stat
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from openai_codex import AsyncCodex

from poppy.adapters.codex import CodexAdapter
from poppy.adapters.codex_onboarding import (
    CodexOnboarding,
    CodexOnboardingError,
    CodexOnboardingPort,
)
from poppy.adapters.github import GitHubAdapter
from poppy.adapters.github_onboarding import GitHubOnboarding
from poppy.application.review_cycle import ReviewCycle
from poppy.application.scheduler import InMemoryScheduler
from poppy.chatgpt_setup import ChatGPTSetup, ChatGPTSetupStore
from poppy.codex_runtime import build_codex_config, require_chatgpt_account
from poppy.config import CONTEXT_OVERHEAD_RESERVE, PoppySettings, read_github_private_key
from poppy.github_setup import GitHubSetupStore
from poppy.http import create_app, create_setup_app
from poppy.setup_session import (
    BOOTSTRAP_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    SetupSessionStore,
    SetupStateError,
)

logger = logging.getLogger(__name__)


class RuntimeConfigurationError(RuntimeError):
    """The local runtime cannot enforce Poppy's isolation settings."""


def create_service(
    settings: PoppySettings,
    *,
    clock: Callable[[], float] = time.time,
    github_onboarding_client: httpx.AsyncClient | None = None,
    codex_onboarding: CodexOnboardingPort | None = None,
) -> FastAPI:
    credentials = settings.reviewer_credentials()
    if credentials is None:
        try:
            data_dir = _prepare_private_directory(settings.data_dir.expanduser().resolve())
            setup_session = SetupSessionStore(
                data_dir=data_dir,
                bootstrap_ttl_seconds=BOOTSTRAP_TTL_SECONDS,
                session_ttl_seconds=SESSION_TTL_SECONDS,
                clock=clock,
            )
            github_setup = GitHubSetupStore(data_dir=data_dir)
            github_setup.initialize()
            chatgpt_store = ChatGPTSetupStore(data_dir=data_dir)
            chatgpt_store.initialize()
            codex_home = _prepare_private_directory(settings.codex_home.expanduser().resolve())
            bootstrap_token = setup_session.initialize()
        except (OSError, SetupStateError) as error:
            raise RuntimeConfigurationError("Poppy setup state is not usable") from error
        public_origin = str(settings.public_base_url).rstrip("/")
        if bootstrap_token is not None:
            setup_url = f"{public_origin}/setup?token={bootstrap_token}"
            logger.warning(
                "Poppy Setup Session: %s",
                setup_url,
                extra={"event": "setup_bootstrap_created", "setup_url": setup_url},
            )
        setup_github_client = github_onboarding_client or httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(30, connect=10),
        )
        github_onboarding = GitHubOnboarding(client=setup_github_client, clock=clock)
        setup_codex_client = (
            AsyncCodex(build_codex_config(codex_home)) if codex_onboarding is None else None
        )
        setup_codex = (
            CodexOnboarding(client=setup_codex_client)
            if setup_codex_client is not None
            else codex_onboarding
        )
        assert setup_codex is not None
        chatgpt_setup = ChatGPTSetup(store=chatgpt_store, codex=setup_codex)

        @asynccontextmanager
        async def setup_lifespan(_app: FastAPI) -> AsyncIterator[None]:
            try:
                try:
                    await chatgpt_setup.verify_persisted_connection()
                except CodexOnboardingError:
                    logger.warning(
                        "Persisted ChatGPT authorization could not be verified",
                        extra={"event": "chatgpt_persisted_authorization_failed"},
                    )
                yield
            finally:
                await chatgpt_setup.close()
                if setup_codex_client is not None:
                    await setup_codex_client.close()
                if github_onboarding_client is None:
                    await setup_github_client.aclose()

        return create_setup_app(
            setup_session=setup_session,
            github_setup=github_setup,
            github_onboarding=github_onboarding,
            chatgpt_setup=chatgpt_setup,
            public_base_url=settings.public_base_url,
            lifespan=setup_lifespan,
        )

    codex_home, workspace = _prepare_directories(
        codex_home=settings.codex_home,
        workspace=settings.codex_workspace,
    )
    private_key = read_github_private_key(credentials.private_key_file)
    github_client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    codex_client = AsyncCodex(build_codex_config(codex_home))
    github = GitHubAdapter(
        app_id=str(credentials.app_id),
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
        webhook_secret=credentials.webhook_secret.get_secret_value().encode(),
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
        _prepare_private_directory(resolved_home)
        _prepare_private_directory(resolved_workspace)
        if any(resolved_workspace.iterdir()):
            raise RuntimeConfigurationError("Codex review workspace must remain empty")
    except OSError as error:
        raise RuntimeConfigurationError("Poppy runtime directories are not usable") from error
    return resolved_home, resolved_workspace


def _prepare_private_directory(path: Path) -> Path:
    if path.exists() and not path.is_dir():
        raise RuntimeConfigurationError("Poppy runtime path is not a directory")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        path.chmod(0o700)
    return path
