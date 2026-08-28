import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

DOCKER = shutil.which("docker")
REPOSITORY_ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.container


def test_compose_configuration_is_valid() -> None:
    docker = require_docker_daemon()
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("POPPY_", "SERVICE_URL_POPPY_"))
    }
    environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    environment["POPPY_PUBLIC_BASE_URL"] = "https://poppy.example"

    subprocess.run(
        [docker, "compose", "--file", "compose.yaml", "config", "--quiet"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=True,
        timeout=30,
    )


def test_production_container_starts_healthy_in_setup_mode() -> None:
    docker = require_docker_daemon()
    suffix = uuid.uuid4().hex
    image = f"poppy-setup-test:{suffix}"
    container = f"poppy-setup-test-{suffix}"
    volume = f"poppy-setup-test-{suffix}"

    try:
        subprocess.run(
            [docker, "build", "--tag", image, "."],
            cwd=REPOSITORY_ROOT,
            check=True,
            timeout=300,
        )
        subprocess.run(
            [
                docker,
                "run",
                "--detach",
                "--name",
                container,
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=128m",
                "--mount",
                f"type=volume,source={volume},target=/var/lib/poppy/codex",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges=true",
                "--health-start-period",
                "0s",
                "--health-interval",
                "1s",
                "--health-timeout",
                "3s",
                "--health-retries",
                "10",
                "--env",
                "POPPY_DATA_DIR=/var/lib/poppy/codex",
                "--env",
                "POPPY_PUBLIC_BASE_URL=https://poppy.example",
                image,
            ],
            check=True,
            timeout=30,
        )

        health = wait_for_container_health(docker, container)
        if health != "healthy":
            logs = subprocess.run(
                [docker, "logs", container],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            pytest.fail(f"container health is {health}:\n{logs.stdout}\n{logs.stderr}")

        response = subprocess.run(
            [
                docker,
                "exec",
                container,
                "python",
                "-c",
                (
                    "import urllib.request; "
                    "print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read().decode())"
                ),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert json.loads(response.stdout) == {"status": "ok", "mode": "setup"}
    finally:
        subprocess.run(
            [docker, "rm", "--force", container],
            capture_output=True,
            check=False,
            timeout=30,
        )
        subprocess.run(
            [docker, "volume", "rm", "--force", volume],
            capture_output=True,
            check=False,
            timeout=30,
        )
        subprocess.run(
            [docker, "image", "rm", "--force", image],
            capture_output=True,
            check=False,
            timeout=30,
        )


def require_docker_daemon() -> str:
    if DOCKER is None:
        pytest.fail("the container verification job requires the Docker CLI")
    daemon = subprocess.run(
        [DOCKER, "info"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if daemon.returncode != 0:
        pytest.fail(f"the container verification job requires a Docker daemon:\n{daemon.stderr}")
    return DOCKER


def wait_for_container_health(docker: str, container: str) -> str:
    deadline = time.monotonic() + 20
    health = "starting"
    while time.monotonic() < deadline:
        inspected = subprocess.run(
            [
                docker,
                "inspect",
                "--format",
                "{{.State.Health.Status}}",
                container,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        health = inspected.stdout.strip()
        if health in {"healthy", "unhealthy"}:
            return health
        time.sleep(0.25)
    return health
