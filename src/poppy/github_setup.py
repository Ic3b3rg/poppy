import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from poppy.adapters.github_onboarding import GitHubAppCredentials, GitHubInstallation
from poppy.setup_session import SetupStateError

GITHUB_SETUP_STATE_FILE = "github-setup.json"
_CORRELATION_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True, slots=True)
class PendingInstallation:
    app: GitHubAppCredentials
    correlation_state: str


@dataclass(frozen=True, slots=True)
class GitHubSetupView:
    status: Literal["disconnected", "installation", "connected"]
    pending: PendingInstallation | None = None


class GitHubSetupStore:
    def __init__(self, *, data_dir: Path) -> None:
        self._state_file = data_dir / GITHUB_SETUP_STATE_FILE

    def initialize(self) -> None:
        if self._state_file.exists():
            self.view()

    def view(self) -> GitHubSetupView:
        if not self._state_file.exists():
            return GitHubSetupView(status="disconnected")
        payload = self._read_state()
        phase = payload.get("phase")
        if phase == "manifest":
            _parse_manifest(payload)
            return GitHubSetupView(status="disconnected")
        if phase == "exchange":
            _parse_exchange(payload)
            return GitHubSetupView(status="disconnected")
        if phase == "installation":
            return GitHubSetupView(status="installation", pending=_parse_pending(payload))
        if phase == "connected":
            _parse_connected(payload)
            return GitHubSetupView(status="connected")
        raise SetupStateError("GitHub setup state has an unsupported phase")

    def begin_manifest(self) -> str:
        state = secrets.token_urlsafe(32)
        self._write_state(
            {
                "version": 1,
                "phase": "manifest",
                "manifest_state": state,
            }
        )
        return state

    def consume_manifest(self, presented_state: str) -> bool:
        payload = self._read_state()
        if payload.get("phase") != "manifest":
            _validate_payload(payload)
            return False
        correlation_state = _parse_manifest(payload)
        if not secrets.compare_digest(correlation_state, presented_state):
            return False
        self._write_state({"version": 1, "phase": "exchange"})
        return True

    def await_installation(self, app: GitHubAppCredentials) -> PendingInstallation:
        payload = self._read_state()
        try:
            _parse_exchange(payload)
        except SetupStateError as error:
            raise SetupStateError("GitHub setup state cannot accept App credentials") from error
        correlation_state = secrets.token_urlsafe(32)
        pending = PendingInstallation(app=app, correlation_state=correlation_state)
        self._write_state(
            {
                "version": 1,
                "phase": "installation",
                "app": _serialize_app(app),
                "installation_state": correlation_state,
            }
        )
        return pending

    def consume_installation(self, presented_state: str) -> PendingInstallation | None:
        payload = self._read_state()
        if payload.get("phase") != "installation":
            _validate_payload(payload)
            return None
        pending = _parse_pending(payload)
        if not secrets.compare_digest(pending.correlation_state, presented_state):
            return None
        rotated = PendingInstallation(
            app=pending.app,
            correlation_state=secrets.token_urlsafe(32),
        )
        self._write_pending_installation(rotated)
        return rotated

    def connect(
        self,
        *,
        pending: PendingInstallation,
        installation: GitHubInstallation,
    ) -> bool:
        payload = self._read_state()
        if payload.get("phase") != "installation":
            return False
        current = _parse_pending(payload)
        if current.app != pending.app or not secrets.compare_digest(
            current.correlation_state,
            pending.correlation_state,
        ):
            return False
        self._write_state(
            {
                "version": 1,
                "phase": "connected",
                "app": _serialize_app(pending.app),
                "installation": _serialize_installation(installation),
            }
        )
        return True

    def _write_pending_installation(self, pending: PendingInstallation) -> None:
        self._write_state(
            {
                "version": 1,
                "phase": "installation",
                "app": _serialize_app(pending.app),
                "installation_state": pending.correlation_state,
            }
        )

    def _read_state(self) -> dict[str, Any]:
        try:
            if self._state_file.is_symlink() or not self._state_file.is_file():
                raise SetupStateError("GitHub setup state is not a regular file")
            self._state_file.chmod(0o600)
            payload: Any = json.loads(self._state_file.read_text(encoding="utf-8"))
        except SetupStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SetupStateError("GitHub setup state is not readable") from error
        if not isinstance(payload, dict):
            raise SetupStateError("GitHub setup state has an unsupported format")
        return payload

    def _write_state(self, payload: dict[str, object]) -> None:
        directory = self._state_file.parent
        temporary_path: Path | None = None
        try:
            file_descriptor, raw_temporary_path = tempfile.mkstemp(
                dir=directory,
                prefix=f".{GITHUB_SETUP_STATE_FILE}.",
            )
            temporary_path = Path(raw_temporary_path)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
                json.dump(payload, temporary_file, separators=(",", ":"))
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
            raise SetupStateError("GitHub setup state cannot be persisted") from error
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def github_app_manifest(public_origin: str) -> dict[str, object]:
    return {
        "name": "Poppy",
        "url": public_origin,
        "hook_attributes": {
            "url": f"{public_origin}/webhooks/github",
            "active": True,
        },
        "redirect_url": f"{public_origin}/setup/github/manifest/callback",
        "setup_url": f"{public_origin}/setup/github/installation",
        "public": False,
        "default_permissions": {
            "checks": "write",
            "pull_requests": "write",
            "issues": "write",
            "metadata": "read",
        },
        "default_events": ["pull_request"],
    }


