from pathlib import Path

import yaml


def test_container_is_pinned_and_runs_as_unprivileged_user() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "python:3.14.7-slim-trixie@sha256:" in dockerfile
    assert "ARG UV_VERSION=0.12.7" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "chmod 700 /var/lib/poppy /var/lib/poppy/codex /var/empty/poppy" in dockerfile
    assert "USER poppy" in dockerfile
    assert 'ENTRYPOINT ["poppy"]' in dockerfile
    assert "COPY . " not in dockerfile


def test_compose_preserves_private_instance_data_and_hardens_the_container() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text())
    service = compose["services"]["poppy"]

    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert service["volumes"] == ["poppy_data:/var/lib/poppy"]
    assert compose["volumes"] == {"poppy_data": None}
    assert "secrets" not in service
    assert "secrets" not in compose
    assert not Path(".poppy-empty-secret").exists()
    assert service["environment"]["SERVICE_URL_POPPY_8000"] == "/"
    assert service["environment"]["POPPY_PUBLIC_BASE_URL"] == (
        "${POPPY_PUBLIC_BASE_URL:-${SERVICE_URL_POPPY_8000}}"
    )
    assert service["environment"]["POPPY_DATA_DIR"] == "/var/lib/poppy"
    assert service["environment"]["POPPY_CODEX_HOME"] == "/var/lib/poppy/codex"
    assert service["env_file"] == [{"path": ".env", "required": False}]
    assert service["expose"] == ["8000"]
    assert "ports" not in service
    assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=128m"]

    host_override = yaml.safe_load(Path("compose.host.yaml").read_text())
    assert host_override == {
        "services": {"poppy": {"ports": ["127.0.0.1:${POPPY_HOST_PORT:-8000}:8000"]}}
    }


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


def test_ci_runs_every_required_quality_gate_without_secrets() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text()
    parsed = yaml.safe_load(workflow)

    assert parsed["permissions"] == {"contents": "read"}
    assert "pull_request:" in workflow
    assert "push:" in workflow
    assert "3d3c42e5aac5ba805825da76410c181273ba90b1" in workflow
    assert "5fda3b95a4ea91299a34e894583c3862153e4b97" in workflow
    assert "20cfd1bf945f4377ade1205e4dbc17946fc9a30d" in workflow
    for command in (
        "uv sync --frozen",
        "uv run --frozen pytest",
        "uv run --frozen mypy src",
        "uv run --frozen ruff check .",
        "uv run --frozen ruff format --check .",
        "uv run --frozen pip-audit --local --skip-editable",
        "uv build --no-sources",
        "uv run --frozen pytest -m container tests/integration/test_container_runtime.py",
    ):
        assert command in workflow


def test_coolify_guide_covers_the_complete_operator_path() -> None:
    guide = Path("docs/coolify.md").read_text()

    for instruction in (
        "+ New",
        "Public Repository",
        "https://github.com/Ic3b3rg/poppy",
        "Docker Compose",
        "Base Directory",
        "Docker Compose Location",
        "/compose.yaml",
        "Domains",
        ":8000",
        "Persistent Storages",
        "poppy_data",
        "/var/lib/poppy",
        "Deploy",
        "Logs",
        "Poppy Setup Session",
        "Register Poppy on GitHub",
        "Only select repositories",
        "Connect ChatGPT",
        "poppy:size/",
        "Formal Review",
        "Poppy / PR Review",
        "Stop",
        "Start",
        "Redeploy",
        "Troubleshooting",
    ):
        assert instruction in guide

    assert "manual webhook secret" not in guide.lower()
    assert "openai api key" not in guide.lower()
    assert "cloudflare" not in guide.lower()
