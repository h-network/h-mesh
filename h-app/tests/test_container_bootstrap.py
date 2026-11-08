"""container/bootstrap.sh's --attach flag and its printed hint -- exercised
as a real subprocess with a fake `docker` on PATH (this dev environment has
no real dockerd to exec against), same "real subprocess, not a source read"
preference tests/test_install_script.py already establishes for shell
scripts in this tree.
"""

import os
import pty
import re
import select
import stat
import subprocess
import time
from pathlib import Path

import pytest

from _tls_test_material import tls_host_uid

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "container" / "bootstrap.sh"
ENTRYPOINT = REPO_ROOT / "container" / "entrypoint.sh"
COMPOSE_YAML = REPO_ROOT / "container" / "compose.yaml"
COMPOSE_PORTS_YAML = REPO_ROOT / "container" / "compose.ports.yaml"


def _bootstrap_args(env_file):
    return ["bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--skip-build"]


def _api_token(env_file):
    return next(line.partition("=")[2] for line in env_file.read_text().splitlines()
                if line.startswith("API_TOKEN="))


@pytest.mark.parametrize("initial", [None, ""], ids=["missing", "empty"])
def test_bootstrap_generates_private_api_token_without_telegram_and_keeps_it(tmp_path, initial):
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path, log)
    env_file = tmp_path / ".env"
    if initial is not None:
        env_file.write_text("API_TOKEN=\nCUSTOM=keep-me\n")
        env_file.chmod(0o644)
    env = _env_with_fake_docker(tmp_path)
    first = subprocess.run([*_bootstrap_args(env_file), "--non-interactive"],
                           env=env, capture_output=True, text=True, timeout=10)
    assert first.returncode == 0, first.stderr
    token = _api_token(env_file)
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert token not in first.stdout + first.stderr + log.read_text()
    assert token not in (tmp_path / "SUMMARY.md").read_text()
    assert token not in (tmp_path / "attach.sh").read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "TELEGRAM_BOT_TOKEN=" not in env_file.read_text()
    # A live credential from another office must not rotate this identity.
    env["API_TOKEN"] = "different-ambient-office-token"
    second = subprocess.run([*_bootstrap_args(env_file), "--non-interactive"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert second.returncode == 0, second.stderr
    assert _api_token(env_file) == token
    assert token not in second.stdout + second.stderr
    if initial is not None:
        assert "CUSTOM=keep-me" in env_file.read_text()


@pytest.mark.parametrize("source", ["file", "environment"])
def test_bootstrap_preserves_supplied_api_token_without_printing_it(tmp_path, source):
    _fake_docker(tmp_path, tmp_path / "docker.log")
    env_file = tmp_path / ".env"
    env = _env_with_fake_docker(tmp_path)
    token = "explicit-test-api-secret"
    env["H_MESH_ALLOW_PLAINTEXT"] = "1"
    if source == "file":
        env_file.write_text(f"API_TOKEN={token}\n")
    else:
        env["API_TOKEN"] = token
    res = subprocess.run([*_bootstrap_args(env_file), "--non-interactive"],
                         env=env, capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stderr
    assert _api_token(env_file) == token
    assert token not in res.stdout + res.stderr
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "H_MESH_ALLOW_PLAINTEXT=1" in env_file.read_text()


def test_bootstrap_noninteractive_loopback_enablement_is_last_and_in_summary(tmp_path):
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path, log)
    env_file = tmp_path / ".env"
    res = subprocess.run([*_bootstrap_args(env_file), "--non-interactive", "--api-port", "18090"],
                         env=_env_with_fake_docker(tmp_path), capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stderr
    assert "up -d" in log.read_text()
    enablement = res.stdout.strip().splitlines()[-1]
    assert "published API port 18090 is closed" in enablement
    assert "API_TLS_CERT and API_TLS_KEY" in enablement
    assert "H_MESH_ALLOW_PLAINTEXT=1" in enablement
    summary = (tmp_path / "SUMMARY.md").read_text()
    assert enablement in summary
    assert str(tmp_path / "attach.sh") in summary
    assert f"{BOOTSTRAP} --pod testpod --tenant testtenant --attach" in summary
    assert "H_MESH_ALLOW_PLAINTEXT=" not in env_file.read_text()


def test_bootstrap_random_source_failure_never_starts_without_api_token(tmp_path):
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path, log)
    od = tmp_path / "od"
    od.write_text("#!/bin/sh\nexit 1\n")
    od.chmod(0o755)
    env_file = tmp_path / ".env"
    res = subprocess.run([*_bootstrap_args(env_file), "--non-interactive"],
                         env=_env_with_fake_docker(tmp_path), capture_output=True, text=True, timeout=10)
    assert res.returncode != 0
    assert "up -d" not in log.read_text()
    assert "API_TOKEN=" not in env_file.read_text()


def _bootstrap_wizard(env_file, env, answers):
    master, slave = pty.openpty()
    proc = subprocess.Popen(_bootstrap_args(env_file), env=env,
                            stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b""
    index = 0
    deadline = time.monotonic() + 15
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if output.rstrip().endswith(b":") and index < len(answers):
                    os.write(master, (answers[index] + "\n").encode())
                    index += 1
            elif proc.poll() is not None:
                break
        proc.wait(timeout=1)
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    return output.decode(errors="replace"), proc.returncode


@pytest.mark.parametrize("transport", ["plaintext", "tls"])
def test_bootstrap_interactive_without_telegram_explicitly_selects_transport(tmp_path, transport, tls_host_uid):
    from _tls_test_material import certificate_pair
    cert, key = certificate_pair(tmp_path)
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path, log)
    env_file = tmp_path / ".env"
    # agents, CLI, office network, publish, API port, session port, accounts, OAuth, local provider, Telegram
    answers = ["", "", "", "", "", "", "", "", "", ""]
    answers += ["", "y"] if transport == "plaintext" else [str(cert), str(key), ""]
    output, code = _bootstrap_wizard(env_file, _env_with_fake_docker(tmp_path), answers)
    assert code == 0, output
    assert "Choose how to reach its published port" in output
    assert "up -d" in log.read_text()
    assert "TELEGRAM_BOT_TOKEN=" not in env_file.read_text()
    assert _api_token(env_file) not in output
    assert "loopback-only" not in output
    if transport == "plaintext":
        assert "H_MESH_ALLOW_PLAINTEXT=1" in env_file.read_text()
        assert "Accept plaintext for this container?" in output
    else:
        assert "API_TLS_CERT=/run/h-mesh/tls/api.crt" in env_file.read_text()
        assert "API_TLS_KEY=/run/h-mesh/tls/api.key" in env_file.read_text()
        assert "H_MESH_ALLOW_PLAINTEXT=" not in env_file.read_text()


def test_bootstrap_interactive_declining_plaintext_requires_tls(tmp_path):
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path, log)
    env_file = tmp_path / ".env"
    output, code = _bootstrap_wizard(env_file, _env_with_fake_docker(tmp_path), [""] * 12)
    assert code != 0
    assert "choose TLS or explicitly accept plaintext" in output
    assert "up -d" not in log.read_text()
    assert "H_MESH_ALLOW_PLAINTEXT=" not in env_file.read_text()


def _fake_docker(bin_dir: Path, log_path: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log_path}"\n'
        "exit 0\n"
    )
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


