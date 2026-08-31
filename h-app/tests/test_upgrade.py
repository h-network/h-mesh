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


def test_upgrade_help():
    res = subprocess.run([sys.executable, "-m", "services.upgrade", "--help"],
                          capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "h-app")})
    assert res.returncode == 0
    assert "--skip-install" in res.stdout
    assert "--skip-pull" in res.stdout


def test_upgrade_restarts_daemons_without_duplicating_and_preserves_the_tmux_session():
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    r = redis.Redis.from_url(redis_url)
    try:
        r.ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")

    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_upgrade_")
    socket_path = os.path.join(tmpdir, "isolated.sock")
    run_dir = os.path.join(tmpdir, "run")
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    session_name = f"sess-{os.urandom(4).hex()}"

    # Same rationale as test_setup_script.py / test_daemons.py: h-agent is
    # supplied by the runtime base image, not present here -- stand one in
    # so hired panes have a long-lived shell instead of exiting immediately.
    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_h_agent = os.path.join(fake_bin, "h-agent")
    with open(fake_h_agent, "w") as f:
        f.write("#!/usr/bin/env bash\nexec bash -il\n")
    os.chmod(fake_h_agent, 0o755)

    # ⚠ setup.sh and h-mesh upgrade both write to ~/.bashrc and ~/.profile
    # now (persisting the venv bin dir on PATH) -- HOME must be isolated or
    # this test edits the real ones.
    home_dir = os.path.join(tmpdir, "home")
    os.makedirs(home_dir, exist_ok=True)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["HOME"] = home_dir
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["H_MESH_RUN_DIR"] = run_dir
    env["AGENT_NAME"] = "architect"
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = redis_url
    env["TMUX_SESSION"] = session_name
    env["TMUX_SOCKET"] = socket_path

    switch_pid = None
    reconciler_pid = None
    old_switch_pid = None
    old_reconciler_pid = None

    try:
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
        assert res.returncode == 0, f"setup.sh failed: {res.stderr}\nstdout: {res.stdout}"

        with open(os.path.join(run_dir, "switch.pid")) as f:
            old_switch_pid = int(f.read().strip())
        with open(os.path.join(run_dir, "tmux_reconciler.pid")) as f:
            old_reconciler_pid = int(f.read().strip())

        # Hire a worker before upgrading, to prove its pane survives.
        venv_python = sys.executable
        env_hire = dict(env)
        env_hire["AGENT_NAME"] = "host"
        hire_res = subprocess.run(
            [venv_python, "-m", "modules.office.cli", "hire", "worker1"],
            env=env_hire, capture_output=True, text=True, timeout=10,
        )
        assert hire_res.returncode == 0, f"hire failed: {hire_res.stderr}"

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if port_type(r, pod=pod, tenant=tenant, agent="worker1") == "tmux":
                break
            time.sleep(0.2)
        assert port_type(r, pod=pod, tenant=tenant, agent="worker1") == "tmux"

        windows = set()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            windows = list_windows(session_name, socket=socket_path)
            if "worker1" in windows:
                break
            time.sleep(0.2)
        assert "worker1" in windows

        # Run upgrade: skip git pull (no real remote to pull from in a
        # test) and skip pip install (this venv already has h-mesh
        # editable-installed, same reasoning as test_setup_script.py).
        upg_res = subprocess.run(
            [
                venv_python, "-m", "services.upgrade",
                "--pod", pod,
                "--tenant", tenant,
                "--session", session_name,
                "--tmux-socket", socket_path,
                "--redis-url", redis_url,
                "--venv", venv_dir,
                "--skip-pull",
                "--skip-install",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert upg_res.returncode == 0, f"upgrade failed: {upg_res.stderr}\nstdout: {upg_res.stdout}"

        with open(os.path.join(run_dir, "switch.pid")) as f:
            switch_pid = int(f.read().strip())
        with open(os.path.join(run_dir, "tmux_reconciler.pid")) as f:
            reconciler_pid = int(f.read().strip())

        # Restarted, not duplicated: new pids, old ones gone, exactly one
        # live pidfile per daemon.
        assert switch_pid != old_switch_pid
        assert reconciler_pid != old_reconciler_pid
        assert not _pid_alive(old_switch_pid)
        assert not _pid_alive(old_reconciler_pid)
        assert _pid_alive(switch_pid)
        assert _pid_alive(reconciler_pid)

        # Registry participants untouched by upgrade.
        assert port_type(r, pod=pod, tenant=tenant, agent="host") == "office"
        assert port_type(r, pod=pod, tenant=tenant, agent="api") == "api"
        assert port_type(r, pod=pod, tenant=tenant, agent="worker1") == "tmux"

        # Upgrade re-persists the venv bin dir on PATH too (repairs an
        # install that predates the fix, or whose venv path changed).
        venv_bin = os.path.join(venv_dir, "bin")
        for rc_filename in (".bashrc", ".profile"):
            rc_content = open(os.path.join(home_dir, rc_filename)).read()
            assert venv_bin in rc_content, f"{rc_filename} missing venv bin on PATH after upgrade"

        # The tmux server/session itself was never touched -- worker1's pane
        # survives the upgrade (the documented limit: it keeps its original
        # env, but it's still there and still addressable).
        windows_after = list_windows(session_name, socket=socket_path)
        assert "worker1" in windows_after

        # Prove the new daemons actually route: send a message post-upgrade.
        env_send = dict(env)
        env_send["AGENT_NAME"] = "host"
        send_res = subprocess.run(
            [venv_python, "-m", "modules.office.cli", "send", "-a", "worker1", "post-upgrade message"],
            env=env_send, capture_output=True, text=True, timeout=10,
        )
        assert send_res.returncode == 0, f"send failed: {send_res.stderr}"

        delivered = False
        pane_output = ""
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            code, stdout, _ = run_tmux(
                "capture-pane", "-J", "-p", "-t", f"{session_name}:worker1", socket=socket_path
            )
            if code == 0 and "post-upgrade message" in stdout:
                delivered = True
                pane_output = stdout
                break
            time.sleep(0.2)
        assert delivered, f"message not delivered after upgrade. Pane:\n{pane_output}"

        # Running upgrade again should not leave a third generation of
        # processes lying around either.
        upg_res2 = subprocess.run(
            [
                venv_python, "-m", "services.upgrade",
                "--pod", pod, "--tenant", tenant, "--session", session_name,
                "--tmux-socket", socket_path, "--redis-url", redis_url,
                "--venv", venv_dir, "--skip-pull", "--skip-install",
            ],
            env=env, capture_output=True, text=True, timeout=30,
        )
        assert upg_res2.returncode == 0, f"second upgrade failed: {upg_res2.stderr}"
        with open(os.path.join(run_dir, "switch.pid")) as f:
            switch_pid2 = int(f.read().strip())
        with open(os.path.join(run_dir, "tmux_reconciler.pid")) as f:
            reconciler_pid2 = int(f.read().strip())
        assert switch_pid2 != switch_pid
        assert not _pid_alive(switch_pid)
        assert not _pid_alive(reconciler_pid)
        assert _pid_alive(switch_pid2)
        assert _pid_alive(reconciler_pid2)

        switch_pid, reconciler_pid = switch_pid2, reconciler_pid2

    finally:
        for pid in (switch_pid, reconciler_pid, old_switch_pid, old_reconciler_pid):
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            run_tmux("kill-server", socket=socket_path)
        except Exception:
            pass
        try:
            keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                r.delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
