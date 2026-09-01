"""Real end-to-end confirmation that the session daemon actually streams a
live tmux pane -- the mechanism behind the Telegram bot's /watch command
(clients/telegram/bot.py's PaneWatchRender) and the web console's terminal
view. services.session used to exist only as a console script nothing in
the bootstrap path ever invoked (same gap as the watchdog, see
test_daemons.py's watchdog test) -- this connects a real WebSocket client
to a real session daemon, watching a real hired agent's real tmux pane,
using the exact protocol modules/session/app.py's /session endpoint
implements (Bearer auth, {"mode": "read-only", "subscribe": [...]}), not a
mock of it.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import redis
from websockets.sync.client import connect as ws_connect

from core.keys import prefix
from core.registry import port_type
from modules.tmux.ops import list_windows
from services.daemons import DAEMON_MODULES, pid_alive, start_daemons, stop_daemons

REPO_ROOT = Path(__file__).resolve().parents[2]


def _skip_unless_redis() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        redis.Redis.from_url(redis_url).ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")


def test_session_daemon_streams_a_real_hired_agents_pane():
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_session_watch_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    session_name = f"sess-{os.urandom(4).hex()}"
    socket_path = os.path.join(tmpdir, "isolated.sock")
    marker = f"MARKER-{os.urandom(4).hex()}"

    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_h_agent = os.path.join(fake_bin, "h-agent")
    with open(fake_h_agent, "w") as f:
        # Prints a distinctive marker before dropping into an interactive
        # shell -- real evidence a subscriber received *this* pane's actual
        # content, not just that a WebSocket connection was accepted.
        f.write(f"#!/usr/bin/env bash\necho {marker}\nexec bash -il\n")
    os.chmod(fake_h_agent, 0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    env["PYTHONUNBUFFERED"] = "1"
    env["TMUX_SESSION"] = session_name
    env["TMUX_TMPDIR"] = tmpdir
    env["TMUX_SOCKET"] = socket_path

    api_token = "test-token-for-session-watch"
    env["API_TOKEN"] = api_token
    env["API_BIND"] = "127.0.0.1"
    env["SESSION_BIND"] = "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        env["API_PORT"] = str(probe.getsockname()[1])
    session_port = None
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        session_port = probe.getsockname()[1]
    env["SESSION_PORT"] = str(session_port)

    r = redis.Redis.from_url(env["REDIS_URL"])
    ws = None
    try:
        # skip telegram_bot (a fake token would use the real TelegramClient,
        # not DryRunTelegramClient, risking a real network call) -- this
        # test is about session, not the bot itself.
        daemon_modules = {**DAEMON_MODULES, "api": "services.api", "session": "services.session"}
        pids = start_daemons(python=python, run_dir=run_dir, env=env, daemon_modules=daemon_modules)
        assert pid_alive(pids["session"]), (run_dir / "session.log").read_text()

        registry_key = prefix(pod, tenant, resource="registry")
        r.hset(registry_key, mapping={"host": "office"})

        hire_env = dict(env)
        hire_env["AGENT_NAME"] = "host"
        hire_res = subprocess.run(
            [str(python), "-m", "modules.office.cli", "hire", "worker1", "--cli", "claude"],
            env=hire_env, capture_output=True, text=True, timeout=15,
        )
        assert hire_res.returncode == 0, f"hire failed: {hire_res.stderr}\n{hire_res.stdout}"

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if port_type(r, pod=pod, tenant=tenant, agent="worker1") == "tmux":
                break
            time.sleep(0.2)
        assert port_type(r, pod=pod, tenant=tenant, agent="worker1") == "tmux", "worker1 never registered"

        # Registry membership lags the reconciler actually creating the
        # physical tmux window it names -- the session daemon's controller
        # only knows about a real window, not a registry entry.
        deadline = time.monotonic() + 10.0
        windows: set[str] = set()
        while time.monotonic() < deadline:
            windows = set(list_windows(session_name, socket=socket_path))
            if "worker1" in windows:
                break
            time.sleep(0.2)
        assert "worker1" in windows, f"worker1's tmux window never appeared, saw: {windows}"

        # The real protocol clients/telegram/bot.py's watch feature and the
        # session-connecting web console both use -- Bearer auth, then a
        # {"mode", "subscribe"} message, per modules/session/app.py.
        ws = ws_connect(
            f"ws://127.0.0.1:{session_port}/session",
            additional_headers={"Authorization": f"Bearer {api_token}"},
        )
        ws.send(json.dumps({"mode": "read-only", "subscribe": ["worker1"], "refresh": True}))

        received_marker = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                raw = ws.recv(timeout=max(0.5, deadline - time.monotonic()))
            except TimeoutError:
                break
            event = json.loads(raw)
            assert "error" not in event, f"session rejected the watch: {event}"
            if event.get("agent") == "worker1" and marker in event.get("data", ""):
                received_marker = True
                break
        assert received_marker, (
            "never received the real pane's marker text over a real watch stream"
        )
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        for name in ("switch", "tmux_reconciler", "watchdog", "api", "session"):
            pidfile = run_dir / f"{name}.pid"
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text().strip()), 9)
                except (ValueError, OSError):
                    pass
        try:
            subprocess.run(["tmux", "-S", socket_path, "kill-server"], capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                r.delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