_SCRUBBED_BOOTSTRAP_ENV_VARS = (
    "H_MESH_TENANT_ENV_FILE",
    "POD",
    "TENANT",
    "AGENTS",
    "DEFAULT_CLI",
    "H_MESH_OFFICE_NETWORK",
    "H_MESH_OFFICE_ALIAS",
    "OFFICE_NETWORK",
    "H_MESH_BIND_PORTS",
    "API_PORT",
    "SESSION_PORT",
    "ACCOUNTS",
    "DEFAULT_ACCOUNT",
    "AGENT_PROVIDERS",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "API_TOKEN",
    "API_TLS_CERT",
    "API_TLS_KEY",
    "SESSION_TLS_CERT",
    "SESSION_TLS_KEY",
    "H_MESH_ALLOW_PLAINTEXT",
    "H_MESH_CA_CERT",
    "H_MESH_TLS_DIR",
)


def _env_with_fake_docker(bin_dir: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    for key in _SCRUBBED_BOOTSTRAP_ENV_VARS:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(("CLAUDE_OAUTH_TOKEN_", "TELEGRAM_", "PROVIDER_")):
            del env[key]
    return env


def test_help_documents_attach():
    res = subprocess.run(
        ["bash", str(BOOTSTRAP), "--help"], capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "--attach" in res.stdout
    assert "--office-network" in res.stdout
    assert "--no-office-network" in res.stdout
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
    env_file = tmp_path / "office.env"
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
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
    assert not (REPO_ROOT / "offices" / "testpod" / "testtenant" / ".env").exists()


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


def test_existing_env_without_bind_key_remains_bound(tmp_path, monkeypatch):
    log = tmp_path / "docker_calls.log"
    env_file = tmp_path / "office.env"
    env_file.write_text("POD=testpod\nTENANT=testtenant\nAPI_PORT=19080\nSESSION_PORT=19081\n")
    _fake_docker(tmp_path, log)

    leaked_names = (*_SCRUBBED_BOOTSTRAP_ENV_VARS, "CLAUDE_OAUTH_TOKEN_DEFAULT", "CLAUDE_OAUTH_TOKEN_ACCOUNT_2")
    for name in leaked_names:
        monkeypatch.setenv(name, "1")
    helper_env = _env_with_fake_docker(tmp_path)
    for name in leaked_names:
        assert name not in helper_env
    assert not any(k.startswith("CLAUDE_OAUTH_TOKEN_") for k in helper_env)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=helper_env, stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
    assert "compose.ports.yaml" in log.read_text().splitlines()[-1]
    text = env_file.read_text()
    assert "H_MESH_BIND_PORTS=1" in text
    assert "API_PORT=19080" in text
    assert "SESSION_PORT=19081" in text
    assert "CLAUDE_OAUTH_TOKEN_" not in text


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


def test_noninteractive_summary_reports_telegram_from_env_file(tmp_path, tls_host_uid):
    from _tls_test_material import certificate_pair
    cert, key = certificate_pair(tmp_path)
    log = tmp_path / "docker_calls.log"
    env_file = tmp_path / "office.env"
    env_file.write_text(
        "POD=testpod\n"
        "TENANT=testtenant\n"
        "TELEGRAM_BOT_TOKEN=tg-secret-token\n"
        "TELEGRAM_CHAT_ID=424242\n"
        "H_MESH_ALLOW_PLAINTEXT=1\n"
    )
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
    assert "Telegram:     enabled, chat id 424242" in res.stdout
    assert "API/session:  plaintext (H_MESH_ALLOW_PLAINTEXT=1)" in res.stdout

    env_file.write_text(
        "POD=testpod\n"
        "TENANT=testtenant\n"
        "TELEGRAM_BOT_TOKEN=tg-secret-token\n"
        "TELEGRAM_CHAT_ID=424242\n"
        f"API_TLS_CERT={cert}\n"
        f"API_TLS_KEY={key}\n"
    )
    res_tls = subprocess.run(
        [
            "bash", str(BOOTSTRAP), "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res_tls.returncode == 0, f"stdout: {res_tls.stdout}\nstderr: {res_tls.stderr}"
    assert "Telegram:     enabled, chat id 424242" in res_tls.stdout
    assert "API/session:  TLS (/run/h-mesh/tls/api.crt)" in res_tls.stdout


def test_bootstrap_writes_office_summary_and_attach_script_without_secrets(tmp_path):
    log = tmp_path / "docker_calls.log"
    office_dir = tmp_path / "offices" / "testpod" / "testtenant"
    office_dir.mkdir(parents=True)
    env_file = office_dir / ".env"
    fake_oauth_default = "fake-oauth-secret-value-111"
    fake_oauth_backup = "fake-oauth-secret-value-222"
    fake_tg_token = "fake-telegram-bot-secret-333"
    fake_tg_chat = "987654321"
    fake_api_token = "fake-api-token-secret-444"
    env_file.write_text(
        "POD=testpod\n"
        "TENANT=testtenant\n"
        "AGENTS=architect,worker\n"
        "DEFAULT_CLI=codex\n"
        "ACCOUNTS=default,backup\n"
        "DEFAULT_ACCOUNT=backup\n"
        f"CLAUDE_OAUTH_TOKEN_DEFAULT={fake_oauth_default}\n"
        f"CLAUDE_OAUTH_TOKEN_BACKUP={fake_oauth_backup}\n"
        f"TELEGRAM_BOT_TOKEN={fake_tg_token}\n"
        f"TELEGRAM_CHAT_ID={fake_tg_chat}\n"
        f"API_TOKEN={fake_api_token}\n"
        "H_MESH_ALLOW_PLAINTEXT=1\n"
    )
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant",
            "--api-port", "18080", "--session-port", "18081",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"

    summary_file = office_dir / "SUMMARY.md"
    attach_script = office_dir / "attach.sh"
    assert summary_file.is_file()
    assert attach_script.is_file()
    assert stat.S_IMODE(summary_file.stat().st_mode) == 0o644
    assert stat.S_IMODE(attach_script.stat().st_mode) == 0o755

    summary_text = summary_file.read_text()
    attach_text = attach_script.read_text()

    for secret in (fake_oauth_default, fake_oauth_backup, fake_tg_token, fake_tg_chat, fake_api_token):
        assert secret not in summary_text
        assert secret not in attach_text

    assert "testpod" in summary_text
    assert "testtenant" in summary_text
    assert "architect,worker" in summary_text
    assert "codex" in summary_text
    assert "default,backup" in summary_text
    assert "2/2 configured" in summary_text
    assert "API 18080, session 18081" in summary_text
    assert "enabled" in summary_text
    assert "plaintext (H_MESH_ALLOW_PLAINTEXT=1)" in summary_text
    assert "h-mesh-testpod-testtenant" in summary_text
    assert str(attach_script) in summary_text
    assert "logs -f" in summary_text
    assert "down" in summary_text
    assert "up -d" in summary_text
    assert "h-mesh-upgrade" in summary_text

    assert "-p \"h-mesh-testpod-testtenant\"" in attach_text or "-p h-mesh-testpod-testtenant" in attach_text
    assert str(REPO_ROOT / "container" / "compose.yaml") in attach_text
    assert str(env_file) in attach_text
    assert 'TMUX_TMPDIR="$HOME/.h-mesh/tmux"' in attach_text
    assert "tmux attach -t" in attach_text

    log.write_text("")
    attach_res = subprocess.run(
        [str(attach_script)],
        cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert attach_res.returncode == 0, f"stdout: {attach_res.stdout}\nstderr: {attach_res.stderr}"
    attach_call = log.read_text().splitlines()[-1]
    assert "-p h-mesh-testpod-testtenant" in attach_call
    assert f"--env-file {env_file}" in attach_call
    assert "exec h-mesh" in attach_call
    assert 'TMUX_TMPDIR="$HOME/.h-mesh/tmux"' in attach_call
    assert attach_call.rstrip().endswith("-- testtenant")

    # Regenerated on subsequent runs while preserving custom .env entries.
    res_rerun = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant",
            "--no-bind-ports",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res_rerun.returncode == 0, f"stdout: {res_rerun.stdout}\nstderr: {res_rerun.stderr}"
    assert "not published" in summary_file.read_text()
    assert "compose.ports.yaml" not in attach_script.read_text()
    assert f"API_TOKEN={fake_api_token}" in env_file.read_text()


def test_compose_and_entrypoint_pin_internal_ports_when_custom_host_ports_are_set(tmp_path):
    log = tmp_path / "docker_calls.log"
    env_file = tmp_path / "office.env"
    _fake_docker(tmp_path, log)

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant",
            "--api-port", "18080", "--session-port", "18081",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
    env_text = env_file.read_text()
    assert "API_PORT=18080" in env_text
    assert "SESSION_PORT=18081" in env_text

    compose_text = COMPOSE_YAML.read_text()
    ports_text = COMPOSE_PORTS_YAML.read_text()
    assert 'API_PORT: "8080"' in compose_text
    assert 'SESSION_PORT: "8081"' in compose_text
    assert '"${API_PORT:-8080}:8080"' in ports_text
    assert '"${SESSION_PORT:-8081}:8081"' in ports_text

    redis_ports_log = tmp_path / "redis_ports.log"
    fake_redis_server = tmp_path / "redis-server"
    fake_redis_server.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s:%s\\n" "$API_PORT" "$SESSION_PORT" > "{redis_ports_log}"\n'
        "exit 0\n"
    )
    fake_redis_server.chmod(0o755)
    fake_redis_cli = tmp_path / "redis-cli"
    fake_redis_cli.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_redis_cli.chmod(0o755)

    ep_env = _env_with_fake_docker(tmp_path)
    ep_env.update({
        "POD": "testpod",
        "TENANT": "testtenant",
        "API_PORT": "18080",
        "SESSION_PORT": "18081",
        "REDIS_DATA_DIR": str(tmp_path / "redis-data"),
        "H_MESH_RUN_DIR": str(tmp_path / "run"),
    })
    ep_res = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        capture_output=True, text=True, timeout=10,
        env=ep_env, stdin=subprocess.DEVNULL,
    )
    assert ep_res.returncode != 0
    assert "redis exited during startup" in ep_res.stdout
    assert redis_ports_log.read_text().strip() == "8080:8081"


def test_entrypoint_preflight_applies_when_api_token_is_set_without_telegram(tmp_path):
    redis_called = tmp_path / "redis_called.log"
    fake_redis_server = tmp_path / "redis-server"
    fake_redis_server.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s:%s\\n" "$API_BIND" "$SESSION_BIND" > "{redis_called}"\n'
        "exit 0\n"
    )
    fake_redis_server.chmod(0o755)
    fake_redis_cli = tmp_path / "redis-cli"
    fake_redis_cli.write_text("#!/usr/bin/env bash\nexit 1\n")
    fake_redis_cli.chmod(0o755)

    base_env = _env_with_fake_docker(tmp_path)
    base_env.update({
        "POD": "testpod",
        "TENANT": "testtenant",
        "API_TOKEN": "standalone-api-secret",
        "API_BIND": "0.0.0.0",
        "SESSION_BIND": "0.0.0.0",
        "REDIS_DATA_DIR": str(tmp_path / "redis-data"),
        "H_MESH_RUN_DIR": str(tmp_path / "run"),
    })
    base_env.pop("TELEGRAM_BOT_TOKEN", None)
    base_env.pop("TELEGRAM_CHAT_ID", None)
    base_env.pop("H_MESH_ALLOW_PLAINTEXT", None)
    base_env.pop("API_TLS_CERT", None)
    base_env.pop("API_TLS_KEY", None)

    loopback = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        capture_output=True, text=True, timeout=10,
        env=base_env, stdin=subprocess.DEVNULL,
    )
    # The fake Redis exits, but transport preflight must allow the office
    # to start, selecting safe loopback listeners before Redis is reached.
    assert "loopback-only" in loopback.stdout
    assert "H_MESH_ALLOW_PLAINTEXT=1" in loopback.stdout
    assert redis_called.read_text().strip() == "127.0.0.1:127.0.0.1"
    redis_called.unlink()

    plaintext_env = {**base_env, "H_MESH_ALLOW_PLAINTEXT": "1"}
    allowed_plaintext = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        capture_output=True, text=True, timeout=10,
        env=plaintext_env, stdin=subprocess.DEVNULL,
    )
    assert "refusing to start" not in allowed_plaintext.stdout
    assert "loopback-only" not in allowed_plaintext.stdout
    assert redis_called.read_text().strip() == "0.0.0.0:0.0.0.0"

    redis_called.unlink()
    tls_env = {
        **base_env,
        "API_TLS_CERT": "/etc/ssl/certs/api.pem",
        "API_TLS_KEY": "/etc/ssl/private/api.key",
    }
    allowed_tls = subprocess.run(
        ["bash", str(ENTRYPOINT)],
        capture_output=True, text=True, timeout=10,
        env=tls_env, stdin=subprocess.DEVNULL,
    )
    assert "refusing to start" not in allowed_tls.stdout
    assert "loopback-only" not in allowed_tls.stdout
    assert redis_called.read_text().strip() == "0.0.0.0:0.0.0.0"

    log = tmp_path / "docker_calls.log"
    _fake_docker(tmp_path, log)
    env_file = tmp_path / "office" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "POD=testpod\n"
        "TENANT=testtenant\n"
        "API_TOKEN=standalone-api-secret\n"
        "H_MESH_ALLOW_PLAINTEXT=1\n"
    )
    boot_res = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert boot_res.returncode == 0, f"stdout: {boot_res.stdout}\nstderr: {boot_res.stderr}"
    assert "Telegram:     not enabled" in boot_res.stdout
    assert "API/session:  plaintext (H_MESH_ALLOW_PLAINTEXT=1)" in boot_res.stdout
    summary_text = (env_file.parent / "SUMMARY.md").read_text()
    assert "**API/session transport:** plaintext (H_MESH_ALLOW_PLAINTEXT=1)" in summary_text
    assert "standalone-api-secret" not in summary_text


