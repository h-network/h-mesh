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
    last = res.stdout.strip().splitlines()[-1]
    assert "published API port 18090 is closed" in last
    assert "API_TLS_CERT and API_TLS_KEY" in last
    assert "H_MESH_ALLOW_PLAINTEXT=1" in last
    assert last in (tmp_path / "SUMMARY.md").read_text()
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
def test_bootstrap_interactive_without_telegram_explicitly_selects_transport(tmp_path, transport):
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path, log)
    env_file = tmp_path / ".env"
    # agents, CLI, publish, API port, session port, accounts, OAuth, Telegram
    answers = ["", "", "", "", "", "", "", ""]
    answers += ["", "y"] if transport == "plaintext" else ["/cert.pem", "/key.pem"]
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
        assert "API_TLS_CERT=/cert.pem" in env_file.read_text()
        assert "API_TLS_KEY=/key.pem" in env_file.read_text()
        assert "H_MESH_ALLOW_PLAINTEXT=" not in env_file.read_text()


def test_bootstrap_interactive_declining_plaintext_requires_tls(tmp_path):
    log = tmp_path / "docker.log"
    _fake_docker(tmp_path, log)
    env_file = tmp_path / ".env"
    output, code = _bootstrap_wizard(env_file, _env_with_fake_docker(tmp_path), [""] * 10)
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
    "H_MESH_BIND_PORTS",
    "API_PORT",
    "SESSION_PORT",
    "ACCOUNTS",
    "DEFAULT_ACCOUNT",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "API_TOKEN",
    "API_TLS_CERT",
    "API_TLS_KEY",
    "SESSION_TLS_CERT",
    "SESSION_TLS_KEY",
    "H_MESH_ALLOW_PLAINTEXT",
)


def _env_with_fake_docker(bin_dir: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    for key in _SCRUBBED_BOOTSTRAP_ENV_VARS:
        env.pop(key, None)
    for key in list(env):
        if key.startswith(("CLAUDE_OAUTH_TOKEN_", "TELEGRAM_")):
            del env[key]
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


def test_noninteractive_summary_reports_telegram_from_env_file(tmp_path):
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
        "API_TLS_CERT=/etc/ssl/certs/h-mesh.pem\n"
        "API_TLS_KEY=/etc/ssl/private/h-mesh.key\n"
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
    assert "API/session:  TLS (/etc/ssl/certs/h-mesh.pem)" in res_tls.stdout


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



