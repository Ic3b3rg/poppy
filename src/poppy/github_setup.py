import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, assert_never
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
_GITHUB_ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")


@dataclass(frozen=True, slots=True)
class GitHubOrganization:
    login: str

    def __post_init__(self) -> None:
        if _GITHUB_ACCOUNT_PATTERN.fullmatch(self.login) is None:
            raise ValueError("GitHub organization is invalid")


@dataclass(frozen=True, slots=True)
class PendingInstallation:
    app: GitHubAppCredentials
    correlation_state: str


@dataclass(frozen=True, slots=True)
class ManifestRegistration:
    correlation_state: str
    organization: GitHubOrganization | None


@dataclass(frozen=True, slots=True)
class ManifestExchange:
    pass


@dataclass(frozen=True, slots=True)
class RegistrationRetry:
    pass


@dataclass(frozen=True, slots=True)
class ConnectedInstallation:
    app: GitHubAppCredentials
    installation: GitHubInstallation


type GitHubSetupState = (
    ManifestRegistration
    | ManifestExchange
    | RegistrationRetry
    | PendingInstallation
    | ConnectedInstallation
)


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
        state = self._read_state()
        if isinstance(state, ManifestExchange):
            self._write_state(RegistrationRetry())

    def view(self) -> GitHubSetupView:
        if not self._state_file.exists():
            return GitHubSetupView(status="disconnected")
        return _setup_view(self._read_state())

    def begin_manifest(
        self,
        *,
        organization: GitHubOrganization | None,
    ) -> ManifestRegistration | None:
        if self._state_file.exists():
            state = self._read_state()
            if isinstance(state, ManifestRegistration):
                return state
            if isinstance(state, ManifestExchange):
                return None
            if not isinstance(state, RegistrationRetry):
                raise SetupStateError("GitHub setup state cannot start App registration")
        registration = ManifestRegistration(
            correlation_state=secrets.token_urlsafe(32),
            organization=organization,
        )
        self._write_state(registration)
        return registration

    def retry_manifest_registration(self) -> None:
        if not isinstance(self._read_state(), ManifestExchange):
            raise SetupStateError("GitHub setup state cannot retry App registration")
        self._write_state(RegistrationRetry())

    def consume_manifest(self, presented_state: str) -> bool:
        state = self._read_state()
        if not isinstance(state, ManifestRegistration):
            return False
        if not secrets.compare_digest(state.correlation_state, presented_state):
            return False
        self._write_state(ManifestExchange())
        return True

    def await_installation(self, app: GitHubAppCredentials) -> PendingInstallation:
        if not isinstance(self._read_state(), ManifestExchange):
            raise SetupStateError("GitHub setup state cannot accept App credentials")
        pending = PendingInstallation(
            app=app,
            correlation_state=secrets.token_urlsafe(32),
        )
        self._write_state(pending)
        return pending

    def consume_installation(self, presented_state: str) -> PendingInstallation | None:
        state = self._read_state()
        if not isinstance(state, PendingInstallation):
            return None
        if not secrets.compare_digest(state.correlation_state, presented_state):
            return None
        rotated = PendingInstallation(
            app=state.app,
            correlation_state=secrets.token_urlsafe(32),
        )
        self._write_state(rotated)
        return rotated

    def connect(
        self,
        *,
        pending: PendingInstallation,
        installation: GitHubInstallation,
    ) -> bool:
        state = self._read_state()
        if not isinstance(state, PendingInstallation):
            return False
        if state.app != pending.app or not secrets.compare_digest(
            state.correlation_state,
            pending.correlation_state,
        ):
            return False
        self._write_state(
            ConnectedInstallation(
                app=pending.app,
                installation=installation,
            )
        )
        return True

    def _read_state(self) -> GitHubSetupState:
        try:
            payload: Any = read_private_json(self._state_file)
            return _parse_state(payload)
        except PrivateStateFileNotRegularError as error:
            raise SetupStateError("GitHub setup state is not a regular file") from error
        except SetupStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SetupStateError("GitHub setup state is not readable") from error

    def _write_state(self, state: GitHubSetupState) -> None:
        try:
            write_private_json_atomically(self._state_file, _serialize_state(state))
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
    organization: GitHubOrganization | None,
) -> str:
    # Private Apps must be registered on the account where they will be installed.
    # https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest#1-you-redirect-people-to-github-to-register-a-new-github-app
    if organization is None:
        path = "/settings/apps/new"
    else:
        path = f"/organizations/{quote(organization.login, safe='')}/settings/apps/new"
    return f"https://github.com{path}?{urlencode({'state': correlation_state})}"


