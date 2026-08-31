import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import poppy.runtime as runtime_module
from poppy.application.scheduler import InMemoryScheduler
from poppy.domain.review import Severity
from poppy.runtime import (
    InstanceRuntime,
    ReadinessStore,
    ReviewerAdapters,
    ReviewerConnection,
)
from poppy.setup_session import SetupSessionStore


class SignallingAdapterFactory:
    def __init__(self) -> None:
        self.created = asyncio.Event()

    async def create(self, connection: ReviewerConnection) -> ReviewerAdapters:
        self.created.set()
        return ReviewerAdapters(github=MagicMock(), codex=MagicMock())


class BlockingPromotionExit:
    def __init__(self) -> None:
        self.exit_started = asyncio.Event()
        self._release = asyncio.Event()

    @asynccontextmanager
    async def promotion(self) -> AsyncIterator[None]:
        try:
            yield
        finally:
            self.exit_started.set()
            await self._release.wait()


@pytest.mark.asyncio
async def test_cancellation_before_readiness_closes_the_unpublished_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = SignallingAdapterFactory()
    runtime, readiness, _setup_session = create_runtime(tmp_path, factory=factory)
    closed_schedulers = record_scheduler_closes(monkeypatch)

    async with runtime.setup_access() as setup_available:
        assert setup_available
        promotion = asyncio.create_task(runtime.promote_if_ready())
        await factory.created.wait()
        promotion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await promotion

    assert not readiness.is_ready()
    assert runtime.active_reviewer() is None
    assert len(closed_schedulers) == 1
    async with runtime.setup_access() as setup_available:
        assert setup_available


@pytest.mark.asyncio
async def test_cancellation_after_readiness_keeps_the_published_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = SignallingAdapterFactory()
    runtime, readiness, _setup_session = create_runtime(tmp_path, factory=factory)
    closed_schedulers = record_scheduler_closes(monkeypatch)
    blocking_exit = BlockingPromotionExit()
    monkeypatch.setattr(runtime, "_setup_gate", blocking_exit)

    promotion = asyncio.create_task(runtime.promote_if_ready())
    await blocking_exit.exit_started.wait()
    promotion.cancel()
    with pytest.raises(asyncio.CancelledError):
        await promotion

    assert readiness.is_ready()
    assert runtime.mode == "reviewer"
    assert runtime.active_reviewer() is not None
    assert not (tmp_path / "data" / "setup-state.json").exists()
    assert closed_schedulers == []
    assert runtime.setup_closed

    await runtime.close()
    assert len(closed_schedulers) == 1


def create_runtime(
    tmp_path: Path,
    *,
    factory: SignallingAdapterFactory,
) -> tuple[InstanceRuntime, ReadinessStore, SetupSessionStore]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    readiness = ReadinessStore(data_dir=data_dir)
    setup_session = SetupSessionStore(
        data_dir=data_dir,
        bootstrap_ttl_seconds=3_600,
        session_ttl_seconds=3_600,
        clock=lambda: 0,
    )
    setup_session.initialize()
    runtime = InstanceRuntime(
        readiness=readiness,
        setup_session=setup_session,
        load_connection=lambda: ReviewerConnection(
            app_id=123,
            app_slug="poppy-example",
            installation_id=456,
            private_key="unused-by-fake-adapter",
            webhook_secret="synthetic-webhook-secret",
        ),
        adapter_factory=factory,
        blocking_severity=Severity.HIGH,
        concurrency=1,
        initially_ready=False,
    )
    return runtime, readiness, setup_session


def record_scheduler_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> list[InMemoryScheduler]:
    closed_schedulers: list[InMemoryScheduler] = []
    original_close = InMemoryScheduler.close

    async def close_and_record(scheduler: InMemoryScheduler) -> None:
        closed_schedulers.append(scheduler)
        await original_close(scheduler)

    monkeypatch.setattr(runtime_module.InMemoryScheduler, "close", close_and_record)
    return closed_schedulers
