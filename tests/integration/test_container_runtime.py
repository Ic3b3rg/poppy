import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

DOCKER = shutil.which("docker")
REPOSITORY_ROOT = Path(__file__).parents[2]

pytestmark = pytest.mark.container


def test_compose_configuration_is_valid_and_portable() -> None:
    docker = require_docker_daemon()
    environment = compose_environment()
    environment["POPPY_PUBLIC_BASE_URL"] = "https://poppy.example"
    compose = render_compose(docker, environment)
    service = compose["services"]["poppy"]

    assert service["environment"]["POPPY_PUBLIC_BASE_URL"] == "https://poppy.example"
    assert service["volumes"] == [
        {
            "type": "volume",
            "source": "poppy_data",
            "target": "/var/lib/poppy",
            "volume": {},
        }
    ]
    assert "secrets" not in service
    assert "secrets" not in compose
    assert "ports" not in service


def test_host_override_publishes_only_the_loopback_port() -> None:
    docker = require_docker_daemon()
    environment = compose_environment()
    environment["POPPY_PUBLIC_BASE_URL"] = "https://poppy.example"

    compose = render_compose(
        docker,
        environment,
        compose_files=("compose.yaml", "compose.host.yaml"),
    )

    assert compose["services"]["poppy"]["ports"] == [
        {
            "mode": "ingress",
            "host_ip": "127.0.0.1",
            "target": 8000,
            "published": "8000",
            "protocol": "tcp",
        }
    ]


def test_compose_uses_the_coolify_generated_https_origin() -> None:
    docker = require_docker_daemon()
    environment = compose_environment()
    environment["SERVICE_URL_POPPY_8000"] = "https://poppy.example"

    compose = render_compose(docker, environment)

    assert (
        compose["services"]["poppy"]["environment"]["POPPY_PUBLIC_BASE_URL"]
        == "https://poppy.example"
    )


def test_production_container_health_and_private_volume_survive_replacement() -> None:
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
        run_test_container(docker, image=image, container=container, volume=volume)
        assert_container_healthy_in_setup_mode(docker, container)
        write_durability_probes(docker, container)

        subprocess.run([docker, "stop", container], check=True, timeout=30)
        subprocess.run([docker, "start", container], check=True, timeout=30)
        assert_container_healthy_in_setup_mode(docker, container)
        assert_durability_probes(docker, container)

        subprocess.run([docker, "rm", "--force", container], check=True, timeout=30)
        run_test_container(docker, image=image, container=container, volume=volume)
        assert_container_healthy_in_setup_mode(docker, container)
        assert_durability_probes(docker, container)
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


def compose_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("POPPY_", "SERVICE_URL_POPPY_"))
    }
    environment["COMPOSE_DISABLE_ENV_FILE"] = "1"
    return environment


def render_compose(
    docker: str,
    environment: dict[str, str],
    *,
    compose_files: tuple[str, ...] = ("compose.yaml",),
) -> dict[str, object]:
    file_arguments = [item for path in compose_files for item in ("--file", path)]
    rendered = subprocess.run(
        [docker, "compose", *file_arguments, "config", "--format", "json"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    payload = json.loads(rendered.stdout)
    assert isinstance(payload, dict)
    return payload


def run_test_container(
    docker: str,
    *,
    image: str,
    container: str,
    volume: str,
) -> None:
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
            f"type=volume,source={volume},target=/var/lib/poppy",
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
            "POPPY_PUBLIC_BASE_URL=https://poppy.example",
            image,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


def assert_container_healthy_in_setup_mode(docker: str, container: str) -> None:
    health = wait_for_container_health(docker, container)
    if health != "healthy":
        pytest.fail(f"container health is {health}: {sanitized_container_logs(docker, container)}")

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


def write_durability_probes(docker: str, container: str) -> None:
    subprocess.run(
        [
            docker,
            "exec",
            container,
            "python",
            "-c",
            (
                "import os, stat; from pathlib import Path; "
                "root=Path('/var/lib/poppy'); "
                "assert stat.S_IMODE(root.stat().st_mode) == 0o700; "
                "assert root.stat().st_uid == os.getuid(); "
                "(root/'state-durability-probe').write_text('state'); "
                "(root/'codex'/'auth-durability-probe').write_text('auth')"
            ),
        ],
        check=True,
        timeout=10,
    )


def assert_durability_probes(docker: str, container: str) -> None:
    probe = subprocess.run(
        [
            docker,
            "exec",
            container,
            "python",
            "-c",
            (
                "from pathlib import Path; root=Path('/var/lib/poppy'); "
                "assert (root/'state-durability-probe').read_text() == 'state'; "
                "assert (root/'codex'/'auth-durability-probe').read_text() == 'auth'"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr


def sanitized_container_logs(docker: str, container: str) -> str:
    logs = subprocess.run(
        [docker, "logs", container],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    combined = f"{logs.stdout}\n{logs.stderr}"
    return re.sub(r"/setup\?token=[A-Za-z0-9_-]+", "/setup?token=[REDACTED]", combined)


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
