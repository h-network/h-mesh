"""`office purge` and `modules.tmux.ops.purge_session_history`.

Destructive code, so every test works inside tmp_path only: HOME and the agent
workdir root are both pointed there, and nothing here touches a real history.
"""

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from modules.office.cli import main as office_main
from modules.tmux import ops
from modules.tmux.ops import PurgeError, has_session_history, purge_session_history

POD = "acme"
TENANT = "hq"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("H_MESH_WORKDIR", str(tmp_path / "work"))
    return h


def _cwd(agent, tmp_path):
    return str(tmp_path / "work" / agent)


def _claude_project(home, cwd, profile=None):
    d = home / f".claude{'-' + profile if profile else ''}" / "projects" / cwd.replace("/", "-")
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text('{"a":1}\n')
    (d / "s2.jsonl").write_text('{"a":2}\n')
    (d / "sub").mkdir()
    (d / "sub" / "x.txt").write_text("x")
    return d


def _codex_rollout(home, cwd, name, profile=None, meta=True):
    d = home / f".codex{'-' + profile if profile else ''}" / "sessions" / "2026" / "11" / "02"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    first = {"type": "session_meta", "payload": {"cwd": cwd}} if meta else {"type": "other"}
    p.write_text(json.dumps(first) + '\n{"more":1}\n')
    return p


def _agy_file(home):
    d = home / ".gemini" / "antigravity-cli"
    d.mkdir(parents=True)
    return d / "history.jsonl"


# --- claude ---------------------------------------------------------------


def test_claude_removes_only_this_agents_project_dir(home, tmp_path):
    mine = _claude_project(home, _cwd("worker", tmp_path))
    other = _claude_project(home, _cwd("worker2", tmp_path))
    other_profile = _claude_project(home, _cwd("worker", tmp_path), profile="acct")
    assert has_session_history("worker", "claude")
    assert purge_session_history("worker", "claude") == 2
    assert not mine.exists()
    assert other.is_dir() and (other / "s1.jsonl").exists()
    assert other_profile.is_dir()
    assert mine.parent.is_dir()
    assert not has_session_history("worker", "claude")


def test_claude_profile_selects_profile_dir(home, tmp_path):
    default = _claude_project(home, _cwd("worker", tmp_path))
    prof = _claude_project(home, _cwd("worker", tmp_path), profile="acct")
    assert purge_session_history("worker", "claude", profile="acct") == 2
    assert not prof.exists()
    assert default.is_dir()


def test_claude_nothing_to_purge_is_zero(home):
    assert purge_session_history("worker", "claude") == 0


def test_claude_symlinked_project_dir_is_refused_and_target_kept(home, tmp_path):
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.jsonl").write_text("x\n")
    projects = home / ".claude" / "projects"
    projects.mkdir(parents=True)
    (projects / _cwd("worker", tmp_path).replace("/", "-")).symlink_to(target)
    with pytest.raises(PurgeError, match="symlink"):
        purge_session_history("worker", "claude")
    assert (target / "keep.jsonl").exists()


# --- codex ----------------------------------------------------------------


def test_codex_removes_only_rollouts_whose_cwd_matches(home, tmp_path):
    mine = _codex_rollout(home, _cwd("worker", tmp_path), "rollout-1.jsonl")
    mine2 = _codex_rollout(home, _cwd("worker", tmp_path), "rollout-2.jsonl")
    other = _codex_rollout(home, _cwd("worker2", tmp_path), "rollout-3.jsonl")
    no_meta = _codex_rollout(home, _cwd("worker", tmp_path), "rollout-4.jsonl", meta=False)
    not_rollout = _codex_rollout(home, _cwd("worker", tmp_path), "notes.jsonl")
    other_profile = _codex_rollout(home, _cwd("worker", tmp_path), "rollout-5.jsonl", profile="acct")
    garbage = mine.parent / "rollout-6.jsonl"
    garbage.write_bytes(b"\xff\xfe not json\n")
    not_a_dict = mine.parent / "rollout-7.jsonl"
    not_a_dict.write_text("[1, 2]\n")
    assert purge_session_history("worker", "codex") == 2
    assert not mine.exists() and not mine2.exists()
    for kept in (other, no_meta, not_rollout, other_profile, garbage, not_a_dict):
        assert kept.exists(), kept
    assert not has_session_history("worker", "codex")


def test_codex_symlinked_rollout_is_not_followed(home, tmp_path):
    real = tmp_path / "elsewhere.jsonl"
    real.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": _cwd("worker", tmp_path)}}) + "\n")
    d = home / ".codex" / "sessions"
    d.mkdir(parents=True)
    (d / "rollout-x.jsonl").symlink_to(real)
    assert purge_session_history("worker", "codex") == 0
    assert real.exists() and (d / "rollout-x.jsonl").is_symlink()


