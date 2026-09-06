"""container/bootstrap.sh's --attach flag and its printed hint -- exercised
as a real subprocess with a fake `docker` on PATH (this dev environment has
no real dockerd to exec against), same "real subprocess, not a source read"
preference tests/test_install_script.py already establishes for shell
scripts in this tree.
"""

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "container" / "bootstrap.sh"


def _fake_docker(bin_dir: Path, log_path: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log_path}"\n'
        "exit 0\n"
    )
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _env_with_fake_docker(bin_dir: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def test_help_documents_attach():
    res = subprocess.run(
        ["bash", str(BOOTSTRAP), "--help"], capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "--attach" in res.stdout
    assert "--bind-ports" in res.stdout
    assert "--no-bind-ports" in res.stdout
    assert "--api-port" in res.stdout
    assert "--session-port" in res.stdout


def test_attach_execs_the_right_compose_command(tmp_path):
    log = tmp_path / "docker_calls.log"
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant", "--attach", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path),
        stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"

    calls = log.read_text().splitlines()
    # First call is bootstrap.sh's own `docker compose version` dependency
    # check; the exec'd attach command is always the last (and, because
    # `exec` replaces the process, final) invocation.
    call = calls[-1]
    assert "-p h-mesh-testpod-testtenant" in call
    assert "exec h-mesh" in call
    # The tenant name is tmux's session name inside the container -- must
    # reach the exec'd command, not get lost in the sh -c wrapping.
    assert call.rstrip().endswith("-- testtenant")
    assert 'TMUX_TMPDIR="$HOME/.h-mesh/tmux"' in call
    assert "tmux attach -t" in call


def test_attach_never_touches_agents_or_up(tmp_path):
    """--attach must not reach the AGENTS/DEFAULT_CLI prompts or `up` at
    all -- it's a read-only convenience for an already-running office, not
    a variant of standing one up."""
    log = tmp_path / "docker_calls.log"
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        ["bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant", "--attach"],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path),
        stdin=subprocess.DEVNULL,  # a prompt reachable by mistake would hang/fail here
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
    assert "up" not in log.read_text()


def test_up_prints_an_attach_hint_with_the_resolved_identity(tmp_path):
    log = tmp_path / "docker_calls.log"
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant", "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path),
        stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
    assert "--pod testpod --tenant testtenant --attach" in res.stdout
    assert "h-mesh-testpod-testtenant" in res.stdout
    assert "tmux attach -t testtenant" in res.stdout
    assert "compose.ports.yaml" in res.stdout


def test_noninteractive_default_binds_default_ports(tmp_path):
    log = tmp_path / "docker_calls.log"
    env_file = tmp_path / "office.env"
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
    assert "compose.ports.yaml" in log.read_text().splitlines()[-1]
    text = env_file.read_text()
    assert "H_MESH_BIND_PORTS=1" in text
    assert "API_PORT=8080" in text
    assert "SESSION_PORT=8081" in text


def test_no_bind_ports_omits_overlay_and_persists_choice(tmp_path):
    log = tmp_path / "docker_calls.log"
    env_file = tmp_path / "office.env"
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--no-bind-ports", "--skip-build",
            "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
    assert "compose.ports.yaml" not in log.read_text().splitlines()[-1]
    assert "H_MESH_BIND_PORTS=0" in env_file.read_text()
    assert "Host ports:   not published" in res.stdout


def test_existing_env_without_bind_key_remains_bound(tmp_path):
    log = tmp_path / "docker_calls.log"
    env_file = tmp_path / "office.env"
    env_file.write_text("POD=testpod\nTENANT=testtenant\nAPI_PORT=19080\nSESSION_PORT=19081\n")
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
    assert "compose.ports.yaml" in log.read_text().splitlines()[-1]
    text = env_file.read_text()
    assert "H_MESH_BIND_PORTS=1" in text
    assert "API_PORT=19080" in text
    assert "SESSION_PORT=19081" in text


def test_custom_port_flags_are_persisted_and_invalid_ports_fail(tmp_path):
    log = tmp_path / "docker_calls.log"
    env_file = tmp_path / "office.env"
    _fake_docker(tmp_path, log)
    base = [
        "bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant",
        "--env-file", str(env_file), "--non-interactive",
    ]

    res = subprocess.run(
        [*base, "--api-port", "18080", "--session-port", "18081"],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
    assert "API_PORT=18080" in env_file.read_text()
    assert "SESSION_PORT=18081" in env_file.read_text()

    res = subprocess.run(
        [*base, "--api-port", "65536"], capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res.returncode != 0
    assert "1 to 65535" in res.stderr
