import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from poppy.application.review_cycle import CodexPort, GitHubPort, ReviewCycle, ReviewRequest
from poppy.application.scheduler import InMemoryScheduler
from poppy.domain.review import Severity
from poppy.private_state import (
    PrivateStateFileNotRegularError,
    read_private_json,
    write_private_json_atomically,
)
from poppy.setup_session import SetupSessionStore, SetupStateError

READINESS_STATE_FILE = "readiness.json"


class RuntimeStateError(RuntimeError):
    """The persisted or active reviewer runtime cannot be used safely."""


@dataclass(frozen=True, slots=True)
class ReviewerConnection:
    app_id: int
    private_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReviewerAdapters:
    github: GitHubPort
    codex: CodexPort


class ReviewerAdapterFactory(Protocol):
    async def create(self, connection: ReviewerConnection) -> ReviewerAdapters: ...


class SchedulerPort(Protocol):
    async def schedule(self, request: ReviewRequest) -> bool: ...


async def _nothing_to_close() -> None:
    return None


@dataclass(frozen=True, slots=True)
class ActiveReviewer:
    webhook_secret: bytes = field(repr=False)
    scheduler: SchedulerPort
    close_scheduler: Callable[[], Awaitable[None]] = field(
        default=_nothing_to_close,
        repr=False,
    )

    async def close(self) -> None:
        await self.close_scheduler()


class ReadinessStore:
    def __init__(self, *, data_dir: Path) -> None:
        self._state_file = data_dir / READINESS_STATE_FILE

    def is_ready(self) -> bool:
        if self._state_file.is_symlink():
            raise RuntimeStateError("Poppy readiness state is not a regular file")
        if not self._state_file.exists():
            return False
        try:
            payload = read_private_json(self._state_file)
        except PrivateStateFileNotRegularError as error:
            raise RuntimeStateError("Poppy readiness state is not a regular file") from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeStateError("Poppy readiness state is not readable") from error
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "status"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
            or payload["status"] != "ready"
        ):
            raise RuntimeStateError("Poppy readiness state has an unsupported format")
        return True

    def mark_ready(self) -> None:
        try:
            write_private_json_atomically(
                self._state_file,
                {"version": 1, "status": "ready"},
            )
        except OSError as error:
            raise RuntimeStateError("Poppy readiness state cannot be persisted") from error


class InstanceRuntime:
    def __init__(
        self,
        *,
        readiness: ReadinessStore,
        setup_session: SetupSessionStore,
        load_connection: Callable[[], ReviewerConnection | None],
        adapter_factory: ReviewerAdapterFactory,
        blocking_severity: Severity,
        concurrency: int,
        initially_ready: bool,
    ) -> None:
        self._readiness = readiness
        self._setup_session = setup_session
        self._load_connection = load_connection
        self._adapter_factory = adapter_factory
        self._blocking_severity = blocking_severity
        self._concurrency = concurrency
        self._setup_closed = initially_ready
        self._active: ActiveReviewer | None = None
        self._promotion_lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        if self._active is not None:
            return "reviewer"
        return "unavailable" if self._setup_closed else "setup"

    @property
    def setup_closed(self) -> bool:
        return self._setup_closed

    def active_reviewer(self) -> ActiveReviewer | None:
        return self._active

    async def start(self) -> None:
        if self._setup_closed:
            async with self._promotion_lock:
                if self._active is not None:
                    return
                connection = self._read_connection()
                if connection is None:
                    raise RuntimeStateError("Ready Poppy credentials are incomplete")
                active = await self._build_reviewer(connection)
                try:
                    self._setup_session.close()
                except SetupStateError as error:
                    await active.close()
                    raise RuntimeStateError("Poppy Setup Session could not be closed") from error
                self._active = active
            return
        await self.promote_if_ready()

    async def promote_if_ready(self) -> bool:
        async with self._promotion_lock:
            if self._setup_closed:
                return self._active is not None
            connection = self._read_connection()
            if connection is None:
                return False

            active = await self._build_reviewer(connection)
            try:
                self._readiness.mark_ready()
                self._setup_closed = True
                self._setup_session.close()
            except (RuntimeStateError, SetupStateError) as error:
                await active.close()
                raise RuntimeStateError("Poppy promotion could not be persisted") from error
            self._active = active
            return True

    async def close(self) -> None:
        active, self._active = self._active, None
        if active is not None:
            await active.close()

    async def _build_reviewer(self, connection: ReviewerConnection) -> ActiveReviewer:
        try:
            adapters = await self._adapter_factory.create(connection)
            cycle = ReviewCycle(
                github=adapters.github,
                codex=adapters.codex,
                blocking_severity=self._blocking_severity,
            )
            scheduler = InMemoryScheduler(
                handler=cycle.run,
                concurrency=self._concurrency,
            )
        except Exception:
            raise RuntimeStateError("Poppy reviewer runtime could not be initialized") from None
        return ActiveReviewer(
            webhook_secret=connection.webhook_secret.encode(),
            scheduler=scheduler,
            close_scheduler=scheduler.close,
        )

    def _read_connection(self) -> ReviewerConnection | None:
        try:
            return self._load_connection()
        except SetupStateError as error:
            raise RuntimeStateError("Poppy integration state is not usable") from error
