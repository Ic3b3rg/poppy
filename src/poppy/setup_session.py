import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

TOKEN_BYTES = 32
STATE_FILE_NAME = "setup-state.json"
BOOTSTRAP_TTL_SECONDS = 24 * 60 * 60
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")


class SetupStateError(RuntimeError):
    """The persisted Setup Session state cannot be used safely."""


@dataclass(frozen=True)
class RecoverableCredential:
    token: str
    expires_at: int


@dataclass(frozen=True)
class ActiveSessionCredential:
    digest: str
    expires_at: int


@dataclass(frozen=True)
class SetupState:
    version: int
    bootstrap: RecoverableCredential | None
    pending_session: RecoverableCredential | None
    session: ActiveSessionCredential | None


@dataclass(frozen=True)
class SetupSessionGrant:
    token: str
    max_age: int


class SetupSessionStore:
    def __init__(
        self,
        *,
        data_dir: Path,
        bootstrap_ttl_seconds: int,
        session_ttl_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._state_file = data_dir / STATE_FILE_NAME
        self._bootstrap_ttl_seconds = bootstrap_ttl_seconds
        self._session_ttl_seconds = session_ttl_seconds
        self._clock = clock

    def initialize(self) -> str | None:
        now = int(self._clock())
        if self._state_file.exists():
            bootstrap = self._read_state().bootstrap
            return bootstrap.token if bootstrap is not None and bootstrap.expires_at > now else None

        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._write_state(
            _new_setup_state(
                bootstrap_token=token,
                now=now,
                bootstrap_ttl_seconds=self._bootstrap_ttl_seconds,
            )
        )
        return token

    def exchange_bootstrap(self, token: str) -> SetupSessionGrant | None:
        state = self._read_state()
        next_state, grant = _begin_session_exchange(
            state,
            presented_token=token,
            new_bootstrap_token=secrets.token_urlsafe(TOKEN_BYTES),
            new_session_token=secrets.token_urlsafe(TOKEN_BYTES),
            now=int(self._clock()),
            session_ttl_seconds=self._session_ttl_seconds,
        )
        if next_state != state:
            self._write_state(next_state)
        return grant

    def confirm_session(self, session_token: str) -> str | None:
        state = self._read_state()
        next_state, is_valid = _confirm_session(
            state,
            presented_token=session_token,
            now=int(self._clock()),
        )
        if not is_valid:
            return None
        if next_state != state:
            self._write_state(next_state)
        return _csrf_token(session_token)

    def mutation_is_authorized(self, *, session_token: str, csrf_token: str) -> bool:
        state = self._read_state()
        if not _active_session_is_valid(
            state,
            presented_token=session_token,
            now=int(self._clock()),
        ):
            return False
        return secrets.compare_digest(_csrf_token(session_token), csrf_token)

    def _read_state(self) -> SetupState:
        try:
            if self._state_file.is_symlink() or not self._state_file.is_file():
                raise SetupStateError("Setup Session state is not a regular file")
            self._state_file.chmod(0o600)
            payload: Any = json.loads(self._state_file.read_text(encoding="utf-8"))
            return _parse_state(payload)
        except SetupStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SetupStateError("Setup Session state is not readable") from error

    def _write_state(self, state: SetupState) -> None:
        directory = self._state_file.parent
        temporary_path: Path | None = None
        try:
            file_descriptor, raw_temporary_path = tempfile.mkstemp(
                dir=directory,
                prefix=f".{STATE_FILE_NAME}.",
            )
            temporary_path = Path(raw_temporary_path)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(_serialize_state(state), temporary_file, separators=(",", ":"))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._state_file)
            directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            directory_descriptor = os.open(directory, directory_flags)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as error:
            raise SetupStateError("Setup Session state cannot be persisted") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def _new_setup_state(
    *,
    bootstrap_token: str,
    now: int,
    bootstrap_ttl_seconds: int,
) -> SetupState:
    return SetupState(
        version=1,
        bootstrap=RecoverableCredential(
            token=bootstrap_token,
            expires_at=now + bootstrap_ttl_seconds,
        ),
        pending_session=None,
        session=None,
    )


