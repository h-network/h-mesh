import subprocess
import sys
from pathlib import Path

from services.tenant_config import read_tenant_env, tenant_env_path, write_tenant_env

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_write_then_read_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("H_MESH_STATE_DIR", str(tmp_path))
    write_tenant_env("mytenant", {"POD": "mypod", "AGENTS": "architect,sme-2", "EMPTY": ""})

    values = read_tenant_env("mytenant")
    assert values == {"POD": "mypod", "AGENTS": "architect,sme-2"}


def test_write_tenant_env_is_chmod_600(monkeypatch, tmp_path):
    monkeypatch.setenv("H_MESH_STATE_DIR", str(tmp_path))
    write_tenant_env("mytenant", {"CLAUDE_OAUTH_TOKEN_DEFAULT": "secret"})

    mode = tenant_env_path("mytenant").stat().st_mode & 0o777
    assert mode == 0o600


def test_write_tenant_env_overwrites_whole_file(monkeypatch, tmp_path):
    monkeypatch.setenv("H_MESH_STATE_DIR", str(tmp_path))
    write_tenant_env("mytenant", {"A": "1", "B": "2"})
    write_tenant_env("mytenant", {"A": "1"})

    assert read_tenant_env("mytenant") == {"A": "1"}


def test_read_tenant_env_missing_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("H_MESH_STATE_DIR", str(tmp_path))
    assert read_tenant_env("nope") == {}


def test_cli_get_and_set_roundtrip(monkeypatch, tmp_path):
    env = {"H_MESH_STATE_DIR": str(tmp_path), "PYTHONPATH": str(REPO_ROOT / "h-app")}
    res = subprocess.run(
        [sys.executable, "-m", "services.tenant_config", "set", "mytenant"],
        input="POD=mypod\nTENANT=mytenant\n",
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr

    res = subprocess.run(
        [sys.executable, "-m", "services.tenant_config", "get", "mytenant", "POD"],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "mypod"

    res = subprocess.run(
        [sys.executable, "-m", "services.tenant_config", "get", "mytenant", "MISSING", "fallback"],
        env=env, capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "fallback"
