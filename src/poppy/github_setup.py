import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlencode

from poppy.adapters.github_onboarding import GitHubAppCredentials, GitHubInstallation
from poppy.private_state import (
    PrivateStateFileNotRegularError,
    read_private_json,
    write_private_json_atomically,
)
from poppy.setup_session import SetupStateError

GITHUB_SETUP_STATE_FILE = "github-setup.json"
_CORRELATION_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True, slots=True)
class PendingInstallation:
    app: GitHubAppCredentials
    correlation_state: str


@dataclass(frozen=True, slots=True)
class ManifestRegistration:
    correlation_state: str
    organization: str | None


@dataclass(frozen=True, slots=True)
class GitHubSetupView:
    status: Literal["disconnected", "installation", "connected"]
    pending: PendingInstallation | None = None


class GitHubSetupStore:
    def __init__(self, *, data_dir: Path) -> None:
        self._state_file = data_dir / GITHUB_SETUP_STATE_FILE

    def initialize(self) -> None:
        if not self._state_file.exists():
            return
        payload = self._read_state()
        if payload.get("phase") == "exchange":
            _parse_exchange(payload)
            self._write_retryable_registration()
            return
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
        if phase == "registration_retry":
            _parse_registration_retry(payload)
            return GitHubSetupView(status="disconnected")
        if phase == "installation":
            return GitHubSetupView(status="installation", pending=_parse_pending(payload))
        if phase == "connected":
            _parse_connected(payload)
            return GitHubSetupView(status="connected")
        raise SetupStateError("GitHub setup state has an unsupported phase")

    def begin_manifest(self, *, organization: str | None) -> ManifestRegistration | None:
        if self._state_file.exists():
            payload = self._read_state()
            if payload.get("phase") == "manifest":
                return _parse_manifest(payload)
            if payload.get("phase") == "exchange":
                _parse_exchange(payload)
                return None
            if payload.get("phase") != "registration_retry":
                _validate_payload(payload)
                raise SetupStateError("GitHub setup state cannot start App registration")
            _parse_registration_retry(payload)
        state = secrets.token_urlsafe(32)
        registration = ManifestRegistration(
            correlation_state=state,
            organization=organization,
        )
        self._write_state(
            {
                "version": 1,
                "phase": "manifest",
                "manifest_state": state,
                "organization": organization,
            }
        )
        return registration

    def retry_manifest_registration(self) -> None:
        payload = self._read_state()
        _parse_exchange(payload)
        self._write_retryable_registration()

    def consume_manifest(self, presented_state: str) -> bool:
        payload = self._read_state()
        if payload.get("phase") != "manifest":
            _validate_payload(payload)
            return False
        registration = _parse_manifest(payload)
        if not secrets.compare_digest(registration.correlation_state, presented_state):
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

    def _write_retryable_registration(self) -> None:
        self._write_state({"version": 1, "phase": "registration_retry"})

    def _read_state(self) -> dict[str, Any]:
        try:
            payload: Any = read_private_json(self._state_file)
        except PrivateStateFileNotRegularError as error:
            raise SetupStateError("GitHub setup state is not a regular file") from error
        except SetupStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SetupStateError("GitHub setup state is not readable") from error
        if not isinstance(payload, dict):
            raise SetupStateError("GitHub setup state has an unsupported format")
        return payload

    def _write_state(self, payload: dict[str, object]) -> None:
        try:
            write_private_json_atomically(self._state_file, payload)
        except OSError as error:
            raise SetupStateError("GitHub setup state cannot be persisted") from error


def github_app_manifest(public_origin: str) -> dict[str, object]:
    return {
        # GitHub App names are globally unique, so derive a stable per-instance default.
        # https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app#registering-a-github-app
        "name": f"Poppy {hashlib.sha256(public_origin.encode()).hexdigest()[:12]}",
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


def manifest_registration_url(
    *,
    correlation_state: str,
    organization: str | None,
) -> str:
    # Private Apps must be registered on the account where they will be installed.
    # https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest#1-you-redirect-people-to-github-to-register-a-new-github-app
    if organization is None:
        path = "/settings/apps/new"
    else:
        path = f"/organizations/{quote(organization, safe='')}/settings/apps/new"
    return f"https://github.com{path}?{urlencode({'state': correlation_state})}"


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


def _parse_manifest(payload: dict[str, Any]) -> ManifestRegistration:
    if set(payload) != {"version", "phase", "manifest_state", "organization"}:
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
    organization = payload["organization"]
    if organization is not None and not isinstance(organization, str):
        raise SetupStateError("GitHub manifest state has an invalid organization")
    return ManifestRegistration(
        correlation_state=correlation_state,
        organization=organization,
    )


def _parse_exchange(payload: dict[str, Any]) -> None:
    if payload != {"version": 1, "phase": "exchange"}:
        raise SetupStateError("GitHub exchange state has an unsupported format")


def _parse_registration_retry(payload: dict[str, Any]) -> None:
    if payload != {"version": 1, "phase": "registration_retry"}:
        raise SetupStateError("GitHub registration retry state has an unsupported format")


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
    if phase == "registration_retry":
        _parse_registration_retry(payload)
        return
    if phase == "installation":
        _parse_pending(payload)
        return
    if phase == "connected":
        _parse_connected(payload)
        return
    raise SetupStateError("GitHub setup state has an unsupported phase")
