import asyncio
import json
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Literal

from poppy.adapters.codex_onboarding import (
    CodexDeviceAuthorization,
    CodexOnboardingError,
    CodexOnboardingPort,
)
from poppy.private_state import (
    PrivateStateFileNotRegularError,
    read_private_json,
    write_private_json_atomically,
)
from poppy.setup_session import SetupStateError

CHATGPT_SETUP_STATE_FILE = "chatgpt-setup.json"
type ChatGPTSettledStatus = Literal[
    "disconnected",
    "connected",
    "failed",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class ChatGPTSettledView:
    status: ChatGPTSettledStatus


@dataclass(frozen=True, slots=True)
class ChatGPTPendingView:
    authorization: CodexDeviceAuthorization
    status: Literal["pending"] = field(default="pending", init=False)


type ChatGPTSetupView = ChatGPTSettledView | ChatGPTPendingView


class ChatGPTAuthorizationStart(Enum):
    STARTED = auto()
    ALREADY_ACTIVE = auto()
    FAILED = auto()


@dataclass(slots=True)
class _CancellationInProgress:
    login_id: str
    completion_failed: bool = False


class ChatGPTSetupStore:
    def __init__(self, *, data_dir: Path) -> None:
        self._state_file = data_dir / CHATGPT_SETUP_STATE_FILE

    def initialize(self) -> None:
        if not self._state_file.exists():
            return
        state = self._read_state()
        if isinstance(state, ChatGPTPendingView):
            self._write_state(ChatGPTSettledView(status="failed"))

    def view(self) -> ChatGPTSetupView:
        if not self._state_file.exists():
            return ChatGPTSettledView(status="disconnected")
        return self._read_state()

    def begin(self, authorization: CodexDeviceAuthorization) -> bool:
        state = self.view()
        if isinstance(state, ChatGPTPendingView) or state.status == "connected":
            return False
        self._write_state(ChatGPTPendingView(authorization=authorization))
        return True

    def connect(self, login_id: str) -> bool:
        state = self.view()
        if not _matches_pending_login(state, login_id):
            return False
        self._write_state(ChatGPTSettledView(status="connected"))
        return True

    def fail(self, login_id: str) -> bool:
        state = self.view()
        if not _matches_pending_login(state, login_id):
            return False
        self._write_state(ChatGPTSettledView(status="failed"))
        return True

    def cancel(self, login_id: str) -> bool:
        state = self.view()
        if not _matches_pending_login(state, login_id):
            return False
        self._write_state(ChatGPTSettledView(status="cancelled"))
        return True

    def fail_to_start(self) -> None:
        state = self.view()
        if not isinstance(state, ChatGPTPendingView) and state.status != "connected":
            self._write_state(ChatGPTSettledView(status="failed"))

    def fail_connected(self) -> bool:
        if self.view().status != "connected":
            return False
        self._write_state(ChatGPTSettledView(status="failed"))
        return True

    def _read_state(self) -> ChatGPTSetupView:
        try:
            payload: Any = read_private_json(self._state_file)
            return _parse_state(payload)
        except PrivateStateFileNotRegularError as error:
            raise SetupStateError("ChatGPT setup state is not a regular file") from error
        except SetupStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SetupStateError("ChatGPT setup state is not readable") from error

    def _write_state(self, state: ChatGPTSetupView) -> None:
        try:
            write_private_json_atomically(self._state_file, _serialize_state(state))
        except OSError as error:
            raise SetupStateError("ChatGPT setup state cannot be persisted") from error


class ChatGPTSetup:
    def __init__(self, *, store: ChatGPTSetupStore, codex: CodexOnboardingPort) -> None:
        self._store = store
        self._codex = codex
        self._completion_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._cancellation: _CancellationInProgress | None = None

    def view(self) -> ChatGPTSetupView:
        return self._store.view()

    async def verify_persisted_connection(self) -> None:
        if self._store.view().status != "connected":
            return
        try:
            await self._codex.verify_chatgpt_account()
        except CodexOnboardingError:
            self._store.fail_connected()
            raise

    async def start_authorization(self) -> ChatGPTAuthorizationStart:
        async with self._connect_lock:
            state = self._store.view()
            if isinstance(state, ChatGPTPendingView) or state.status == "connected":
                return ChatGPTAuthorizationStart.ALREADY_ACTIVE
            try:
                authorization = await self._codex.start_device_authorization()
            except CodexOnboardingError:
                self._store.fail_to_start()
                return ChatGPTAuthorizationStart.FAILED
            try:
                started = self._store.begin(authorization)
            except SetupStateError:
                with suppress(CodexOnboardingError):
                    await self._codex.cancel(authorization.login_id)
                raise
            if not started:
                with suppress(CodexOnboardingError):
                    await self._codex.cancel(authorization.login_id)
                return ChatGPTAuthorizationStart.ALREADY_ACTIVE
            self._completion_task = asyncio.create_task(
                self._complete(authorization.login_id),
                name="poppy-chatgpt-login",
            )
            return ChatGPTAuthorizationStart.STARTED

    async def close(self) -> None:
        await self._stop_completion_task()

    async def cancel(self) -> bool:
        async with self._connect_lock:
            state = self._store.view()
            if not isinstance(state, ChatGPTPendingView):
                return False
            login_id = state.authorization.login_id
            cancellation = _CancellationInProgress(login_id=login_id)
            self._cancellation = cancellation
            try:
                try:
                    await self._codex.cancel(login_id)
                except CodexOnboardingError:
                    if cancellation.completion_failed:
                        self._store.fail(login_id)
                    raise
                cancelled = self._store.cancel(login_id)
                if cancelled:
                    await self._stop_completion_task()
                return cancelled
            finally:
                self._cancellation = None

    async def _stop_completion_task(self) -> None:
        task = self._completion_task
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._completion_task = None

    async def _complete(self, login_id: str) -> None:
        try:
            await self._codex.wait_for_completion(login_id)
        except CodexOnboardingError:
            cancellation = self._cancellation
            if cancellation is not None and cancellation.login_id == login_id:
                cancellation.completion_failed = True
                return
            self._store.fail(login_id)
            return
        self._store.connect(login_id)


def _matches_pending_login(state: ChatGPTSetupView, login_id: str) -> bool:
    return isinstance(state, ChatGPTPendingView) and secrets.compare_digest(
        state.authorization.login_id, login_id
    )


def _serialize_state(state: ChatGPTSetupView) -> dict[str, object]:
    if isinstance(state, ChatGPTPendingView):
        return {
            "version": 1,
            "status": "pending",
            "login_id": state.authorization.login_id,
            "verification_url": state.authorization.verification_url,
            "user_code": state.authorization.user_code,
        }
    return {"version": 1, "status": state.status}


def _parse_state(payload: Any) -> ChatGPTSetupView:
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != 1
    ):
        raise SetupStateError("ChatGPT setup state has an unsupported format")
    status = payload.get("status")
    if status == "pending":
        if set(payload) != {
            "version",
            "status",
            "login_id",
            "verification_url",
            "user_code",
        }:
            raise SetupStateError("ChatGPT setup state has an unsupported format")
        try:
            authorization = CodexDeviceAuthorization(
                login_id=payload["login_id"],
                verification_url=payload["verification_url"],
                user_code=payload["user_code"],
            )
        except (TypeError, ValueError) as error:
            raise SetupStateError("ChatGPT setup state has invalid activation data") from error
        return ChatGPTPendingView(authorization=authorization)
    if status in {"connected", "failed", "cancelled"} and set(payload) == {
        "version",
        "status",
    }:
        return ChatGPTSettledView(status=status)
    raise SetupStateError("ChatGPT setup state has an unsupported phase")