def _begin_session_exchange(
    state: SetupState,
    *,
    presented_token: str,
    new_bootstrap_token: str,
    new_session_token: str,
    now: int,
    session_ttl_seconds: int,
) -> tuple[SetupState, SetupSessionGrant | None]:
    bootstrap = state.bootstrap
    if (
        bootstrap is None
        or bootstrap.expires_at <= now
        or not secrets.compare_digest(bootstrap.token, presented_token)
    ):
        return state, None

    pending_session = state.pending_session
    if pending_session is None or pending_session.expires_at <= now:
        pending_session = RecoverableCredential(
            token=new_session_token,
            expires_at=now + session_ttl_seconds,
        )

    next_state = replace(
        state,
        bootstrap=RecoverableCredential(
            token=new_bootstrap_token,
            expires_at=pending_session.expires_at,
        ),
        pending_session=pending_session,
    )
    return (
        next_state,
        SetupSessionGrant(
            token=pending_session.token,
            max_age=pending_session.expires_at - now,
        ),
    )


def _confirm_session(
    state: SetupState,
    *,
    presented_token: str,
    now: int,
) -> tuple[SetupState, bool]:
    if state.session is not None:
        return state, _active_session_is_valid(
            state,
            presented_token=presented_token,
            now=now,
        )

    pending_session = state.pending_session
    if (
        pending_session is None
        or pending_session.expires_at <= now
        or not secrets.compare_digest(pending_session.token, presented_token)
    ):
        return state, False

    return (
        SetupState(
            version=state.version,
            bootstrap=None,
            pending_session=None,
            session=ActiveSessionCredential(
                digest=_token_digest(presented_token),
                expires_at=pending_session.expires_at,
            ),
        ),
        True,
    )


def _active_session_is_valid(
    state: SetupState,
    *,
    presented_token: str,
    now: int,
) -> bool:
    active_session = state.session
    return (
        active_session is not None
        and active_session.expires_at > now
        and secrets.compare_digest(
            active_session.digest,
            _token_digest(presented_token),
        )
    )


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _csrf_token(session_token: str) -> str:
    return hmac.new(
        session_token.encode(),
        b"poppy-setup-csrf-v1",
        hashlib.sha256,
    ).hexdigest()


def _serialize_state(state: SetupState) -> dict[str, object]:
    return {
        "version": state.version,
        "bootstrap": _serialize_recoverable_credential(state.bootstrap),
        "pending_session": _serialize_recoverable_credential(state.pending_session),
        "session": (
            {
                "digest": state.session.digest,
                "expires_at": state.session.expires_at,
            }
            if state.session is not None
            else None
        ),
    }


def _serialize_recoverable_credential(
    credential: RecoverableCredential | None,
) -> dict[str, object] | None:
    if credential is None:
        return None
    return {"token": credential.token, "expires_at": credential.expires_at}


def _parse_state(payload: Any) -> SetupState:
    expected_fields = {"version", "bootstrap", "pending_session", "session"}
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or type(payload["version"]) is not int
        or payload["version"] != 1
    ):
        raise SetupStateError("Setup Session state has an unsupported format")

    state = SetupState(
        version=1,
        bootstrap=_parse_recoverable_credential(payload["bootstrap"]),
        pending_session=_parse_recoverable_credential(payload["pending_session"]),
        session=_parse_active_session(payload["session"]),
    )
    bootstrap_phase = (
        state.bootstrap is not None
        and state.session is None
        and (
            state.pending_session is None
            or state.pending_session.expires_at == state.bootstrap.expires_at
        )
    )
    active_phase = (
        state.bootstrap is None and state.pending_session is None and state.session is not None
    )
    if not (bootstrap_phase or active_phase):
        raise SetupStateError("Setup Session state has an impossible phase")
    return state


def _parse_recoverable_credential(payload: Any) -> RecoverableCredential | None:
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {"token", "expires_at"}:
        raise SetupStateError("Setup Session credential has an invalid format")
    token = payload["token"]
    expires_at = payload["expires_at"]
    if (
        not isinstance(token, str)
        or _TOKEN_PATTERN.fullmatch(token) is None
        or type(expires_at) is not int
        or expires_at <= 0
    ):
        raise SetupStateError("Setup Session credential has an invalid format")
    return RecoverableCredential(token=token, expires_at=expires_at)


def _parse_active_session(payload: Any) -> ActiveSessionCredential | None:
    if payload is None:
        return None
    if not isinstance(payload, dict) or set(payload) != {"digest", "expires_at"}:
        raise SetupStateError("Setup Session credential has an invalid format")
    digest = payload["digest"]
    expires_at = payload["expires_at"]
    if (
        not isinstance(digest, str)
        or _DIGEST_PATTERN.fullmatch(digest) is None
        or type(expires_at) is not int
        or expires_at <= 0
    ):
        raise SetupStateError("Setup Session credential has an invalid format")
    return ActiveSessionCredential(digest=digest, expires_at=expires_at)