def test_codex_symlinked_subdir_is_not_walked(home, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "rollout-v.jsonl"
    victim.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": _cwd("worker", tmp_path)}}) + "\n")
    d = home / ".codex" / "sessions"
    d.mkdir(parents=True)
    (d / "link").symlink_to(outside)
    assert purge_session_history("worker", "codex") == 0
    assert victim.exists()


def test_codex_no_sessions_dir_is_zero(home):
    assert purge_session_history("worker", "codex") == 0


# --- agy ------------------------------------------------------------------


def _line(workspace, n=0):
    return json.dumps({"workspace": workspace, "n": n}) + "\n"


def test_agy_rewrites_without_this_workspace_and_keeps_everything_else_byte_for_byte(home, tmp_path):
    f = _agy_file(home)
    mine = _cwd("worker", tmp_path)
    other = _cwd("worker2", tmp_path)
    original_mode = 0o640
    content = (
        _line(other, 1) + _line(mine, 2) + "not json at all\n" + _line(mine, 3)
        + '{"no_workspace":true}\n' + _line(other, 4).rstrip("\n")
    )
    f.write_text(content)
    os.chmod(f, original_mode)
    assert purge_session_history("worker", "agy") == 2
    assert f.read_text() == _line(other, 1) + "not json at all\n" + '{"no_workspace":true}\n' + _line(other, 4).rstrip("\n")
    assert stat.S_IMODE(f.stat().st_mode) == original_mode
    assert [p.name for p in f.parent.iterdir()] == ["history.jsonl"]
    assert not has_session_history("worker", "agy")


def test_agy_no_matching_lines_leaves_the_file_untouched(home, tmp_path):
    f = _agy_file(home)
    f.write_text(_line(_cwd("worker2", tmp_path)))
    before = f.stat()
    assert purge_session_history("worker", "agy") == 0
    after = f.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_agy_missing_file_is_zero(home):
    assert purge_session_history("worker", "agy") == 0


def test_agy_retries_when_the_file_changes_during_rewrite(home, tmp_path):
    f = _agy_file(home)
    mine = _cwd("worker", tmp_path)
    other = _cwd("worker2", tmp_path)
    f.write_text(_line(mine, 1) + _line(other, 2))
    late = _line(other, 99)
    real_fsync = ops.os.fsync
    appended = []

    def fsync_then_append(fd):
        real_fsync(fd)
        if not appended:  # another agy agent appends once, mid-rewrite
            appended.append(True)
            with f.open("a") as h:
                h.write(late)

    with patch.object(ops.os, "fsync", fsync_then_append):
        assert purge_session_history("worker", "agy") == 1
    assert f.read_text() == _line(other, 2) + late


def test_agy_gives_up_and_removes_nothing_if_the_file_never_settles(home, tmp_path):
    f = _agy_file(home)
    mine = _cwd("worker", tmp_path)
    f.write_text(_line(mine, 1))
    original = f.read_text()
    real_fsync = ops.os.fsync
    count = []

    def fsync_then_append(fd):
        real_fsync(fd)
        count.append(1)
        with f.open("a") as h:
            h.write(_line("/elsewhere", len(count)))

    with patch.object(ops.os, "fsync", fsync_then_append):
        with pytest.raises(PurgeError, match="kept changing"):
            purge_session_history("worker", "agy")
    assert f.read_text().startswith(original)
    assert _line(mine, 1) in f.read_text()
    assert [p.name for p in f.parent.iterdir()] == ["history.jsonl"]


# --- scoping / validation -----------------------------------------------------


@pytest.mark.parametrize("profile", [
    pytest.param("../x", id="dotdot-slash"),
    pytest.param("a/b", id="slash"),
    pytest.param("..", id="dotdot"),
    pytest.param("a b", id="space"),
])
def test_bad_profile_is_refused(home, tmp_path, profile):
    victim = home / "x"
    victim.mkdir()
    with pytest.raises(PurgeError, match="invalid profile"):
        purge_session_history("worker", "claude", profile=profile)
    assert victim.is_dir()


def test_empty_profile_means_the_default_store(home, tmp_path):
    default = _claude_project(home, _cwd("worker", tmp_path))
    assert purge_session_history("worker", "claude", profile="") == 2
    assert not default.exists()


def test_unknown_cli_is_refused(home):
    with pytest.raises(PurgeError, match="unknown cli"):
        purge_session_history("worker", "bash")


@pytest.mark.parametrize("cwd", [
    pytest.param("/", id="root"),
    pytest.param("relative/dir", id="relative"),
    pytest.param("/a/../b", id="dotdot"),
    pytest.param("/a//b", id="double-slash"),
])
def test_odd_working_directories_are_refused(home, cwd):
    with pytest.raises(PurgeError, match="refusing"):
        purge_session_history("worker", "claude", cwd=cwd)


def test_explicit_home_root_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("H_MESH_WORKDIR", str(tmp_path / "work"))
    alt = tmp_path / "alt"
    d = _claude_project(alt, str(tmp_path / "work" / "worker"))
    assert purge_session_history("worker", "claude", home_root=alt) == 2
    assert not d.exists()


# --- office purge ---------------------------------------------------------


class _Registry:
    def __init__(self, members=(), error=None):
        self.members = set(members)
        self.error = error

    def hexists(self, key, field):
        if self.error:
            raise self.error
        return field in self.members


def _run(monkeypatch, argv, registry, caller="architect"):
    monkeypatch.setenv("AGENT_NAME", caller)
    monkeypatch.setenv("POD", POD)
    monkeypatch.setenv("TENANT", TENANT)
    with patch("modules.office.cli.redis.Redis.from_url", return_value=registry):
        office_main(argv)


def test_office_purge_deletes_a_retired_agents_history(home, tmp_path, monkeypatch, capsys):
    d = _claude_project(home, _cwd("worker", tmp_path))
    _run(monkeypatch, ["purge", "worker", "--cli", "claude", "--confirm", "worker"], _Registry(["architect"]))
    assert "purged 2 sessions for worker (claude)" in capsys.readouterr().out
    assert not d.exists()


def test_office_purge_passes_profile_through(home, tmp_path, monkeypatch, capsys):
    default = _claude_project(home, _cwd("worker", tmp_path))
    prof = _claude_project(home, _cwd("worker", tmp_path), profile="acct")
    _run(monkeypatch, ["purge", "worker", "--cli", "claude", "--profile", "acct", "--confirm", "worker"], _Registry())
    assert not prof.exists() and default.exists()


def test_office_purge_agy_reports_lines(home, tmp_path, monkeypatch, capsys):
    f = _agy_file(home)
    f.write_text(_line(_cwd("worker", tmp_path)) + _line("/other"))
    _run(monkeypatch, ["purge", "worker", "--cli", "agy", "--confirm", "worker"], _Registry())
    assert "purged 1 history line for worker (agy)" in capsys.readouterr().out
    assert f.read_text() == _line("/other")


def test_office_purge_refuses_an_enrolled_agent(home, tmp_path, monkeypatch, capsys):
    d = _claude_project(home, _cwd("worker", tmp_path))
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["purge", "worker", "--cli", "claude", "--confirm", "worker"], _Registry(["worker"]))
    assert exc.value.code == 1
    assert "still enrolled" in capsys.readouterr().err
    assert (d / "s1.jsonl").exists()