def _fake_docker_with_networks(bin_dir: Path, log_path: Path, initial_networks: dict[str, str] | None = None) -> Path:
    db_path = bin_dir / "networks.db"
    lines = [f"{name}={label}" for name, label in (initial_networks or {"bridge": ""}).items()]
    db_path.write_text("\n".join(lines) + ("\n" if lines else ""))
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{log_path}"\n'
        f'DB="{db_path}"\n'
        'if [ "$1" = "network" ]; then\n'
        '    sub="$2"\n'
        '    shift 2\n'
        '    case "$sub" in\n'
        '        ls)\n'
        '            want_label=0\n'
        '            for arg in "$@"; do\n'
        '                [ "$arg" = "label=io.h-mesh.office-network=1" ] && want_label=1\n'
        '            done\n'
        '            while IFS="=" read -r name val; do\n'
        '                [ -z "$name" ] && continue\n'
        '                if [ "$want_label" -eq 1 ]; then\n'
        '                    [ "$val" = "1" ] && echo "$name"\n'
        '                else\n'
        '                    echo "$name"\n'
        '                fi\n'
        '            done < "$DB"\n'
        '            exit 0\n'
        '            ;;\n'
        '        inspect)\n'
        '            target="${!#}"\n'
        '            if grep -q "^${target}=" "$DB" 2>/dev/null; then\n'
        '                sed -n "s/^${target}=//p" "$DB" | tail -n1\n'
        '                exit 0\n'
        '            fi\n'
        '            echo "Error: No such network: $target" >&2\n'
        '            exit 1\n'
        '            ;;\n'
        '        create)\n'
        '            label_val=""\n'
        '            prev=""\n'
        '            for arg in "$@"; do\n'
        '                if [ "$prev" = "--label" ] && [ "$arg" = "io.h-mesh.office-network=1" ]; then\n'
        '                    label_val="1"\n'
        '                fi\n'
        '                prev="$arg"\n'
        '            done\n'
        '            target="${!#}"\n'
        '            printf "%s=%s\\n" "$target" "$label_val" >> "$DB"\n'
        '            echo "$target"\n'
        '            exit 0\n'
        '            ;;\n'
        '    esac\n'
        'fi\n'
        'exit 0\n'
    )
    docker.chmod(0o755)
    return db_path


