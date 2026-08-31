import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import redis

from core.keys import prefix
from core.registry import port_type
from modules.tmux.ops import list_windows, run_tmux
from services.tenant_config import read_tenant_env

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "setup.sh"

# One blank answer per prompt in the "accept every default" path: pod,
# tenant, agent count, multiple-accounts?, default account's OAuth token,
# default CLI, CLI/account exceptions, local provider?.
DEFAULT_PATH_ANSWERS = ["", "", "", "", "", "", "", ""]


def _run_wizard(cwd: str, args: list[str], env: dict, answers: list[str], timeout: float = 60) -> tuple[str, int]:
    """Drive setup.sh through a real pty, sending one scripted answer per
    prompt. A pty is the only faithful way to exercise the `[ -t 0 ]`
    interactive branch -- a piped/redirected stdin (what subprocess.run
    would give it otherwise) is never a tty, so the wizard would never
    activate at all."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [str(SETUP_SH), *args],
        stdin=slave, stdout=slave, stderr=slave, env=env, cwd=cwd,
    )
    os.close(slave)

    output = b""
    answer_idx = 0
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            ready, _, _ = select.select([master], [], [], 1)
            if master not in ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if output.rstrip().endswith(b":") and answer_idx < len(answers):
                os.write(master, (answers[answer_idx] + "\n").encode())
                answer_idx += 1

        if proc.poll() is None:
            proc.wait(timeout=max(1, deadline - time.monotonic()))
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    return output.decode(errors="replace"), proc.returncode


@pytest.fixture
def wizard_env():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    r = redis.Redis.from_url(redis_url)
    try:
        r.ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")

    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_wizard_")
    home_dir = os.path.join(tmpdir, "home")
    os.makedirs(home_dir, exist_ok=True)
    socket_path = os.path.join(tmpdir, "isolated.sock")
    run_dir = os.path.join(tmpdir, "run")
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    session_name = f"sess-{os.urandom(4).hex()}"

    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_h_agent = os.path.join(fake_bin, "h-agent")
    with open(fake_h_agent, "w") as f:
        f.write("#!/usr/bin/env bash\nexec bash -il\n")
    os.chmod(fake_h_agent, 0o755)

    env = dict(os.environ)
    env["HOME"] = home_dir
    env["H_MESH_STATE_DIR"] = os.path.join(home_dir, ".h-mesh")
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["H_MESH_RUN_DIR"] = run_dir
    env["AGENT_NAME"] = "architect"
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = redis_url
    env["TMUX_SESSION"] = session_name
    env["TMUX_SOCKET"] = socket_path

    for k in list(env.keys()):
        if k.startswith("CLAUDE_OAUTH_TOKEN_") or k == "CLAUDE_CODE_OAUTH_TOKEN":
            del env[k]

    ctx = {
        "tmpdir": tmpdir, "home_dir": home_dir, "socket_path": socket_path,
        "run_dir": run_dir, "pod": pod, "tenant": tenant, "session_name": session_name,
        "env": env,
    }
    yield ctx

    try:
        run_tmux("kill-server", socket=socket_path)
    except Exception:
        pass
    for name in ("switch", "tmux_reconciler"):
        pidfile = Path(run_dir) / f"{name}.pid"
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
            except (ValueError, OSError):
                pass
    try:
        keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
        if keys:
            r.delete(*keys)
    except Exception:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_wizard_accepting_every_default_hires_a_single_architect_agent(wizard_env, monkeypatch):
    ctx = wizard_env
    output, code = _run_wizard(
        cwd=str(REPO_ROOT),
        args=[
            "--venv", sys.prefix,
            "--skip-install", "--skip-deps",
        ],
        env=ctx["env"],
        answers=DEFAULT_PATH_ANSWERS,
    )
    assert code == 0, f"setup.sh exited {code}:\n{output}"
    assert "Agents: architect" in output or "architect" in output, output

    monkeypatch.setenv("H_MESH_STATE_DIR", ctx["env"]["H_MESH_STATE_DIR"])
    persisted = read_tenant_env(ctx["tenant"])
    assert persisted.get("AGENTS") == "architect"
    assert persisted.get("DEFAULT_CLI") == "claude"

    redis_url = ctx["env"]["REDIS_URL"]
    r = redis.Redis.from_url(redis_url)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if port_type(r, pod=ctx["pod"], tenant=ctx["tenant"], agent="architect") == "tmux":
            break
        time.sleep(0.2)
    assert port_type(r, pod=ctx["pod"], tenant=ctx["tenant"], agent="architect") == "tmux"

    windows = set()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        windows = list_windows(ctx["session_name"], socket=ctx["socket_path"])
        if "architect" in windows:
            break
        time.sleep(0.2)
    assert "architect" in windows


def test_wizard_rerun_is_idempotent_and_does_not_rehire(wizard_env, monkeypatch):
    ctx = wizard_env
    output1, code1 = _run_wizard(
        cwd=str(REPO_ROOT),
        args=["--venv", sys.prefix, "--skip-install", "--skip-deps"],
        env=ctx["env"],
        answers=DEFAULT_PATH_ANSWERS,
    )
    assert code1 == 0, output1

    with open(Path(ctx["run_dir"]) / "switch.pid") as f:
        first_switch_pid = int(f.read().strip())
    with open(Path(ctx["run_dir"]) / "tmux_reconciler.pid") as f:
        first_reconciler_pid = int(f.read().strip())
    os.kill(first_switch_pid, signal.SIGKILL)

    output2, code2 = _run_wizard(
        cwd=str(REPO_ROOT),
        args=["--venv", sys.prefix, "--skip-install", "--skip-deps"],
        env=ctx["env"],
        answers=DEFAULT_PATH_ANSWERS,
    )
    assert code2 == 0, output2
    assert "already running" in output2

    monkeypatch.setenv("H_MESH_STATE_DIR", ctx["env"]["H_MESH_STATE_DIR"])
    persisted = read_tenant_env(ctx["tenant"])
    assert persisted.get("AGENTS") == "architect"

    with open(Path(ctx["run_dir"]) / "switch.pid") as f:
        second_switch_pid = int(f.read().strip())
    with open(Path(ctx["run_dir"]) / "tmux_reconciler.pid") as f:
        second_reconciler_pid = int(f.read().strip())
    # switch was killed before the re-run, so it should come back with a new
    # pid; tmux_reconciler was left alive, so start_daemons() should have
    # left it running rather than spawning a second one.
    assert second_switch_pid != first_switch_pid
    assert second_reconciler_pid == first_reconciler_pid

    reconciler_matches = subprocess.run(
        ["pgrep", "-f", "services.tmux_reconciler"], capture_output=True, text=True
    ).stdout.split()
    assert reconciler_matches.count(str(first_reconciler_pid)) == 1, (
        f"expected exactly one tmux_reconciler process, found pids: {reconciler_matches}"
    )


def test_wizard_hires_a_per_agent_cli_exception_with_the_right_cli(wizard_env):
    ctx = wizard_env
    answers = [
        "",       # pod
        "",       # tenant
        "2",      # how many agents
        "",       # agent #1 name -> architect
        "",       # agent #2 name -> sme-2
        "",       # use more than one account? -> n
        "",       # OAuth token for 'default'
        "",       # default CLI -> claude
        "sme-2",  # any agents differing
        "codex",  # sme-2 -- CLI
        "",       # local model provider? -> n
    ]
    output, code = _run_wizard(
        cwd=str(REPO_ROOT),
        args=["--venv", sys.prefix, "--skip-install", "--skip-deps"],
        env=ctx["env"],
        answers=answers,
        timeout=90,
    )
    assert code == 0, f"setup.sh exited {code}:\n{output}"

    redis_url = ctx["env"]["REDIS_URL"]
    r = redis.Redis.from_url(redis_url)
    for agent in ("architect", "sme-2"):
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if port_type(r, pod=ctx["pod"], tenant=ctx["tenant"], agent=agent) == "tmux":
                break
            time.sleep(0.2)
        assert port_type(r, pod=ctx["pod"], tenant=ctx["tenant"], agent=agent) == "tmux"

    launch_key_architect = prefix(ctx["pod"], ctx["tenant"], agent="architect", resource="launch")
    launch_key_sme2 = prefix(ctx["pod"], ctx["tenant"], agent="sme-2", resource="launch")
    architect_cli = r.get(launch_key_architect)
    sme2_cli = r.get(launch_key_sme2)
    architect_cli = architect_cli.decode() if isinstance(architect_cli, bytes) else architect_cli
    sme2_cli = sme2_cli.decode() if isinstance(sme2_cli, bytes) else sme2_cli

    assert architect_cli == "claude"
    assert sme2_cli == "codex"
