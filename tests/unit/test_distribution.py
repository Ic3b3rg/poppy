from pathlib import Path


def test_container_is_pinned_and_runs_as_unprivileged_user() -> None:
    dockerfile = Path("Dockerfile").read_text()

    assert "python:3.14.7-slim-trixie@sha256:" in dockerfile
    assert "ARG UV_VERSION=0.10.10" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    assert "chmod 700 /var/lib/poppy/codex /var/empty/poppy" in dockerfile
    assert "USER poppy" in dockerfile
    assert 'ENTRYPOINT ["poppy"]' in dockerfile
    assert "COPY . " not in dockerfile


def test_compose_preserves_only_codex_auth_and_hardens_the_container() -> None:
    compose = Path("compose.yaml").read_text()

    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "poppy_codex:/var/lib/poppy/codex" in compose
    assert "./secrets/github-app.pem" in compose
    assert "127.0.0.1:${POPPY_HOST_PORT:-8000}:8000" in compose


def test_build_context_excludes_credentials_and_local_state() -> None:
    ignored = Path(".dockerignore").read_text().splitlines()

    assert ".env" in ignored
    assert "*.pem" in ignored
    assert "auth.json" in ignored
    assert "secrets" in ignored
    assert ".git" in ignored
