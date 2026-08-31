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
from poppy.adapters.github_onboarding import GitHubAppCredentials, GitHubOnboarding
from poppy.application.review_cycle import ReviewCycle
from poppy.application.scheduler import InMemoryScheduler
from poppy.chatgpt_setup import ChatGPTSetup, ChatGPTSetupStore
from poppy.codex_runtime import build_codex_config, require_chatgpt_account
from poppy.config import CONTEXT_OVERHEAD_RESERVE, PoppySettings, read_github_private_key
from poppy.github_setup import GitHubSetupStore
from poppy.http import create_app, create_setup_app
from poppy.runtime import (
    InstanceRuntime,
    ReadinessStore,
    ReviewerAdapterFactory,
    ReviewerAdapters,
    ReviewerConnection,
    RuntimeStateError,
)
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
    reviewer_adapter_factory: ReviewerAdapterFactory | None = None,
) -> FastAPI:
    credentials = settings.reviewer_credentials()
    if credentials is None:
        return _create_onboarding_service(
            settings=settings,
            clock=clock,
            github_onboarding_client=github_onboarding_client,
            codex_onboarding=codex_onboarding,
            reviewer_adapter_factory=reviewer_adapter_factory,
        )

    codex_home, workspace = _prepare_directories(
        codex_home=settings.codex_home,
        workspace=settings.codex_workspace,
    )
    private_key = read_github_private_key(credentials.private_key_file)
    github_client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10))
    codex_client = AsyncCodex(build_codex_config(codex_home))
    adapters = _build_reviewer_adapters(
        settings=settings,
        app_id=credentials.app_id,
        private_key=private_key,
        github_client=github_client,
        codex_client=codex_client,
        workspace=workspace,
    )
    cycle = ReviewCycle(
        github=adapters.github,
        codex=adapters.codex,
        blocking_severity=settings.blocking_severity,
    )
    scheduler = InMemoryScheduler(
        handler=cycle.run,
        concurrency=settings.concurrency,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await require_chatgpt_account(codex_client)
            logger.info("Codex authenticated with ChatGPT")
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


class _ProductionReviewerAdapterFactory:
    def __init__(
        self,
        *,
        settings: PoppySettings,
        github_client: httpx.AsyncClient,
        codex_client: AsyncCodex,
    ) -> None:
        self._settings = settings
        self._github_client = github_client
        self._codex_client = codex_client
        self._github_onboarding = GitHubOnboarding(client=github_client)

    async def create(self, connection: ReviewerConnection) -> ReviewerAdapters:
        _, workspace = _prepare_directories(
            codex_home=self._settings.codex_home,
            workspace=self._settings.codex_workspace,
        )
        app = GitHubAppCredentials(
            app_id=connection.app_id,
            slug=connection.app_slug,
            private_key=connection.private_key,
            webhook_secret=connection.webhook_secret,
        )
        await self._github_onboarding.get_installation(
            app=app,
            installation_id=connection.installation_id,
        )
        await require_chatgpt_account(self._codex_client)
        logger.info("Codex authenticated with ChatGPT")
        return _build_reviewer_adapters(
            settings=self._settings,
            app_id=connection.app_id,
            private_key=connection.private_key,
            github_client=self._github_client,
            codex_client=self._codex_client,
            workspace=workspace,
        )


def _build_reviewer_adapters(
    *,
    settings: PoppySettings,
    app_id: int,
    private_key: str,
    github_client: httpx.AsyncClient,
    codex_client: AsyncCodex,
    workspace: Path,
) -> ReviewerAdapters:
    return ReviewerAdapters(
        github=GitHubAdapter(
            app_id=str(app_id),
            private_key=private_key,
            client=github_client,
            max_patch_characters=(settings.context_limit_characters - CONTEXT_OVERHEAD_RESERVE),
        ),
        codex=CodexAdapter(
            client=codex_client,
            workspace=workspace,
            model=settings.model,
            timeout_seconds=settings.review_timeout_seconds,
            max_input_characters=settings.context_limit_characters,
            language=settings.review_language,
            tone=settings.review_tone,
        ),
    )


def _create_onboarding_service(
    *,
    settings: PoppySettings,
    clock: Callable[[], float],
    github_onboarding_client: httpx.AsyncClient | None,
    codex_onboarding: CodexOnboardingPort | None,
    reviewer_adapter_factory: ReviewerAdapterFactory | None,
) -> FastAPI:
    try:
        data_dir = _prepare_private_directory(settings.data_dir.expanduser().resolve())
        readiness = ReadinessStore(data_dir=data_dir)
        initially_ready = readiness.is_ready()
        setup_session = SetupSessionStore(
            data_dir=data_dir,
            bootstrap_ttl_seconds=BOOTSTRAP_TTL_SECONDS,
            session_ttl_seconds=SESSION_TTL_SECONDS,
            clock=clock,
        )
        github_setup = GitHubSetupStore(data_dir=data_dir)
        chatgpt_store = ChatGPTSetupStore(data_dir=data_dir)
        if initially_ready:
            codex_home = settings.codex_home.expanduser().resolve()
            bootstrap_token = None
        else:
            github_setup.initialize()
            chatgpt_store.initialize()
            codex_home = _prepare_private_directory(settings.codex_home.expanduser().resolve())
            bootstrap_token = setup_session.initialize()
    except (OSError, RuntimeStateError, SetupStateError) as error:
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
    needs_codex_client = codex_onboarding is None or reviewer_adapter_factory is None
    setup_codex_client = AsyncCodex(build_codex_config(codex_home)) if needs_codex_client else None
    setup_codex = (
        CodexOnboarding(client=setup_codex_client)
        if codex_onboarding is None and setup_codex_client is not None
        else codex_onboarding
    )
    assert setup_codex is not None

    if reviewer_adapter_factory is None:
        assert setup_codex_client is not None
        active_adapter_factory: ReviewerAdapterFactory = _ProductionReviewerAdapterFactory(
            settings=settings,
            github_client=setup_github_client,
            codex_client=setup_codex_client,
        )
    else:
        active_adapter_factory = reviewer_adapter_factory

    runtime = InstanceRuntime(
        readiness=readiness,
        setup_session=setup_session,
        load_connection=lambda: _load_setup_connection(github_setup, chatgpt_store),
        adapter_factory=active_adapter_factory,
        blocking_severity=settings.blocking_severity,
        concurrency=settings.concurrency,
        initially_ready=initially_ready,
    )

    async def promote_after_chatgpt() -> None:
        try:
            await runtime.promote_if_ready()
        except RuntimeStateError:
            logger.warning(
                "Poppy reviewer promotion failed",
                extra={"event": "reviewer_promotion_failed"},
            )

    chatgpt_setup = ChatGPTSetup(
        store=chatgpt_store,
        codex=setup_codex,
        on_connected=promote_after_chatgpt,
    )

    @asynccontextmanager
    async def setup_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            if not runtime.setup_closed:
                try:
                    await chatgpt_setup.verify_persisted_connection()
                except CodexOnboardingError:
                    logger.warning(
                        "Persisted ChatGPT authorization could not be verified",
                        extra={"event": "chatgpt_persisted_authorization_failed"},
                    )
            try:
                await runtime.start()
            except RuntimeStateError:
                if runtime.setup_closed:
                    logger.error(
                        "Poppy reviewer state is unavailable",
                        extra={"event": "reviewer_state_unavailable"},
                    )
                else:
                    logger.warning(
                        "Poppy reviewer promotion could not resume",
                        extra={"event": "reviewer_promotion_resume_failed"},
                    )
            yield
        finally:
            await chatgpt_setup.close()
            await runtime.close()
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
        reviewer_runtime=runtime,
        lifespan=setup_lifespan,
    )


def _load_setup_connection(
    github_setup: GitHubSetupStore,
    chatgpt_store: ChatGPTSetupStore,
) -> ReviewerConnection | None:
    if chatgpt_store.view().status != "connected":
        return None
    connected = github_setup.connected_installation()
    if connected is None:
        return None
    return ReviewerConnection(
        app_id=connected.app.app_id,
        app_slug=connected.app.slug,
        installation_id=connected.installation.installation_id,
        private_key=connected.app.private_key,
        webhook_secret=connected.app.webhook_secret,
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
