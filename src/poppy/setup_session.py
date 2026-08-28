import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

TOKEN_BYTES = 32
STATE_FILE_NAME = "setup-state.json"


class SetupStateError(RuntimeError):
    """The persisted Setup Session state cannot be used safely."""


@dataclass(frozen=True)
class StoredCredential:
    digest: str
    expires_at: int


@dataclass(frozen=True)
class SetupState:
    version: int
    bootstrap: StoredCredential | None
    session: StoredCredential | None


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
        if self._state_file.exists():
            self._read_state()
            return None

        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = int(self._clock())
        self._write_state(
            SetupState(
                version=1,
                bootstrap=StoredCredential(
                    digest=_token_digest(token),
                    expires_at=now + self._bootstrap_ttl_seconds,
                ),
                session=None,
            )
        )
        return token

    def exchange_bootstrap(self, token: str) -> SetupSessionGrant | None:
        state = self._read_state()
        credential = state.bootstrap
        if (
            credential is None
            or credential.expires_at <= int(self._clock())
            or not secrets.compare_digest(credential.digest, _token_digest(token))
        ):
            return None

        session_token = secrets.token_urlsafe(TOKEN_BYTES)
        self._write_state(
            SetupState(
                version=state.version,
                bootstrap=None,
                session=StoredCredential(
                    digest=_token_digest(session_token),
                    expires_at=int(self._clock()) + self._session_ttl_seconds,
                ),
            )
        )
        return SetupSessionGrant(token=session_token, max_age=self._session_ttl_seconds)

    def session_is_valid(self, token: str) -> bool:
        credential = self._read_state().session
        return (
            credential is not None
            and credential.expires_at > int(self._clock())
            and secrets.compare_digest(credential.digest, _token_digest(token))
        )

    def csrf_token_for(self, session_token: str) -> str | None:
        if not self.session_is_valid(session_token):
            return None
        return _csrf_token(session_token)

    def mutation_is_authorized(self, *, session_token: str, csrf_token: str) -> bool:
        expected_token = self.csrf_token_for(session_token)
        return expected_token is not None and secrets.compare_digest(expected_token, csrf_token)

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
            directory_descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError as error:
            raise SetupStateError("Setup Session state cannot be persisted") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


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
        "bootstrap": asdict(state.bootstrap) if state.bootstrap is not None else None,
        "session": asdict(state.session) if state.session is not None else None,
    }


def _parse_state(payload: Any) -> SetupState:
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise SetupStateError("Setup Session state has an unsupported format")
    return SetupState(
        version=1,
        bootstrap=_parse_credential(payload.get("bootstrap")),
        session=_parse_credential(payload.get("session")),
    )


def _parse_credential(payload: Any) -> StoredCredential | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise SetupStateError("Setup Session credential has an invalid format")
    digest = payload.get("digest")
    expires_at = payload.get("expires_at")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
    ):
        raise SetupStateError("Setup Session credential has an invalid format")
    return StoredCredential(digest=digest, expires_at=expires_at)
