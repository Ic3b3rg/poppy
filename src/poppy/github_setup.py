import json
import os
import secrets
import tempfile
from pathlib import Path

from poppy.setup_session import SetupStateError

GITHUB_SETUP_STATE_FILE = "github-setup.json"


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