def test_office_network_noninteractive_creates_labelled_network_writes_override_and_defaults_no_ports(tmp_path):
    log = tmp_path / "docker_calls.log"
    db_path = _fake_docker_with_networks(tmp_path, log)
    office_dir = tmp_path / "offices" / "netpod" / "nettenant"
    office_dir.mkdir(parents=True)
    env_file = office_dir / ".env"

    res = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "netpod", "--tenant", "nettenant",
            "--office-network", "hmesh-shared",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env={**_env_with_fake_docker(tmp_path), "H_MESH_ALLOW_PLAINTEXT": "1"},
        stdin=subprocess.DEVNULL,
    )
    assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"

    calls = log.read_text()
    assert "network create --label io.h-mesh.office-network=1 hmesh-shared" in calls
    assert "hmesh-shared=1" in db_path.read_text()

    env_text = env_file.read_text()
    assert "H_MESH_OFFICE_NETWORK=hmesh-shared" in env_text
    assert "H_MESH_OFFICE_ALIAS=netpod-nettenant" in env_text
    assert "H_MESH_BIND_PORTS=0" in env_text

    override_file = office_dir / "compose.network.yaml"
    assert override_file.is_file()
    override_text = override_file.read_text()
    assert "external: true" in override_text
    assert "name: hmesh-shared" in override_text
    assert "- netpod-nettenant" in override_text

    up_call = log.read_text().splitlines()[-1]
    assert f"-f {override_file}" in up_call
    assert "compose.ports.yaml" not in up_call

    summary_text = (office_dir / "SUMMARY.md").read_text()
    attach_text = (office_dir / "attach.sh").read_text()
    assert "**Office network:** hmesh-shared (alias: netpod-nettenant; peer URL: http://netpod-nettenant:8080)" in summary_text
    assert "**Published ports:** not published" in summary_text
    assert f"-f {override_file}" in summary_text
    assert f"-f {override_file}" in attach_text
    assert _api_token(env_file) not in summary_text
    assert _api_token(env_file) not in attach_text

    # Explicit --bind-ports is still respected when an office network is chosen via env.
    office2_dir = tmp_path / "offices" / "netpod2" / "nettenant"
    office2_dir.mkdir(parents=True)
    env_file2 = office2_dir / ".env"
    env2 = {**_env_with_fake_docker(tmp_path), "H_MESH_OFFICE_NETWORK": "hmesh-shared", "H_MESH_ALLOW_PLAINTEXT": "1"}
    res2 = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "netpod2", "--tenant", "nettenant",
            "--bind-ports", "--api-port", "18190", "--session-port", "18191",
            "--env-file", str(env_file2), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=env2, stdin=subprocess.DEVNULL,
    )
    assert res2.returncode == 0, f"stdout: {res2.stdout}\nstderr: {res2.stderr}"
    assert "H_MESH_BIND_PORTS=1" in env_file2.read_text()
    up_call2 = log.read_text().splitlines()[-1]
    assert "compose.ports.yaml" in up_call2
    assert f"-f {office2_dir / 'compose.network.yaml'}" in up_call2