def installation_url(pending: PendingInstallation) -> str:
    return (
        f"https://github.com/apps/{pending.app.slug}/installations/new?"
        f"state={pending.correlation_state}"
    )


def _serialize_app(app: GitHubAppCredentials) -> dict[str, object]:
    return {
        "id": app.app_id,
        "slug": app.slug,
        "private_key": app.private_key,
        "webhook_secret": app.webhook_secret,
    }


def _serialize_installation(installation: GitHubInstallation) -> dict[str, object]:
    return {
        "id": installation.installation_id,
        "account_id": installation.account_id,
        "account_login": installation.account_login,
        "target_type": installation.target_type,
    }


def _parse_pending(payload: dict[str, Any]) -> PendingInstallation:
    if set(payload) != {"version", "phase", "app", "installation_state"}:
        raise SetupStateError("GitHub installation state has an unsupported format")
    _require_version(payload)
    if payload["phase"] != "installation":
        raise SetupStateError("GitHub installation state has an impossible phase")
    correlation_state = payload["installation_state"]
    if (
        not isinstance(correlation_state, str)
        or _CORRELATION_PATTERN.fullmatch(correlation_state) is None
    ):
        raise SetupStateError("GitHub installation state has an invalid correlation value")
    return PendingInstallation(
        app=_parse_app(payload["app"]),
        correlation_state=correlation_state,
    )


def _parse_connected(payload: dict[str, Any]) -> None:
    if set(payload) != {"version", "phase", "app", "installation"}:
        raise SetupStateError("GitHub connection state has an unsupported format")
    _require_version(payload)
    if payload["phase"] != "connected":
        raise SetupStateError("GitHub connection state has an impossible phase")
    _parse_app(payload["app"])
    installation = payload["installation"]
    if not isinstance(installation, dict) or set(installation) != {
        "id",
        "account_id",
        "account_login",
        "target_type",
    }:
        raise SetupStateError("GitHub connection state has invalid installation metadata")
    if (
        type(installation["id"]) is not int
        or installation["id"] <= 0
        or type(installation["account_id"]) is not int
        or installation["account_id"] <= 0
        or not isinstance(installation["account_login"], str)
        or not installation["account_login"]
        or not isinstance(installation["target_type"], str)
        or not installation["target_type"]
    ):
        raise SetupStateError("GitHub connection state has invalid installation metadata")


def _parse_app(payload: Any) -> GitHubAppCredentials:
    if not isinstance(payload, dict) or set(payload) != {
        "id",
        "slug",
        "private_key",
        "webhook_secret",
    }:
        raise SetupStateError("GitHub App credentials have an unsupported format")
    try:
        return GitHubAppCredentials(
            app_id=payload["id"],
            slug=payload["slug"],
            private_key=payload["private_key"],
            webhook_secret=payload["webhook_secret"],
        )
    except (TypeError, ValueError) as error:
        raise SetupStateError("GitHub App credentials are invalid") from error


def _parse_manifest(payload: dict[str, Any]) -> str:
    if set(payload) != {"version", "phase", "manifest_state"}:
        raise SetupStateError("GitHub manifest state has an unsupported format")
    _require_version(payload)
    if payload["phase"] != "manifest":
        raise SetupStateError("GitHub manifest state has an impossible phase")
    correlation_state = payload["manifest_state"]
    if (
        not isinstance(correlation_state, str)
        or _CORRELATION_PATTERN.fullmatch(correlation_state) is None
    ):
        raise SetupStateError("GitHub manifest state has an invalid correlation value")
    return correlation_state


def _parse_exchange(payload: dict[str, Any]) -> None:
    if payload != {"version": 1, "phase": "exchange"}:
        raise SetupStateError("GitHub exchange state has an unsupported format")


def _require_version(payload: dict[str, Any]) -> None:
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise SetupStateError("GitHub setup state has an unsupported version")


def _validate_payload(payload: dict[str, Any]) -> None:
    phase = payload.get("phase")
    if phase == "manifest":
        _parse_manifest(payload)
        return
    if phase == "exchange":
        _parse_exchange(payload)
        return
    if phase == "installation":
        _parse_pending(payload)
        return
    if phase == "connected":
        _parse_connected(payload)
        return
    raise SetupStateError("GitHub setup state has an unsupported phase")
