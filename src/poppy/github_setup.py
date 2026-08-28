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
_APP_NAME_PATTERN = re.compile(r"Poppy [0-9a-f]{12}")


@dataclass(frozen=True, slots=True)
class GitHubOrganization:
    login: str

    def __post_init__(self) -> None:
        if _GITHUB_ACCOUNT_PATTERN.fullmatch(self.login) is None:
            raise ValueError("GitHub organization is invalid")


@dataclass(frozen=True, slots=True)
class PossibleOrphanGitHubApp:
    name: str
    organization: GitHubOrganization | None

    def __post_init__(self) -> None:
        if _APP_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError("possible orphan GitHub App name is invalid")


@dataclass(frozen=True, slots=True)
class RegistrationContext:
    organization: GitHubOrganization | None
    possible_orphan_apps: tuple[PossibleOrphanGitHubApp, ...] = ()


@dataclass(frozen=True, slots=True)
class ManifestRegistration:
    correlation_state: str
    app_name: str
    context: RegistrationContext

    def __post_init__(self) -> None:
        if _CORRELATION_PATTERN.fullmatch(self.correlation_state) is None:
            raise ValueError("GitHub manifest correlation state is invalid")
        if _APP_NAME_PATTERN.fullmatch(self.app_name) is None:
            raise ValueError("GitHub App name is invalid")


@dataclass(frozen=True, slots=True)
class ManifestExchange:
    registration: ManifestRegistration


@dataclass(frozen=True, slots=True)
class RegistrationRetry:
    context: RegistrationContext


@dataclass(frozen=True, slots=True)
class PendingInstallation:
    app: GitHubAppCredentials
    correlation_state: str
    context: RegistrationContext


@dataclass(frozen=True, slots=True)
class ConnectedInstallation:
    app: GitHubAppCredentials
    installation: GitHubInstallation
    context: RegistrationContext


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
    context: RegistrationContext | None = None


