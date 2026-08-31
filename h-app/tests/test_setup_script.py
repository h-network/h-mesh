import os
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

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SH = REPO_ROOT / "setup.sh"


def test_setup_help():
    res = subprocess.run([str(SETUP_SH), "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "Usage: ./setup.sh" in res.stdout
    assert "--pod" in res.stdout
    assert "--tenant" in res.stdout
    assert "--redis-url" in res.stdout


def test_setup_seeds_registry_and_runs_e2e_hire_and_message():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    r = redis.Redis.from_url(redis_url)
    try:
        r.ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")

    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_setup_")
    socket_path = os.path.join(tmpdir, "isolated.sock")
    run_dir = os.path.join(tmpdir, "run")
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    session_name = f"sess-{os.urandom(4).hex()}"

    # h-agent is supplied by the runtime base image, not by this repo or CI --
    # stand in a fake one so the hired window has a long-lived shell to
    # deliver a message into, same as the base image would give it.
    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_h_agent = os.path.join(fake_bin, "h-agent")
    with open(fake_h_agent, "w") as f:
        f.write("#!/usr/bin/env bash\nexec bash -il\n")
    os.chmod(fake_h_agent, 0o755)

    state_dir = os.path.join(tmpdir, "state")
    os.makedirs(state_dir, exist_ok=True)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["H_MESH_RUN_DIR"] = run_dir
    env["H_MESH_STATE_DIR"] = state_dir
    env["H_MESH_LOG_FILE"] = os.path.join(state_dir, "window.log.jsonl")
    env["AGENT_NAME"] = "architect"
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = redis_url
    env["TMUX_SESSION"] = session_name
    env["TMUX_SOCKET"] = socket_path

    # Scrub ambient tokens to prevent real credential leakage into test processes
    for k in list(env.keys()):
        if k.startswith("CLAUDE_OAUTH_TOKEN_") or k == "CLAUDE_CODE_OAUTH_TOKEN":
            del env[k]

    switch_pid = None
    reconciler_pid = None

    try:
        # Reuse the venv already running this test process -- it necessarily
        # has h-mesh's dependencies installed, since this file imports them.
        # A path like REPO_ROOT/.venv is not guaranteed to exist or be
        # populated; --skip-install would leave a freshly created one empty.
        venv_dir = sys.prefix
        res = subprocess.run(
            [
                str(SETUP_SH),
                "--pod", pod,
                "--tenant", tenant,
                "--session", session_name,
                "--tmux-socket", socket_path,
                "--redis-url", redis_url,
                "--venv", venv_dir,
                "--skip-install",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            sw_log = ""
            tr_log = ""
            try:
                with open(os.path.join(run_dir, "switch.log")) as f:
                    sw_log = f.read()
            except Exception:
                pass
            try:
                with open(os.path.join(run_dir, "tmux_reconciler.log")) as f:
                    tr_log = f.read()
            except Exception:
                pass
            assert res.returncode == 0, f"setup.sh failed: {res.stderr}\nstdout: {res.stdout}\nswitch.log: {sw_log}\ntmux_reconciler.log: {tr_log}"

        # 1. Verify fixed participants are seeded in Redis
        reg_key = prefix(pod, tenant, resource="registry")
        assert port_type(r, pod=pod, tenant=tenant, agent="host") == "office"
        assert port_type(r, pod=pod, tenant=tenant, agent="api") == "api"

        # Read daemon PIDs from run_dir
        with open(os.path.join(run_dir, "switch.pid")) as f:
            switch_pid = int(f.read().strip())
        with open(os.path.join(run_dir, "tmux_reconciler.pid")) as f:
            reconciler_pid = int(f.read().strip())

        assert switch_pid > 0
        assert reconciler_pid > 0

        # 2. Hire an agent: host hires 'worker1'
        venv_python = sys.executable
        env_hire = dict(env)
        env_hire["AGENT_NAME"] = "host"
        hire_res = subprocess.run(
            [venv_python, "-m", "modules.office.cli", "hire", "worker1"],
            env=env_hire,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert hire_res.returncode == 0, f"hire failed: {hire_res.stderr}\nstdout: {hire_res.stdout}"

        # Wait for switch and office port to process StartAgent and update registry
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            pt = port_type(r, pod=pod, tenant=tenant, agent="worker1")
            if pt == "tmux":
                break
            time.sleep(0.2)
        if port_type(r, pod=pod, tenant=tenant, agent="worker1") != "tmux":
            sw_log = open(os.path.join(run_dir, "switch.log")).read()
            tr_log = open(os.path.join(run_dir, "tmux_reconciler.log")).read()
            assert False, f"worker1 not registered. switch.log:\n{sw_log}\ntmux_reconciler.log:\n{tr_log}"

        # Wait for reconciler to create the tmux window for worker1
        deadline = time.monotonic() + 10.0
        windows = set()
        while time.monotonic() < deadline:
            try:
                windows = list_windows(session_name, socket=socket_path)
                if "worker1" in windows:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        assert "worker1" in windows

        # 3. Send a message to worker1
        env_send = dict(env)
        env_send["AGENT_NAME"] = "host"
        send_res = subprocess.run(
            [venv_python, "-m", "modules.office.cli", "send", "-a", "worker1", "welcome to h-mesh worker1"],
            env=env_send,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert send_res.returncode == 0, f"send failed: {send_res.stderr}\nstdout: {send_res.stdout}"

        # Wait for switch and tmux port to deliver the message to worker1 window
        deadline = time.monotonic() + 10.0
        delivered = False
        pane_output = ""
        while time.monotonic() < deadline:
            code, stdout, stderr = run_tmux(
                "capture-pane", "-J", "-p", "-t", f"{session_name}:worker1", socket=socket_path
            )
            if code == 0 and "welcome to h-mesh worker1" in stdout:
                delivered = True
                pane_output = stdout
                break
            time.sleep(0.2)

        assert delivered, f"Message was not delivered to worker1 pane. Last pane text:\n{pane_output}"
        assert "[message from host] welcome to h-mesh worker1" in pane_output

    finally:
        # Stop background daemons
        for pid in (switch_pid, reconciler_pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass

        # Kill test tmux server
        try:
            run_tmux("kill-server", socket=socket_path)
        except Exception:
            pass

        # Clean test Redis keys
        try:
            keys = r.keys(f"{pod}.{tenant}.*") or []
            colon_keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            all_keys = list(set(keys + colon_keys))
            if all_keys:
                r.delete(*all_keys)
        except Exception:
            pass

        shutil.rmtree(tmpdir, ignore_errors=True)
