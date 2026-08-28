from pathlib import Path

import yaml


def test_container_is_pinned_and_runs_as_unprivileged_user() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "python:3.14.7-slim-trixie@sha256:" in dockerfile
    assert "ARG UV_VERSION=0.10.10" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "chmod 700 /var/lib/poppy/codex /var/empty/poppy" in dockerfile
    assert "USER poppy" in dockerfile
    assert 'ENTRYPOINT ["poppy"]' in dockerfile
    assert "COPY . " not in dockerfile


def test_compose_preserves_private_instance_data_and_hardens_the_container() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    service = compose["services"]["poppy"]

    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert service["volumes"] == ["poppy_codex:/var/lib/poppy/codex"]
    assert compose["volumes"] == {"poppy_codex": None}
    assert service["secrets"] == ["github_app_private_key"]
    assert compose["secrets"] == {
        "github_app_private_key": {
            "file": "${POPPY_GITHUB_PRIVATE_KEY_SOURCE:-./.poppy-empty-secret}"
        }
    }
    assert Path(".poppy-empty-secret").read_bytes() == b""
    assert service["environment"]["SERVICE_URL_POPPY_8000"] == "/"
    assert service["environment"]["POPPY_PUBLIC_BASE_URL"] == (
        "${POPPY_PUBLIC_BASE_URL:-${SERVICE_URL_POPPY_8000}}"
    )
    assert service["environment"]["POPPY_DATA_DIR"] == "/var/lib/poppy/codex"
    assert service["environment"]["POPPY_CODEX_HOME"] == "/var/lib/poppy/codex"
    assert service["env_file"] == [{"path": ".env", "required": False}]
    assert service["ports"] == ["127.0.0.1:${POPPY_HOST_PORT:-8000}:8000"]
    assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=128m"]


def test_build_context_excludes_credentials_and_local_state() -> None:
    ignored = Path(".dockerignore").read_text().splitlines()

    assert ".env" in ignored
    assert "*.pem" in ignored
    assert "auth.json" in ignored
    assert "secrets" in ignored
    assert ".git" in ignored


def test_example_environment_starts_without_integration_credentials() -> None:
    environment = dict(
        line.split("=", maxsplit=1)
        for line in Path(".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    )

    assert "POPPY_PUBLIC_BASE_URL" in environment
    assert "POPPY_GITHUB_APP_ID" not in environment
    assert "POPPY_GITHUB_WEBHOOK_SECRET" not in environment
