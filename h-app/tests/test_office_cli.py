import json
import sys
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.keys import prefix
from modules.office import cli as office_cli
from modules.office.cli import main as office_main


POD = "acme"
TENANT = "hq"


class FakeRedis:
    """A minimal in-memory double covering exactly what office/cli.py calls."""

    def __init__(self, *, registry=None):
        self.values: dict[str, object] = {}
        self.hashes: dict[str, dict] = defaultdict(dict)
        self.lists: dict[str, list] = defaultdict(list)
        self.streams: dict[str, list] = defaultdict(list)
        registry_key = prefix(POD, TENANT, resource="registry")
        for agent, pt in (registry or {}).items():
            self.hashes[registry_key][agent] = pt

    # --- hashes (registry) ---
    def hkeys(self, key):
        return list(self.hashes[key].keys())

    def hexists(self, key, field):
        return field in self.hashes[key]

    def hget(self, key, field):
        return self.hashes[key].get(field)

    def hgetall(self, key):
        return dict(self.hashes[key])

    def hset(self, key, field=None, value=None, mapping=None):
        if mapping:
            self.hashes[key].update(mapping)
            return len(mapping)
        self.hashes[key][field] = value
        return 1

    def hdel(self, key, *fields):
        count = 0
        for field in fields:
            if field in self.hashes[key]:
                del self.hashes[key][field]
                count += 1
        return count

    # --- values ---
    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    # --- lists (board) ---
    def rpush(self, key, value):
        self.lists[key].append(value)
        return len(self.lists[key])

    def lpop(self, key):
        if self.lists[key]:
            return self.lists[key].pop(0)
        return None

    def lrange(self, key, start, end):
        items = self.lists[key]
        if end == -1:
            return list(items[start:])
        return list(items[start:end + 1])

    def llen(self, key):
        return len(self.lists[key])

    def lrem(self, key, count, value):
        items = self.lists[key]
        if value in items:
            items.remove(value)
            return 1
        return 0

    # --- streams (usage) ---
    def xrange(self, key, min="-", max="+"):
        return list(self.streams[key])


def _member(r, agent, port_type="tmux"):
    r.hashes[prefix(POD, TENANT, resource="registry")][agent] = port_type


def _env(monkeypatch, agent="architect"):
    monkeypatch.setenv("AGENT_NAME", agent)
    monkeypatch.setenv("POD", POD)
    monkeypatch.setenv("TENANT", TENANT)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


def test_dispatch_table_has_no_duplicate_names_and_matches_derived_maps():
    names = office_cli._COMMANDS
    assert len(names) == len(set(names))
    assert set(office_cli._DESCRIPTIONS) == set(names)
    assert set(office_cli._DISPATCH) == set(names)


def test_profiles_is_not_a_command_yet():
    # available_profiles() has no h-mesh equivalent -- deliberately absent,
    # not an oversight.
    assert "profiles" not in office_cli._COMMANDS


def test_root_help_lists_every_command_without_environment_or_redis(capsys):
    office_main([])
    out = capsys.readouterr().out
    for name in office_cli._COMMANDS:
        assert name in out


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


@patch("modules.office.cli.send")
def test_send_refuses_unknown_agent(mock_send, monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["send", "-a", "ghost", "hi"])
    assert "unknown destination agent" in capsys.readouterr().err
    mock_send.assert_not_called()


@patch("modules.office.cli.send")
def test_send_builds_message_envelope(mock_send, monkeypatch, capsys):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis(registry={"backend": "tmux"})
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["send", "-a", "backend", "hello there"])
    kwargs = mock_send.call_args[1]
    assert kwargs["destination"] == "backend"
    assert kwargs["payload"] == {"text": "hello there"}
    assert kwargs["kind"] == "Message"
    assert "sent to backend: 11 bytes (stream-1)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# hire / letGo / pause / resume (_lifecycle_command)
# ---------------------------------------------------------------------------


@patch("modules.office.cli.send")
def test_hire_defaults_to_fresh_session(mock_send, monkeypatch):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hire", "worker-1", "--cli", "claude"])
        # Reusing the name must produce the same deterministic launch request;
        # local CLI history is deliberately irrelevant to the sending side.
        office_main(["hire", "worker-1", "--cli", "claude"])
    assert mock_send.call_count == 2
    kwargs = mock_send.call_args_list[-1].kwargs
    assert kwargs["destination"] == "host"
    assert kwargs["kind"] == "StartAgent"
    assert kwargs["payload"] == {
        "agent": "worker-1",
        "cli": "claude",
        "resume": False,
    }


@patch("modules.office.cli.send")
def test_hire_carries_profile_provider_resume_permissions_and_tools(mock_send, monkeypatch):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main([
            "hire", "worker-1", "--cli", "claude", "--profile", "work",
            "--provider", "anthropic", "--resume", "--skip-permissions",
            "--claude-tools", "",
        ])
    payload = mock_send.call_args[1]["payload"]
    assert payload["profile"] == "work"
    assert payload["provider"] == "anthropic"
    assert payload["resume"] is True
    assert payload["skip_permissions"] is True
    assert payload["claude_tools"] == ""


@patch("modules.office.cli.send")
def test_hire_fresh_and_no_skip_permissions(mock_send, monkeypatch):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hire", "worker-1", "--cli", "claude", "--fresh", "--no-skip-permissions"])
    payload = mock_send.call_args[1]["payload"]
    assert payload["resume"] is False
    assert payload["skip_permissions"] is False


