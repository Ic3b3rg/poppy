import json
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from poppy.adapters.github_onboarding import GitHubAppCredentials
from poppy.setup_session import SetupStateError

GITHUB_SETUP_STATE_FILE = "github-setup.json"


@dataclass(frozen=True, slots=True)
class PendingInstallation:
    app: GitHubAppCredentials
    correlation_state: str


class GitHubSetupStore:
    def __init__(self, *, data_dir: Path) -> None:
        self._state_file = data_dir / GITHUB_SETUP_STATE_FILE

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
        if (
            payload.get("phase") != "manifest"
            or not isinstance(payload.get("manifest_state"), str)
            or not secrets.compare_digest(payload["manifest_state"], presented_state)
        ):
            return False
        self._write_state({"version": 1, "phase": "exchange"})
        return True

    def await_installation(self, app: GitHubAppCredentials) -> PendingInstallation:
        payload = self._read_state()
        if payload != {"version": 1, "phase": "exchange"}:
            raise SetupStateError("GitHub setup state cannot accept App credentials")
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