def test_office_network_idempotent_rerun_and_explicit_detach(tmp_path):
    log = tmp_path / "docker_calls.log"
    _fake_docker_with_networks(tmp_path, log)
    office_dir = tmp_path / "offices" / "testpod" / "testtenant"
    office_dir.mkdir(parents=True)
    env_file = office_dir / ".env"
    override_file = office_dir / "compose.network.yaml"
    base_env = {**_env_with_fake_docker(tmp_path), "H_MESH_ALLOW_PLAINTEXT": "1"}

    first = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant",
            "--office-network", "hmesh-idem",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=base_env, stdin=subprocess.DEVNULL,
    )
    assert first.returncode == 0, first.stderr
    assert override_file.is_file()
    assert log.read_text().count("network create --label io.h-mesh.office-network=1 hmesh-idem") == 1

    # Rerun without --office-network must NOT recreate the network and must NOT detach the office.
    second = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=base_env, stdin=subprocess.DEVNULL,
    )
    assert second.returncode == 0, second.stderr
    assert log.read_text().count("network create --label io.h-mesh.office-network=1 hmesh-idem") == 1
    assert "H_MESH_OFFICE_NETWORK=hmesh-idem" in env_file.read_text()
    assert "H_MESH_OFFICE_ALIAS=testpod-testtenant" in env_file.read_text()
    assert "H_MESH_BIND_PORTS=0" in env_file.read_text()
    assert override_file.is_file()
    assert f"-f {override_file}" in log.read_text().splitlines()[-1]

    # Interactive rerun with all blank answers also keeps the existing office network.
    out_interactive, code_interactive = _bootstrap_wizard(
        env_file, base_env, ["", "", "", "", "", "", "", ""]
    )
    assert code_interactive == 0, out_interactive
    assert "H_MESH_OFFICE_NETWORK=hmesh-idem" in env_file.read_text()
    assert override_file.is_file()

    # Explicit --no-office-network detaches and removes compose.network.yaml.
    third = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "testtenant",
            "--no-office-network",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=base_env, stdin=subprocess.DEVNULL,
    )
    assert third.returncode == 0, third.stderr
    assert "H_MESH_OFFICE_NETWORK=" not in env_file.read_text()
    assert "H_MESH_OFFICE_ALIAS=" not in env_file.read_text()
    assert not override_file.exists()
    assert "**Office network:** none" in (office_dir / "SUMMARY.md").read_text()