class GitHubSetupStore:
    def __init__(self, *, data_dir: Path) -> None:
        self._state_file = data_dir / GITHUB_SETUP_STATE_FILE

    def initialize(self) -> None:
        if not self._state_file.exists():
            return
        state = self._read_state()
        if isinstance(state, ManifestExchange):
            self._write_state(
                RegistrationRetry(context=_record_possible_orphan(state.registration))
            )

    def view(self) -> GitHubSetupView:
        if not self._state_file.exists():
            return GitHubSetupView(status="disconnected")
        return _setup_view(self._read_state())

    def connected_installation(self) -> ConnectedInstallation | None:
        if not self._state_file.exists():
            return None
        state = self._read_state()
        return state if isinstance(state, ConnectedInstallation) else None

    def begin_manifest(
        self,
        *,
        organization: GitHubOrganization | None,
        public_origin: str,
    ) -> ManifestRegistration | None:
        context = RegistrationContext(organization=organization)
        if self._state_file.exists():
            state = self._read_state()
            if isinstance(state, ManifestRegistration):
                if state.context.organization == organization:
                    return state
                context = RegistrationContext(
                    organization=organization,
                    possible_orphan_apps=_record_possible_orphan(state).possible_orphan_apps,
                )
            if isinstance(state, ManifestExchange):
                return None
            elif isinstance(state, RegistrationRetry):
                context = RegistrationContext(
                    organization=organization,
                    possible_orphan_apps=state.context.possible_orphan_apps,
                )
            elif not isinstance(state, ManifestRegistration):
                raise SetupStateError("GitHub setup state cannot start App registration")
        correlation_state = secrets.token_urlsafe(32)
        registration = ManifestRegistration(
            correlation_state=correlation_state,
            app_name=github_app_name(public_origin, correlation_state),
            context=context,
        )
        self._write_state(registration)
        return registration

    def retry_manifest_registration(self) -> PossibleOrphanGitHubApp:
        state = self._read_state()
        if not isinstance(state, ManifestExchange):
            raise SetupStateError("GitHub setup state cannot retry App registration")
        orphan = _possible_orphan(state.registration)
        self._write_state(RegistrationRetry(context=_record_possible_orphan(state.registration)))
        return orphan

    def consume_manifest(self, presented_state: str) -> bool:
        state = self._read_state()
        if not isinstance(state, ManifestRegistration):
            return False
        if not secrets.compare_digest(state.correlation_state, presented_state):
            return False
        self._write_state(ManifestExchange(registration=state))
        return True

    def await_installation(self, app: GitHubAppCredentials) -> PendingInstallation:
        state = self._read_state()
        if not isinstance(state, ManifestExchange):
            raise SetupStateError("GitHub setup state cannot accept App credentials")
        pending = PendingInstallation(
            app=app,
            correlation_state=secrets.token_urlsafe(32),
            context=state.registration.context,
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
            context=state.context,
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
                context=pending.context,
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


def github_app_name(public_origin: str, correlation_state: str) -> str:
    identity = f"{public_origin}\0{correlation_state}".encode()
    return f"Poppy {hashlib.sha256(identity).hexdigest()[:12]}"


def github_app_manifest(
    public_origin: str,
    *,
    app_name: str,
) -> dict[str, object]:
    return {
        # GitHub App names are globally unique, so derive a new default for each attempt.
        # https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app#registering-a-github-app
        "name": app_name,
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
        return GitHubSetupView(status="installation", pending=state, context=state.context)
    if isinstance(state, ConnectedInstallation):
        return GitHubSetupView(status="connected", context=state.context)
    if isinstance(state, ManifestRegistration):
        return GitHubSetupView(status="disconnected", context=state.context)
    if isinstance(state, ManifestExchange):
        return GitHubSetupView(status="disconnected", context=state.registration.context)
    if isinstance(state, RegistrationRetry):
        return GitHubSetupView(status="disconnected", context=state.context)
    assert_never(state)


def _possible_orphan(registration: ManifestRegistration) -> PossibleOrphanGitHubApp:
    return PossibleOrphanGitHubApp(
        name=registration.app_name,
        organization=registration.context.organization,
    )


def _record_possible_orphan(registration: ManifestRegistration) -> RegistrationContext:
    return RegistrationContext(
        organization=registration.context.organization,
        possible_orphan_apps=(
            *registration.context.possible_orphan_apps,
            _possible_orphan(registration),
        ),
    )


def _serialize_state(state: GitHubSetupState) -> dict[str, object]:
    if isinstance(state, ManifestRegistration):
        return {
            "version": 1,
            "phase": "manifest",
            **_serialize_registration(state),
        }
    if isinstance(state, ManifestExchange):
        return {
            "version": 1,
            "phase": "exchange",
            "registration": _serialize_registration(state.registration),
        }
    if isinstance(state, RegistrationRetry):
        return {
            "version": 1,
            "phase": "registration_retry",
            "context": _serialize_context(state.context),
        }
    if isinstance(state, PendingInstallation):
        return {
            "version": 1,
            "phase": "installation",
            "app": _serialize_app(state.app),
            "installation_state": state.correlation_state,
            "context": _serialize_context(state.context),
        }
    if isinstance(state, ConnectedInstallation):
        return {
            "version": 1,
            "phase": "connected",
            "app": _serialize_app(state.app),
            "installation": _serialize_installation(state.installation),
            "context": _serialize_context(state.context),
        }
    assert_never(state)


def _serialize_registration(registration: ManifestRegistration) -> dict[str, object]:
    return {
        "manifest_state": registration.correlation_state,
        "app_name": registration.app_name,
        "context": _serialize_context(registration.context),
    }


def _serialize_context(context: RegistrationContext) -> dict[str, object]:
    return {
        "organization": (context.organization.login if context.organization is not None else None),
        "possible_orphan_apps": [
            {
                "name": orphan.name,
                "organization": (
                    orphan.organization.login if orphan.organization is not None else None
                ),
            }
            for orphan in context.possible_orphan_apps
        ],
    }


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
    if set(payload) != {"version", "phase", "app", "installation_state", "context"}:
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
        context=_parse_context(payload["context"]),
    )


def _parse_connected(payload: dict[str, Any]) -> ConnectedInstallation:
    if set(payload) != {"version", "phase", "app", "installation", "context"}:
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
        context=_parse_context(payload["context"]),
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
    if set(payload) != {"version", "phase", "manifest_state", "app_name", "context"}:
        raise SetupStateError("GitHub manifest state has an unsupported format")
    _require_version(payload)
    if payload["phase"] != "manifest":
        raise SetupStateError("GitHub manifest state has an impossible phase")
    return _parse_registration(
        {
            "manifest_state": payload["manifest_state"],
            "app_name": payload["app_name"],
            "context": payload["context"],
        }
    )


def _parse_exchange(payload: dict[str, Any]) -> ManifestExchange:
    if set(payload) != {"version", "phase", "registration"}:
        raise SetupStateError("GitHub exchange state has an unsupported format")
    _require_version(payload)
    if payload["phase"] != "exchange":
        raise SetupStateError("GitHub exchange state has an impossible phase")
    return ManifestExchange(registration=_parse_registration(payload["registration"]))


def _parse_registration_retry(payload: dict[str, Any]) -> RegistrationRetry:
    if set(payload) != {"version", "phase", "context"}:
        raise SetupStateError("GitHub registration retry state has an unsupported format")
    _require_version(payload)
    if payload["phase"] != "registration_retry":
        raise SetupStateError("GitHub registration retry state has an impossible phase")
    return RegistrationRetry(context=_parse_context(payload["context"]))


def _parse_registration(payload: Any) -> ManifestRegistration:
    if not isinstance(payload, dict) or set(payload) != {
        "manifest_state",
        "app_name",
        "context",
    }:
        raise SetupStateError("GitHub manifest registration has an unsupported format")
    try:
        return ManifestRegistration(
            correlation_state=payload["manifest_state"],
            app_name=payload["app_name"],
            context=_parse_context(payload["context"]),
        )
    except (TypeError, ValueError) as error:
        raise SetupStateError("GitHub manifest registration is invalid") from error


def _parse_context(payload: Any) -> RegistrationContext:
    if not isinstance(payload, dict) or set(payload) != {
        "organization",
        "possible_orphan_apps",
    }:
        raise SetupStateError("GitHub registration context has an unsupported format")
    possible_orphan_apps = payload["possible_orphan_apps"]
    if not isinstance(possible_orphan_apps, list):
        raise SetupStateError("GitHub registration context has invalid orphan Apps")
    return RegistrationContext(
        organization=_parse_organization(payload["organization"]),
        possible_orphan_apps=tuple(
            _parse_possible_orphan(orphan) for orphan in possible_orphan_apps
        ),
    )


def _parse_possible_orphan(payload: Any) -> PossibleOrphanGitHubApp:
    if not isinstance(payload, dict) or set(payload) != {"name", "organization"}:
        raise SetupStateError("GitHub orphan App record has an unsupported format")
    try:
        return PossibleOrphanGitHubApp(
            name=payload["name"],
            organization=_parse_organization(payload["organization"]),
        )
    except (TypeError, ValueError) as error:
        raise SetupStateError("GitHub orphan App record is invalid") from error


def _parse_organization(payload: Any) -> GitHubOrganization | None:
    if payload is None:
        return None
    try:
        return GitHubOrganization(payload)
    except (TypeError, ValueError) as error:
        raise SetupStateError("GitHub registration context has an invalid organization") from error


def _require_version(payload: dict[str, Any]) -> None:
    if type(payload.get("version")) is not int or payload["version"] != 1:
        raise SetupStateError("GitHub setup state has an unsupported version")
