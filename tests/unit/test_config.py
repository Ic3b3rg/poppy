from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from poppy.config import PoppySettings, read_github_private_key
from poppy.domain.review import Severity


def test_settings_have_safe_review_defaults(tmp_path: Path) -> None:
    key_file = tmp_path / "github-app.pem"
    key_file.write_text("test-private-key")

    settings = PoppySettings(
        github_app_id=123,
        github_private_key_file=key_file,
        github_webhook_secret=SecretStr("a-secret-that-is-long-enough"),
        _env_file=None,
    )

    assert settings.model == "gpt-5.6-luna"
    assert settings.blocking_severity is Severity.HIGH
    assert settings.concurrency == 3
    assert settings.context_limit_characters == 160_000
    assert settings.review_timeout_seconds == 300
    assert read_github_private_key(settings.github_private_key_file) == "test-private-key"
    assert "a-secret-that-is-long-enough" not in repr(settings)


@pytest.mark.parametrize("context_limit", [39_999, 40_000])
def test_context_limit_cannot_remove_reserved_prompt_space(
    tmp_path: Path,
    context_limit: int,
) -> None:
    key_file = tmp_path / "github-app.pem"
    key_file.write_text("test-private-key")

    with pytest.raises(ValidationError):
        PoppySettings(
            github_app_id=123,
            github_private_key_file=key_file,
            github_webhook_secret=SecretStr("a-secret-that-is-long-enough"),
            context_limit_characters=context_limit,
            _env_file=None,
        )


def test_private_key_must_be_a_readable_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="private key"):
        read_github_private_key(tmp_path / "missing.pem")


@pytest.mark.parametrize(
    "public_base_url",
    [
        "http://poppy.example",
        "https://poppy.example/nested",
        "https://poppy.example/?query=value",
        "https://poppy.example/#fragment",
    ],
)
def test_public_base_url_is_a_complete_https_origin(public_base_url: str) -> None:
    with pytest.raises(ValidationError):
        PoppySettings(public_base_url=public_base_url, _env_file=None)


def test_empty_portable_url_falls_back_when_coolify_magic_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POPPY_PUBLIC_BASE_URL", "")

    settings = PoppySettings(_env_file=None)

    assert str(settings.public_base_url) == "https://localhost/"


def test_partial_legacy_reviewer_credentials_are_rejected() -> None:
    with pytest.raises(ValidationError, match="configured together"):
        PoppySettings(github_app_id=123, _env_file=None)