def test_office_network_refuses_unlabelled_existing_network(tmp_path):
    log = tmp_path / "docker_calls.log"
    _fake_docker_with_networks(tmp_path, log, initial_networks={"bridge": "", "unlabelled-net": ""})
    office_dir = tmp_path / "offices" / "testpod" / "testtenant"
    office_dir.mkdir(parents=True)
    env_file = office_dir / ".env"

    for bad_net in ("unlabelled-net", "bridge"):
        log.write_text("")
        res = subprocess.run(
            [
                "bash", str(BOOTSTRAP),
                "--pod", "testpod", "--tenant", "testtenant",
                "--office-network", bad_net,
                "--env-file", str(env_file), "--skip-build", "--non-interactive",
            ],
            capture_output=True, text=True, timeout=10,
            env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
        )
        assert res.returncode != 0
        assert "io.h-mesh.office-network=1" in res.stderr
        assert "refusing to join a network bootstrap did not create" in res.stderr
        assert "up -d" not in log.read_text()
        assert not (office_dir / "compose.network.yaml").exists()


def test_office_network_interactive_picker_filters_by_label_and_creates_or_joins(tmp_path):
    log = tmp_path / "docker_calls.log"
    _fake_docker_with_networks(
        tmp_path, log,
        initial_networks={
            "bridge": "",
            "foreign-unlabelled": "",
            "hmesh-alpha": "1",
            "hmesh-beta": "1",
        },
    )
    office_dir = tmp_path / "offices" / "testpod" / "testtenant"
    office_dir.mkdir(parents=True)
    env_file = office_dir / ".env"

    # Choice 3 (join existing) -> pick #2 (hmesh-beta) -> publish ports default no ("") -> accounts/OAuth/local provider/Telegram -> plaintext ("", "y")
    answers_join = ["", "", "3", "2", "", "", "", "", "", "", "y"]
    output, code = _bootstrap_wizard(env_file, _env_with_fake_docker(tmp_path), answers_join)
    assert code == 0, output
    assert "1) hmesh-alpha" in output
    assert "2) hmesh-beta" in output
    assert "Join office network (number or name):" in output
    assert "Join office network (number or name) [" not in output
    assert "bridge" not in output
    assert "foreign-unlabelled" not in output
    assert "Peers on office network 'hmesh-beta' reach its API on port 8080" in output
    assert "Choose how to reach its published port" not in output
    assert "H_MESH_OFFICE_NETWORK=hmesh-beta" in env_file.read_text()
    assert "H_MESH_BIND_PORTS=0" in env_file.read_text()
    assert "name: hmesh-beta" in (office_dir / "compose.network.yaml").read_text()

    # When exactly one labelled network exists, joining defaults to that network.
    single_bin = tmp_path / "single_bin"
    single_bin.mkdir()
    _fake_docker_with_networks(
        single_bin, log,
        initial_networks={"bridge": "", "hmesh-only": "1"},
    )
    office_single_dir = tmp_path / "offices" / "singlepod" / "testtenant"
    office_single_dir.mkdir(parents=True)
    env_single = office_single_dir / ".env"
    single_env = _env_with_fake_docker(tmp_path)
    single_env["PATH"] = f"{single_bin}:{single_env['PATH']}"
    single_env["H_MESH_ALLOW_PLAINTEXT"] = "1"
    answers_join_single = ["", "", "3", "", "", "", "", "", ""]
    output_single, code_single = _bootstrap_wizard(env_single, single_env, answers_join_single)
    assert code_single == 0, output_single
    assert "Join office network (number or name) [hmesh-only]:" in output_single
    assert "H_MESH_OFFICE_NETWORK=hmesh-only" in env_single.read_text()

    # Choice 2 (create new) -> name "hmesh-created" -> publish ports default no ("") -> accounts/OAuth/local provider/Telegram
    office2_dir = tmp_path / "offices" / "testpod2" / "testtenant"
    office2_dir.mkdir(parents=True)
    env_file2 = office2_dir / ".env"
    env2 = {**_env_with_fake_docker(tmp_path), "H_MESH_ALLOW_PLAINTEXT": "1"}
    answers_create = ["", "", "2", "hmesh-created", "", "", "", "", ""]
    output2, code2 = _bootstrap_wizard(env_file2, env2, answers_create)
    assert code2 == 0, output2
    assert "network create --label io.h-mesh.office-network=1 hmesh-created" in log.read_text()
    assert "H_MESH_OFFICE_NETWORK=hmesh-created" in env_file2.read_text()
    assert "H_MESH_BIND_PORTS=0" in env_file2.read_text()


