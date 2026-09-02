"""End-to-end confirmation that `office hire --wait` actually distinguishes
confirmed/failed/unknown against a REAL switch+reconciler pair, not just the
FakeRedis-backed unit tests in test_office_cli.py. Real incident: setup.sh's
roster-hire loop printed "hired" for any exit 0 from `hire`, which only ever
proved the StartAgent envelope was durably enqueued (ADMITTED) -- never that
the agent actually registered (CREATED). This exercises the actual mesh
(a real hired agent's registry row, a real dead-letter rejection) end to end.
"""

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import redis

from core.keys import prefix
from services.daemons import DAEMON_MODULES, start_daemons

REPO_ROOT = Path(__file__).resolve().parents[2]


def _skip_unless_redis() -> None:
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    try:
        redis.Redis.from_url(redis_url).ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")


def _office_env(tmpdir: str, pod: str, tenant: str) -> dict:
    fake_bin = os.path.join(tmpdir, "bin")
    os.makedirs(fake_bin, exist_ok=True)
    fake_h_agent = os.path.join(fake_bin, "h-agent")
    with open(fake_h_agent, "w") as f:
        f.write("#!/usr/bin/env bash\nexec bash -il\n")
    os.chmod(fake_h_agent, 0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["PYTHONPATH"] = str(REPO_ROOT / "h-app")
    env["POD"] = pod
    env["TENANT"] = tenant
    env["REDIS_URL"] = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    env["PYTHONUNBUFFERED"] = "1"
    env["TMUX_SESSION"] = f"sess-{os.urandom(4).hex()}"
    env["TMUX_TMPDIR"] = tmpdir
    env["TMUX_SOCKET"] = os.path.join(tmpdir, "isolated.sock")
    return env


def test_hire_wait_confirms_a_real_agent_end_to_end():
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_hire_wait_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _office_env(tmpdir, pod, tenant)
    r = redis.Redis.from_url(env["REDIS_URL"])

    try:
        pids = start_daemons(python=python, run_dir=run_dir, env=env)
        assert set(pids) == set(DAEMON_MODULES)

        registry_key = prefix(pod, tenant, resource="registry")
        r.hset(registry_key, mapping={"host": "office"})

        hire_env = dict(env)
        hire_env["AGENT_NAME"] = "host"
        res = subprocess.run(
            [str(python), "-m", "modules.office.cli", "hire", "worker1", "--cli", "claude", "--wait"],
            env=hire_env, capture_output=True, text=True, timeout=40,
        )
        assert res.returncode == 0, f"stdout: {res.stdout}\nstderr: {res.stderr}"
        assert "confirmed: worker1 registered" in res.stdout
    finally:
        for pidfile in run_dir.glob("*.pid") if run_dir.is_dir() else []:
            try:
                os.kill(int(pidfile.read_text().strip()), 9)
            except (ValueError, OSError):
                pass
        try:
            subprocess.run(["tmux", "-S", env["TMUX_SOCKET"], "kill-server"],
                            capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                r.delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_hire_wait_fails_end_to_end_on_a_real_rejection_not_a_timeout():
    # A malformed --profile (invalid segment characters) makes
    # lib.agentlifecycle.start_agent raise a real ValueError server-side --
    # a genuine rejection, not just "nothing happened yet".
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_hire_wait_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _office_env(tmpdir, pod, tenant)
    r = redis.Redis.from_url(env["REDIS_URL"])

    try:
        pids = start_daemons(python=python, run_dir=run_dir, env=env)
        assert set(pids) == set(DAEMON_MODULES)

        registry_key = prefix(pod, tenant, resource="registry")
        r.hset(registry_key, mapping={"host": "office"})

        hire_env = dict(env)
        hire_env["AGENT_NAME"] = "host"
        res = subprocess.run(
            [str(python), "-m", "modules.office.cli", "hire", "worker1", "--cli", "claude",
             "--profile", "bad profile!", "--wait", "15"],
            env=hire_env, capture_output=True, text=True, timeout=40,
        )
        assert res.returncode == 1, f"stdout: {res.stdout}\nstderr: {res.stderr}"
        assert "failed: worker1 was not registered" in res.stderr
    finally:
        for pidfile in run_dir.glob("*.pid") if run_dir.is_dir() else []:
            try:
                os.kill(int(pidfile.read_text().strip()), 9)
            except (ValueError, OSError):
                pass
        try:
            subprocess.run(["tmux", "-S", env["TMUX_SOCKET"], "kill-server"],
                            capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                r.delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_hire_wait_never_confirms_a_rejected_rehire_of_an_already_registered_agent_end_to_end():
    # Reviewer FAILED an earlier version of this branch on exactly this,
    # reproduced against real FakeRedis-seeded state; this proves the same
    # harm is closed against the real switch/lifecycle pipeline, not just a
    # mocked one. Hire worker1 for real first (so it's genuinely
    # registered), then re-hire the SAME agent with a malformed --profile
    # -- a real rejection -- and confirm the second call is never told
    # "confirmed" despite the agent still being tmux-registered throughout
    # (from the first, successful hire).
    _skip_unless_redis()
    tmpdir = tempfile.mkdtemp(prefix="h_mesh_test_hire_wait_")
    run_dir = Path(tmpdir) / "run"
    pod = f"testpod-{os.urandom(4).hex()}"
    tenant = f"testtenant-{os.urandom(4).hex()}"
    python = Path(sys.executable)
    env = _office_env(tmpdir, pod, tenant)
    r = redis.Redis.from_url(env["REDIS_URL"])

    try:
        pids = start_daemons(python=python, run_dir=run_dir, env=env)
        assert set(pids) == set(DAEMON_MODULES)

        registry_key = prefix(pod, tenant, resource="registry")
        r.hset(registry_key, mapping={"host": "office"})

        hire_env = dict(env)
        hire_env["AGENT_NAME"] = "host"
        first = subprocess.run(
            [str(python), "-m", "modules.office.cli", "hire", "worker1", "--cli", "claude", "--wait"],
            env=hire_env, capture_output=True, text=True, timeout=40,
        )
        assert first.returncode == 0, f"stdout: {first.stdout}\nstderr: {first.stderr}"
        assert "confirmed: worker1 registered" in first.stdout

        second = subprocess.run(
            [str(python), "-m", "modules.office.cli", "hire", "worker1", "--cli", "claude",
             "--profile", "bad profile!", "--wait", "15"],
            env=hire_env, capture_output=True, text=True, timeout=40,
        )
        assert "confirmed" not in second.stdout, (
            f"told confirmed for a rejected re-hire of an already-registered agent:\n{second.stdout}"
        )
        assert second.returncode == 1, f"stdout: {second.stdout}\nstderr: {second.stderr}"
        assert "failed: worker1 was not registered" in second.stderr
    finally:
        for pidfile in run_dir.glob("*.pid") if run_dir.is_dir() else []:
            try:
                os.kill(int(pidfile.read_text().strip()), 9)
            except (ValueError, OSError):
                pass
        try:
            subprocess.run(["tmux", "-S", env["TMUX_SOCKET"], "kill-server"],
                            capture_output=True, timeout=5)
        except Exception:
            pass
        try:
            keys = r.keys(f"pod:{pod}:tenant:{tenant}:*") or []
            if keys:
                r.delete(*keys)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)