def test_office_purge_fails_closed_when_the_registry_is_unreadable(home, tmp_path, monkeypatch, capsys):
    d = _claude_project(home, _cwd("worker", tmp_path))
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["purge", "worker", "--cli", "claude", "--confirm", "worker"],
             _Registry(error=redis.ConnectionError("down")))
    assert exc.value.code == 1
    assert "cannot confirm" in capsys.readouterr().err
    assert (d / "s1.jsonl").exists()


@pytest.mark.parametrize("confirm", [
    pytest.param("worker2", id="other-agent"),
    pytest.param("Worker", id="case"),
    pytest.param("work", id="prefix"),
    pytest.param("", id="empty"),
])
def test_office_purge_requires_the_exact_name_repeated(home, tmp_path, monkeypatch, capsys, confirm):
    d = _claude_project(home, _cwd("worker", tmp_path))
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["purge", "worker", "--cli", "claude", "--confirm", confirm], _Registry())
    assert exc.value.code == 1
    assert "--confirm must repeat" in capsys.readouterr().err
    assert (d / "s1.jsonl").exists()


@pytest.mark.parametrize("name", [
    pytest.param("../x", id="dotdot-slash"),
    pytest.param("wor*", id="glob"),
    pytest.param("a/b", id="slash"),
    pytest.param(".", id="dot"),
])
def test_office_purge_rejects_names_that_are_not_one_agent(home, tmp_path, monkeypatch, capsys, name):
    victim = home / ".claude" / "projects" / "keep"
    victim.mkdir(parents=True)
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["purge", name, "--cli", "claude", "--confirm", name], _Registry())
    assert exc.value.code == 1
    assert "invalid agent name" in capsys.readouterr().err
    assert victim.is_dir()


@pytest.mark.parametrize("argv", [
    pytest.param(["purge", "worker", "--confirm", "worker"], id="no-cli"),
    pytest.param(["purge", "worker", "--cli", "claude"], id="no-confirm"),
    pytest.param(["purge", "worker", "--cli", "bash", "--confirm", "worker"], id="bad-cli"),
    pytest.param(["purge", "--cli", "claude", "--confirm", "worker"], id="no-agent"),
])
def test_office_purge_usage_errors_remove_nothing(home, tmp_path, monkeypatch, argv):
    d = _claude_project(home, _cwd("worker", tmp_path))
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, argv, _Registry())
    assert exc.value.code == 2
    assert (d / "s1.jsonl").exists()


def test_office_purge_reports_a_refusal_from_the_history_code(home, tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, ["purge", "worker", "--cli", "claude", "--profile", "../x", "--confirm", "worker"], _Registry())
    assert exc.value.code == 1
    assert "purge failed: invalid profile" in capsys.readouterr().err