def test_hire_has_no_export_import_flags(capsys):
    with pytest.raises(SystemExit):
        office_main(["hire", "--help"])
    out = capsys.readouterr().out
    assert "--export" not in out
    assert "--import" not in out


@patch("modules.office.cli.send")
def test_let_go_aliases_share_the_lifecycle_contract(mock_send, monkeypatch):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["let-go", "worker-1"])
    assert mock_send.call_args[1]["kind"] == "StopAgent"
    assert mock_send.call_args[1]["payload"] == {"agent": "worker-1"}


# ---------------------------------------------------------------------------
# board: take / done / hold / list, via lib.board_interaction
# ---------------------------------------------------------------------------


def _ticket(agent, task_id="a1b2c3d4", title="do the thing", status="todo", **extra):
    ticket = {
        "v": 1, "id": task_id, "title": title, "description": "",
        "created_by": "architect", "status": status, "created_ts": "2026-08-31T00:00:00.000Z",
        "started_ts": None, "done_ts": None,
    }
    ticket.update(extra)
    return ticket


def test_take_moves_todo_into_doing_using_board_interaction_shape(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    r.lists[todo_key].append(json.dumps(_ticket("architect")))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["take"])
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    assert len(r.lists[doing_key]) == 1
    saved = json.loads(r.lists[doing_key][0])
    assert saved["status"] == "doing"
    assert saved["started_ts"] is not None
    out = capsys.readouterr().out
    assert '"status":"doing"' in out


def test_take_refuses_when_doing_nonempty(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[doing_key].append(json.dumps(_ticket("architect", status="doing")))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["take"])
    assert "already have one open task" in capsys.readouterr().err


def test_hold_then_list_shows_priority_and_age(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[doing_key].append(json.dumps(_ticket("architect", status="doing", priority="high")))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hold"])
        capsys.readouterr()
        office_main(["list"])
    out = capsys.readouterr().out
    assert "p:high" in out
    assert "a1b2c3d4  do the thing" in out


def test_malformed_board_entry_raises_office_error_not_board_error(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    r.lists[todo_key].append(json.dumps({"title": "no id here"}))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["take"])
    assert "office: error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# add -- unchanged behavior, AddTicket envelope only
# ---------------------------------------------------------------------------


@patch("modules.office.cli.send")
def test_add_sends_envelope_and_never_writes_recipient_board(mock_send, monkeypatch):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis(registry={"backend": "tmux"})
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["add", "-a", "backend", "-t", "title", "-d", "desc"])
    kwargs = mock_send.call_args[1]
    assert kwargs["kind"] == "AddTicket"
    assert kwargs["payload"]["title"] == "title"
    todo_key = prefix(POD, TENANT, "backend", "tasks.todo")
    assert r.lists[todo_key] == []


# ---------------------------------------------------------------------------
# cloneToAll
# ---------------------------------------------------------------------------


def test_clone_to_all_dry_run_reports_without_writing(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"backend": "tmux", "frontend": "tmux"})
    monkeypatch.setattr(office_cli, "_WORKDIR_ROOT", tmp_path)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["cloneToAll", "git@example.com:org/repo.git", "--dry-run"])
    out = capsys.readouterr().out
    assert "backend: would clone" in out
    assert "frontend: would clone" in out
    assert "summary: cloned=0 skipped=0 failed=0" in out


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def test_usage_empty_stream_prints_header_only(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["usage"])
    out = capsys.readouterr().out
    assert "agent" in out and "cli" in out and "model" in out


def test_usage_json_reports_total(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    usage_key = prefix(POD, TENANT, resource="usage")
    r.streams[usage_key].append((
        "1-0",
        {"usage": json.dumps({
            "agent": "architect", "cli": "claude", "model": "claude-sonnet-4",
            "input": 1000, "output": 500, "cache_read": 0, "cache_write": 0,
        })},
    ))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["usage", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["agent"] == "architect"
    assert payload["total_usd"] > 0


# ---------------------------------------------------------------------------
# peers / status / broadcast
# ---------------------------------------------------------------------------


def test_peers_prints_only_other_tmux_agents(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux", "api": "api"})
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["peers"])
    assert capsys.readouterr().out.strip() == "backend"


def test_peers_verbose_reads_launch_profile_and_current_task(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux"})
    r.values[prefix(POD, TENANT, "backend", "launch")] = "claude"
    r.values[prefix(POD, TENANT, "backend", "profile")] = "work"
    doing_key = prefix(POD, TENANT, "backend", "tasks.doing")
    r.lists[doing_key].append(json.dumps(_ticket("backend", status="doing")))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["peers", "-v"])
    out = capsys.readouterr().out
    assert "framework=claude" in out
    assert "profile=work" in out
    assert "do the thing" in out


def test_peers_interfaces_labels_api_and_office_separately(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux", "api": "api", "host": "office"})
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["peers", "-i"])
    out = capsys.readouterr().out
    assert "backend" in out.splitlines()[0]
    assert "api (api)" in out
    assert "host (office)" in out


def test_status_reports_unknown_with_no_activity_feed(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"backend": "tmux"})
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["status"])
    out = capsys.readouterr().out
    assert "unknown" in out
    assert "no activity feed" in out


@patch("modules.office.cli.send")
def test_broadcast_resolves_tmux_peers_without_self(mock_send, monkeypatch):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux", "frontend": "tmux", "api": "api"})
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["broadcast", "standup", "in", "five"])
    destinations = {call.kwargs["destination"] for call in mock_send.call_args_list}
    assert destinations == {"backend", "frontend"}
    assert mock_send.call_args_list[0].kwargs["payload"] == {"text": "standup in five"}
