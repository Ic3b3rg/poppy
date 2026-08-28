from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from poppy.adapters.codex import DEFAULT_MODEL
from poppy.domain.review import Severity

CONTEXT_OVERHEAD_RESERVE = 40_000


@dataclass(frozen=True)
class ReviewerCredentials:
    app_id: int
    private_key_file: Path
    webhook_secret: SecretStr


class CodexHomeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POPPY_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    codex_home: Path = Path("/var/lib/poppy/codex")


class PoppySettings(CodexHomeSettings):
    github_app_id: int | None = Field(default=None, gt=0)
    github_private_key_file: Path | None = None
    github_webhook_secret: SecretStr | None = Field(default=None, min_length=16)

    data_dir: Path = Path("/var/lib/poppy")
    public_base_url: HttpUrl = HttpUrl("https://localhost")
    setup_bootstrap_ttl_seconds: int = Field(default=86_400, ge=60, le=604_800)
    setup_session_ttl_seconds: int = Field(default=604_800, ge=300, le=2_592_000)
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

    @field_validator("public_base_url")
    @classmethod
    def require_https_origin(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.scheme != "https"
            or value.path not in {"", "/"}
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError("Public base URL must be a complete HTTPS origin")
        return value

    @model_validator(mode="after")
    def reject_partial_reviewer_credentials(self) -> "PoppySettings":
        provided = (
            self.github_app_id is not None,
            self.github_private_key_file is not None,
            self.github_webhook_secret is not None,
        )
        if any(provided) and not all(provided):
            raise ValueError("GitHub reviewer credentials must be configured together")
        return self

    def reviewer_credentials(self) -> ReviewerCredentials | None:
        if (
            self.github_app_id is None
            or self.github_private_key_file is None
            or self.github_webhook_secret is None
        ):
            return None
        return ReviewerCredentials(
            app_id=self.github_app_id,
            private_key_file=self.github_private_key_file,
            webhook_secret=self.github_webhook_secret,
        )


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
