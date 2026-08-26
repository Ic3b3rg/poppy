from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from poppy.adapters.codex import DEFAULT_MODEL
from poppy.domain.review import Severity

CONTEXT_OVERHEAD_RESERVE = 40_000


class CodexHomeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POPPY_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    codex_home: Path = Path("/var/lib/poppy/codex")


class PoppySettings(CodexHomeSettings):
    github_app_id: int = Field(gt=0)
    github_private_key_file: Path
    github_webhook_secret: SecretStr = Field(min_length=16)

    codex_workspace: Path = Path("/var/empty/poppy")
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=100)
    review_language: str = Field(default="Italian", min_length=1, max_length=80)
    review_tone: str = Field(default="concise and direct", min_length=1, max_length=160)
    blocking_severity: Severity = Severity.HIGH
    context_limit_characters: int = Field(default=160_000, gt=CONTEXT_OVERHEAD_RESERVE)
    review_timeout_seconds: float = Field(default=300, ge=10, le=3_600)
    concurrency: int = Field(default=3, ge=1, le=10)

    listen_host: str = "0.0.0.0"
    listen_port: int = Field(default=8_000, ge=1, le=65_535)
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"


def read_github_private_key(path: Path) -> str:
    try:
        if not path.is_file():
            raise ValueError("GitHub private key path is not a file")
        private_key = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError("GitHub private key file is not readable") from error
    if not private_key.strip():
        raise ValueError("GitHub private key file is empty")
    return private_key
