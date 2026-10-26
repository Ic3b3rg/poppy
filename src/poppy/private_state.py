import json
import os
import tempfile
from pathlib import Path
from typing import Any


class PrivateStateFileNotRegularError(RuntimeError):
    """The private state path is unsafe to read."""


def read_private_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise PrivateStateFileNotRegularError
    path.chmod(0o600)
    return json.loads(path.read_text(encoding="utf-8"))


def write_private_json_atomically(path: Path, payload: object) -> None:
    directory = path.parent
    temporary_path: Path | None = None
    try:
        file_descriptor, raw_temporary_path = tempfile.mkstemp(
            dir=directory,
            prefix=f".{path.name}.",
        )
        temporary_path = Path(raw_temporary_path)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, separators=(",", ":"))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(directory, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