def _setup_view(state: GitHubSetupState) -> GitHubSetupView:
    if isinstance(state, PendingInstallation):
        return GitHubSetupView(status="installation", pending=state)
    if isinstance(state, ConnectedInstallation):
        return GitHubSetupView(status="connected")
    return GitHubSetupView(status="disconnected")


def _serialize_state(state: GitHubSetupState) -> dict[str, object]:
    if isinstance(state, ManifestRegistration):
        return {
            "version": 1,
            "phase": "manifest",
            "manifest_state": state.correlation_state,
            "organization": (state.organization.login if state.organization is not None else None),
        }
    if isinstance(state, ManifestExchange):
        return {"version": 1, "phase": "exchange"}
    if isinstance(state, RegistrationRetry):
        return {"version": 1, "phase": "registration_retry"}
    if isinstance(state, PendingInstallation):
        return {
            "version": 1,
            "phase": "installation",
            "app": _serialize_app(state.app),
            "installation_state": state.correlation_state,
        }
    if isinstance(state, ConnectedInstallation):
        return {
            "version": 1,
            "phase": "connected",
            "app": _serialize_app(state.app),
            "installation": _serialize_installation(state.installation),
        }
    assert_never(state)


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


def _parse_state(payload: Any) -> GitHubSetupState:
    if not isinstance(payload, dict):
        raise SetupStateError("GitHub setup state has an unsupported format")
    phase = payload.get("phase")
    if phase == "manifest":
        return _parse_manifest(payload)
    if phase == "exchange":
        return _parse_exchange(payload)
    if phase == "registration_retry":
        return _parse_registration_retry(payload)
    if phase == "installation":
        return _parse_pending(payload)
    if phase == "connected":
        return _parse_connected(payload)
    raise SetupStateError("GitHub setup state has an unsupported phase")


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


def _parse_connected(payload: dict[str, Any]) -> ConnectedInstallation:
    if set(payload) != {"version", "phase", "app", "installation"}:
        raise SetupStateError("GitHub connection state has an unsupported format")
    _require_version(payload)
    if payload["phase"] != "connected":
        raise SetupStateError("GitHub connection state has an impossible phase")
    app = _parse_app(payload["app"])
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
    return ConnectedInstallation(
        app=app,
        installation=GitHubInstallation(
            installation_id=installation["id"],
            account_id=installation["account_id"],
            account_login=installation["account_login"],
            target_type=installation["target_type"],
        ),
    )


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
    raw_organization = payload["organization"]
    try:
        organization = (
            GitHubOrganization(raw_organization) if raw_organization is not None else None
        )
    except (TypeError, ValueError) as error:
        raise SetupStateError("GitHub manifest state has an invalid organization") from error
    return ManifestRegistration(correlation_state=correlation_state, organization=organization)


def _parse_exchange(payload: dict[str, Any]) -> ManifestExchange:
    if payload != {"version": 1, "phase": "exchange"}:
        raise SetupStateError("GitHub exchange state has an unsupported format")
    return ManifestExchange()


def _parse_registration_retry(payload: dict[str, Any]) -> RegistrationRetry:
    if payload != {"version": 1, "phase": "registration_retry"}:
        raise SetupStateError("GitHub registration retry state has an unsupported format")
    return RegistrationRetry()


def _require_version(payload: dict[str, Any]) -> None:
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise SetupStateError("GitHub setup state has an unsupported version")