def test_office_network_validates_alias_is_legal_dns_label(tmp_path):
    log = tmp_path / "docker_calls.log"
    _fake_docker_with_networks(tmp_path, log)
    env_file = tmp_path / ".env"

    # Both pod (40 chars) and tenant (30 chars) are valid identity segments individually,
    # but <pod>-<tenant> is 71 chars (> 63 RFC 1123 DNS label limit).
    long_pod = "p" * 40
    long_tenant = "t" * 30
    res_long = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", long_pod, "--tenant", long_tenant,
            "--office-network", "hmesh-shared",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res_long.returncode != 0
    assert "not a legal DNS label" in res_long.stderr
    assert "network create" not in log.read_text()
    assert "up -d" not in log.read_text()

    # A trailing hyphen in tenant also makes <pod>-<tenant> an illegal DNS label.
    res_hyphen = subprocess.run(
        [
            "bash", str(BOOTSTRAP),
            "--pod", "testpod", "--tenant", "bad-",
            "--office-network", "hmesh-shared",
            "--env-file", str(env_file), "--skip-build", "--non-interactive",
        ],
        capture_output=True, text=True, timeout=10,
        env=_env_with_fake_docker(tmp_path), stdin=subprocess.DEVNULL,
    )
    assert res_hyphen.returncode != 0
    assert "not a legal DNS label" in res_hyphen.stderr
    assert "network create" not in log.read_text()
    assert "up -d" not in log.read_text()


