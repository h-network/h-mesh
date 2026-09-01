"""Real smoke test for services/telegram_bot.py's main() -- not just "a PID
appeared". services.telegram_bot used to call TelegramBot.run(), a method
that doesn't exist; every real start crashed instantly with AttributeError,
caught live by a user, caught by nothing here, because nothing executed
this module's main() at all.

Runs the actual launcher against a real, isolated switch+api pair (with the
registry seeded, same as setup.sh) so enrol() -- a real StartAgent envelope
through the mesh -- succeeds fast instead of retrying for up to 60s. No
TELEGRAM_BOT_TOKEN (dry-run mode: DryRunTelegramClient.get_updates() always
returns [], so run_polling() never makes a real network call to Telegram's
servers -- this test makes no external network calls at all). A daemon that
crashes on entry dies within a fraction of a second (measured: the original
bug crashed in ~0.3s); staying alive for several seconds is real evidence
main() actually works, not a PID-existence proxy for it.
"""

import os
import shutil
import signal
import socket
import sys
import tempfile
import time
from pathlib import Path

import pytest
import redis

from core.keys import prefix
from services.daemons import DAEMON_MODULES, pid_alive, start_daemons, stop_daemons


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _skip_unless_redis() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        redis.Redis.from_url(redis_url).ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")


def test_telegram_bot_launcher_starts_and_stays_alive_against_a_real_api():
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_telegram_launcher_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    api_port = _free_port()
    api_token = "test-token-for-telegram-launcher-smoke"

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "h-app")
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    env["PYTHONUNBUFFERED"] = "1"
    # Isolated, not inherited -- see h-app/tests/conftest.py.
    env["TMUX_TMPDIR"] = tmpdir
    env["TMUX_SESSION"] = f"sess-{os.urandom(4).hex()}"
    env["TMUX_SOCKET"] = os.path.join(tmpdir, "isolated.sock")
    env["API_TOKEN"] = api_token
    env["API_BIND"] = "127.0.0.1"
    env["API_PORT"] = str(api_port)
    env["H_MESH_API_URL"] = f"http://127.0.0.1:{api_port}"
    env["TELEGRAM_CHAT_ID"] = "12345"
    env["TELEGRAM_CURSOR_FILE"] = os.path.join(tmpdir, "cursor.json")
    # Deliberately no TELEGRAM_BOT_TOKEN -- dry-run mode, so run_polling()
    # never makes a real network call to Telegram's servers.

    # enrol() is a real StartAgent envelope through the mesh -- needs the
    # switch running and "host"/"api" seeded (same as setup.sh) to succeed
    # fast, or it retries for up to 60s before giving up.
    r = redis.Redis.from_url(env["REDIS_URL"])
    registry_key = prefix(pod, tenant, resource="registry")
    r.hset(registry_key, mapping={"host": "office", "api": "api"})

    daemon_modules = {**DAEMON_MODULES, "api": "services.api", "telegram_bot": "services.telegram_bot"}
    try:
        pids = start_daemons(python=python, run_dir=run_dir, env=env, daemon_modules=daemon_modules)
        assert set(pids) == set(DAEMON_MODULES) | {"api", "telegram_bot"}

        # The original bug crashed within ~0.3s. Staying alive for several
        # seconds is real evidence main() didn't hit an unhandled exception,
        # not just that the process existed a moment after spawn.
        time.sleep(3)
        for name, pid in pids.items():
            assert pid_alive(pid), (
                f"{name} (pid {pid}) died -- check {run_dir / f'{name}.log'}:\n"
                + (run_dir / f"{name}.log").read_text()
            )

        log_text = (run_dir / "telegram_bot.log").read_text()
        assert "Traceback" not in log_text, log_text
        assert "AttributeError" not in log_text, log_text
        assert "Telegram bot starting long-polling loop" in log_text, log_text
        assert "Enrolled application 'telegram'" in log_text, log_text

        stop_daemons(run_dir)
        for name, pid in pids.items():
            assert not pid_alive(pid)
    finally:
        for name in daemon_modules:
            pidfile = run_dir / f"{name}.pid"
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
                except (ValueError, OSError):
                    pass
        try:
            import subprocess
            subprocess.run(["tmux", "-S", env["TMUX_SOCKET"], "kill-server"],
                            capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            r = redis.Redis.from_url(env["REDIS_URL"])
            keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                r.delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_telegram_bot_launcher_exits_cleanly_with_a_clear_error_when_api_token_missing():
    # Doesn't need Redis or a real API -- fails before either is touched.
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_telegram_launcher_")
    try:
        import subprocess
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "h-app")
        env.pop("API_TOKEN", None)
        env.pop("H_MESH_API_TOKEN", None)
        res = subprocess.run(
            [sys.executable, "-m", "services.telegram_bot"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert res.returncode == 1
        assert "API token required" in res.stderr
        assert "Traceback" not in res.stderr
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