def test_dockerfile_copied_runtime_scripts_are_world_readable_and_executable(tmp_path):
    dockerfile = (REPO_ROOT / "container" / "Dockerfile").read_text()
    assert "chmod +x" not in dockerfile, (
        "bare 'chmod +x' on a 0600 file from a umask 077 checkout produces 0711 "
        "(no read bit for non-owner ubuntu, causing exit 126 Permission denied)"
    )
    assert "COPY --chmod=0755 container/entrypoint.sh /usr/local/bin/entrypoint.sh" in dockerfile
    assert "RUN chmod 0755 /usr/local/bin/entrypoint.sh" in dockerfile
    assert "chmod 0755 /app/setup.sh" in dockerfile

    # Simulate a umask 077 checkout where runtime scripts start at mode 0600,
    # then apply the Dockerfile's chmod 0755 and verify a+rx (0o555) bits are set.
    for rel in ("container/entrypoint.sh", "setup.sh"):
        src = REPO_ROOT / rel
        assert src.is_file()
        staged = tmp_path / src.name
        staged.write_bytes(src.read_bytes())
        staged.chmod(0o600)
        subprocess.run(["chmod", "0755", str(staged)], check=True)
        mode = staged.stat().st_mode & 0o777
        assert (mode & 0o555) == 0o555, f"{rel} after chmod 0755 is {oct(mode)}, not a+rx"





