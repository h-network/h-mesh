import json
import os
import sys
import threading
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import pytest
import redis

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

    def lindex(self, key, index):
        try:
            return self.lists[key][index]
        except IndexError:
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

    def eval(self, script, key_count, *args):
        keys = args[:key_count]
        argv = args[key_count:]
        if "office preflighted task transition" in script:
            source_key, doing_key = keys
            raw, serialized, require_empty = argv
            if require_empty == "1" and self.lists[doing_key]:
                return [0, "busy"]
            if raw not in self.lists[source_key]:
                return [0, "changed"]
            self.lists[source_key].remove(raw)
            self.lists[doing_key].append(serialized)
            return [1, "ok"]
        if "office atomic task rewrite" in script:
            key = keys[0]
            raw, serialized = argv
            try:
                index = self.lists[key].index(raw)
            except ValueError:
                return 0
            self.lists[key][index] = serialized
            return 1
        raise AssertionError("unexpected Lua script")

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
# lifecycle commands (_lifecycle_command)
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
def test_hire_can_transfer_leadership(mock_send, monkeypatch):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hire", "replacement", "--lead"])
    assert mock_send.call_args.kwargs["payload"]["lead"] is True


def test_peers_warns_when_configured_lead_is_not_enrolled(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "worker": "tmux"})
    r.values[prefix(POD, TENANT, resource="lead")] = "retired-lead"
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["peers"])
    captured = capsys.readouterr()
    assert "worker" in captured.out
    assert "configured lead 'retired-lead' is not an enrolled agent" in captured.err


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


def test_return_moves_doing_ticket_to_back_of_todo(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    queued = json.dumps(_ticket("architect", task_id="queued"))
    r.lists[todo_key].append(queued)
    r.lists[doing_key].append(
        json.dumps(_ticket("architect", status="doing", started_ts="2026-09-01T00:00:00Z"))
    )

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["return"])

    assert r.lists[doing_key] == []
    assert r.lists[todo_key][0] == queued
    returned = json.loads(r.lists[todo_key][1])
    assert returned["status"] == "todo"
    assert returned["started_ts"] is None


def test_done_requires_and_lists_outcome(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[doing_key].append(json.dumps(_ticket("architect", status="doing")))

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["done"])
        assert r.lists[doing_key]
        office_main(["done", "--outcome", "failed"])
        capsys.readouterr()
        office_main(["list"])

    assert "outcome:failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "command",
    [
        ["return"],
        ["hold", "--reason", "blocked"],
        ["done", "--outcome", "failed"],
        ["cancel"],
    ],
)
def test_board_moves_preserve_doing_ticket_when_transition_fails_before_execution(
    command, monkeypatch
):
    """A pre-execution transport error leaves the source untouched.

    This proves helper routing and preservation before Lua begins. It does not
    claim client certainty after Redis executes but its response is lost; that
    boundary remains outcome-unknown to the caller.
    """
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    original = json.dumps(_ticket("architect", status="doing"))
    r.lists[doing_key].append(original)

    def fail_atomic_move(*args):
        if "office preflighted task transition" in args[0]:
            raise ConnectionError("injected pre-execution failure")
        raise AssertionError("unexpected script")

    r.eval = fail_atomic_move
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(ConnectionError, match="injected pre-execution failure"):
            office_main(command)

    assert r.lists[doing_key] == [original]


def test_take_preserves_todo_ticket_when_atomic_transition_fails(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    original = json.dumps(_ticket("architect"))
    r.lists[todo_key].append(original)
    r.eval = lambda *args: (_ for _ in ()).throw(ConnectionError("injected failure"))

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(ConnectionError, match="injected failure"):
            office_main(["take"])

    assert r.lists[todo_key] == [original]


def test_real_redis_wrongtype_transition_preserves_source_and_destination():
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        r.ping()
    except Exception:
        pytest.skip("Redis server not available at REDIS_URL")

    suffix = os.urandom(8).hex()
    source_key = f"test:office:transition:{suffix}:source"
    destination_key = f"test:office:transition:{suffix}:destination"
    raw = b'{"id":"ticket","title":"must survive"}'
    replacement = b'{"id":"ticket","title":"replacement"}'
    try:
        r.rpush(source_key, raw)
        r.set(destination_key, b"wrong-type-sentinel")

        with pytest.raises(
            office_cli.OfficeError, match="destination task list has wrong Redis type"
        ):
            office_cli._transition_selected(
                r,
                source_key=source_key,
                destination_key=destination_key,
                raw=raw,
                replacement=replacement,
            )

        assert r.lrange(source_key, 0, -1) == [raw]
        assert r.get(destination_key) == b"wrong-type-sentinel"
    finally:
        r.delete(source_key, destination_key)


def test_plain_done_prompts_for_outcome_in_legacy_interactive_guides(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    done_key = prefix(POD, TENANT, "architect", "tasks.done")
    r.lists[doing_key].append(json.dumps(_ticket("architect", status="doing")))

    with (
        patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")),
        patch("modules.office.cli.sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="completed"),
    ):
        office_main(["done"])

    assert json.loads(r.lists[done_key][0])["outcome"] == "completed"


def test_retitle_rewrites_open_ticket_in_place_and_preserves_position(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    first = json.dumps(_ticket("architect", task_id="first", title="stale premise"))
    second = json.dumps(_ticket("architect", task_id="second", title="next work"))
    r.lists[todo_key].extend([first, second])

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["retitle", "first", "--title", "corrected premise"])

    rewritten = json.loads(r.lists[todo_key][0])
    assert rewritten["id"] == "first"
    assert rewritten["title"] == "corrected premise"
    assert json.loads(r.lists[todo_key][1])["id"] == "second"


@pytest.mark.parametrize(
    "ticket",
    [
        _ticket(
            "architect",
            status="doing",
            title="stale premise",
            external_ref="must-survive",
            future_metadata={"owner": "ops"},
        ),
        {
            "v": 1,
            "id": "legacy-id",
            "title": "stale premise",
            "description": "legacy shape",
            "from": "architect",
            "status": "doing",
            "created_at": "2026-08-31T00:00:00.000Z",
            "custom_legacy_flag": True,
        },
    ],
)
def test_retitle_changes_only_title_in_full_stored_object(ticket, monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[doing_key].append(json.dumps(ticket))

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["retitle", "--title", "corrected premise"])

    rewritten = json.loads(r.lists[doing_key][0])
    assert rewritten["title"] == "corrected premise"
    assert {key: value for key, value in rewritten.items() if key != "title"} == {
        key: value for key, value in ticket.items() if key != "title"
    }


def test_retitle_changes_only_raw_title_token(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    raw = '''{
  "v": 1,
  "id": "a1b2c3d4",
  "title": "stale premise",
  "description": "literal café",
  "from": "architect",
  "status": "doing",
  "created_at": "2026-08-31T00:00:00.000Z",
  "extension_number": 1e+00,
  "future_metadata": {"owner": "ops"},
  "explicit_null": null
}'''
    raw_bytes = raw.encode()
    expected = raw.replace('"stale premise"', '"corrected premise"', 1).encode()
    r.lists[doing_key].append(raw_bytes)

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["retitle", "--title", "corrected premise"])

    assert r.lists[doing_key] == [expected]


def test_retitle_replaces_escaped_title_token_not_matching_description(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    raw = (
        '{"id":"a1b2c3d4","description":"stale \\"premise\\"",'
        '"title":"stale \\"premise\\"","status":"doing"}'
    )
    # The first matching token belongs to description; only the parsed title
    # span may change.
    expected = raw[: raw.rfind('"stale \\"premise\\""')] + '"corrected premise"' + raw[
        raw.rfind('"stale \\"premise\\""') + len('"stale \\"premise\\""') :
    ]
    r.lists[doing_key].append(raw)

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["retitle", "--title", "corrected premise"])

    assert r.lists[doing_key] == [expected]


def test_retitle_refuses_duplicate_top_level_title_without_rewriting(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    raw = '{"id":"a1b2c3d4","title":"first","title":"effective","status":"doing"}'
    r.lists[doing_key].append(raw)

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["retitle", "--title", "replacement"])

    assert "exactly one top-level title" in capsys.readouterr().err
    assert r.lists[doing_key] == [raw]


def test_retitle_records_old_and_new_title_in_both_audit_channels(monkeypatch, tmp_path):
    _env(monkeypatch)
    task_record = tmp_path / "tasks.jsonl"
    window_log = tmp_path / "window.jsonl"
    monkeypatch.setenv("TASK_RECORD", str(task_record))
    monkeypatch.setenv("H_MESH_LOG_FILE", str(window_log))
    monkeypatch.setenv("H_MESH_LOG_QUIET", "1")
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[doing_key].append(
        json.dumps(_ticket("architect", status="doing", title="stale premise"))
    )

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["retitle", "--title", "corrected premise"])

    task_event = json.loads(task_record.read_text().splitlines()[-1])
    log_event = json.loads(window_log.read_text().splitlines()[-1])
    for event in (task_event, log_event):
        assert event["old_title"] == "stale premise"
        assert event["title"] == "corrected premise"


def test_retitle_preserves_old_title_when_rewrite_fails_before_execution(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    original = json.dumps(_ticket("architect", status="doing", title="stale premise"))
    r.lists[doing_key].append(original)
    r.eval = lambda *args: (_ for _ in ()).throw(ConnectionError("injected pre-execution failure"))

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(ConnectionError, match="injected pre-execution failure"):
            office_main(["retitle", "--title", "corrected premise"])

    assert r.lists[doing_key] == [original]


def test_retitle_does_not_edit_closed_ticket(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    done_key = prefix(POD, TENANT, "architect", "tasks.done")
    original = json.dumps(_ticket("architect", status="done", title="closed title"))
    r.lists[done_key].append(original)

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["retitle", "a1b2", "--title", "replacement"])

    assert "no task matches" in capsys.readouterr().err
    assert r.lists[done_key] == [original]


def test_show_prints_full_ticket_without_mutating_board(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    raw = json.dumps(
        _ticket("architect", description="full constraints and context", priority="high")
    )
    r.lists[todo_key].append(raw)
    before = list(r.lists[todo_key])

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["show", "a1b2"])

    shown = json.loads(capsys.readouterr().out)
    assert shown["description"] == "full constraints and context"
    assert r.lists[todo_key] == before
    assert shown["started_ts"] is None


def test_show_can_read_another_enrolled_agents_ticket(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"worker": "tmux"})
    hold_key = prefix(POD, TENANT, "worker", "tasks.hold")
    raw = json.dumps(
        _ticket("worker", status="hold", description="waiting on credentials")
    )
    r.lists[hold_key].append(raw)

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["show", "a1b2", "-a", "worker"])

    shown = json.loads(capsys.readouterr().out)
    assert shown["description"] == "waiting on credentials"
    assert r.lists[hold_key] == [raw]


def test_done_writes_outcome_to_both_audit_channels(monkeypatch, tmp_path):
    _env(monkeypatch)
    task_record = tmp_path / "tasks.jsonl"
    window_log = tmp_path / "window.jsonl"
    monkeypatch.setenv("TASK_RECORD", str(task_record))
    monkeypatch.setenv("H_MESH_LOG_FILE", str(window_log))
    monkeypatch.setenv("H_MESH_LOG_QUIET", "1")
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[doing_key].append(json.dumps(_ticket("architect", status="doing")))

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["done", "--outcome", "failed"])

    task_event = json.loads(task_record.read_text().splitlines()[-1])
    log_event = json.loads(window_log.read_text().splitlines()[-1])
    assert task_event["event"] == "done"
    assert task_event["outcome"] == "failed"
    assert log_event["event"] == "task_done"
    assert log_event["outcome"] == "failed"


def test_hold_then_list_shows_priority_and_age(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[doing_key].append(json.dumps(_ticket("architect", status="doing", priority="high")))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hold", "--reason", "waiting for dependency"])
        capsys.readouterr()
        office_main(["list"])
    out = capsys.readouterr().out
    assert "p:high" in out
    assert "a1b2c3d4  do the thing" in out


def test_hold_requires_and_lists_reason(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[doing_key].append(json.dumps(_ticket("architect", status="doing")))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hold", "--reason", "waiting for API credentials"])
        capsys.readouterr()
        office_main(["list"])
    assert "reason:waiting for API credentials" in capsys.readouterr().out


def test_list_accepts_legacy_hold_and_done_without_new_fields(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    keys = office_cli._task_keys(POD, TENANT, "architect")
    r.lists[keys["hold"]].append(json.dumps(_ticket("architect", status="hold")))
    r.lists[keys["done"]].append(json.dumps(_ticket("architect", task_id="legacy-done", status="done")))

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["list"])

    listed = capsys.readouterr().out
    assert "a1b2c3d4  do the thing" in listed
    assert "legacy-d  do the thing" in listed


def test_malformed_board_entry_raises_office_error_not_board_error(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    r.lists[todo_key].append(json.dumps({"title": "no id here"}))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["take"])
    assert "office: error:" in capsys.readouterr().err
    invalid_key = prefix(POD, TENANT, "architect", "tasks.invalid")
    assert r.lists[todo_key] == []
    assert r.lists[invalid_key] == [json.dumps({"title": "no id here"})]
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["list"])
    listed = capsys.readouterr().out
    assert "invalid:" in listed
    assert "malformed ticket preserved" in listed


def test_take_by_id_quarantines_malformed_entries_without_blocking_valid_match(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    invalid_key = prefix(POD, TENANT, "architect", "tasks.invalid")
    malformed = json.dumps({"title": "no id here"})
    r.lists[todo_key].extend([
        malformed,
        json.dumps(_ticket("architect", task_id="valid-id")),
    ])
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["take", "valid"])

    assert r.lists[invalid_key] == [malformed]
    assert r.lists[todo_key] == []
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    assert json.loads(r.lists[doing_key][0])["id"] == "valid-id"


def test_take_quarantines_invalid_utf8_bytes(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    invalid_key = prefix(POD, TENANT, "architect", "tasks.invalid")
    malformed = b"\xffnot-a-ticket"
    r.lists[todo_key].append(malformed)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["take"])

    assert "not a valid ticket" in capsys.readouterr().err
    assert r.lists[todo_key] == []
    assert r.lists[invalid_key] == [malformed]


def test_concurrent_takes_leave_exactly_one_doing_ticket(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis()
    todo_key = prefix(POD, TENANT, "architect", "tasks.todo")
    doing_key = prefix(POD, TENANT, "architect", "tasks.doing")
    r.lists[todo_key].extend([
        json.dumps(_ticket("architect", task_id="aaaaaaaa")),
        json.dumps(_ticket("architect", task_id="bbbbbbbb")),
    ])
    barrier = threading.Barrier(2)
    original_llen = r.llen

    def racing_llen(key):
        result = original_llen(key)
        if key == doing_key:
            barrier.wait(timeout=2)
        return result

    r.llen = racing_llen
    errors = []

    def take(reference):
        try:
            office_cli._take_command([reference])
        except BaseException as exc:
            errors.append(exc)

    with (
        patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")),
        patch("modules.office.cli.record_task_event"),
        patch("modules.office.cli._log_task"),
        patch("builtins.print"),
    ):
        threads = [
            threading.Thread(target=take, args=("aaaaaaaa",)),
            threading.Thread(target=take, args=("bbbbbbbb",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert len(r.lists[doing_key]) == 1
    assert len(r.lists[todo_key]) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], office_cli.OfficeError)
    assert "already have one open task" in str(errors[0])


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
# clone-to-all
# ---------------------------------------------------------------------------


def test_clone_to_all_dry_run_reports_without_writing(monkeypatch, tmp_path, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"backend": "tmux", "frontend": "tmux"})
    monkeypatch.setattr(office_cli, "get_workdir_root", lambda: str(tmp_path))
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["clone-to-all", "git@example.com:org/repo.git", "--dry-run"])
    out = capsys.readouterr().out
    assert "backend: would clone" in out
    assert "frontend: would clone" in out
    assert "summary: cloned=0 skipped=0 failed=0" in out


def test_clone_to_all_uses_host_workdir_fallback(monkeypatch, tmp_path):
    _env(monkeypatch)
    monkeypatch.delenv("H_MESH_WORKDIR", raising=False)
    monkeypatch.delenv("H_MESH_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("lib.paths.os.path.isdir", lambda path: path != "/workdir")
    r = FakeRedis(registry={"backend": "tmux"})

    with (
        patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")),
        patch("modules.office.cli._git_clone", return_value=(True, "")) as git_clone,
    ):
        office_main(["clone-to-all", "git@example.com:org/repo.git"])

    expected = tmp_path / "h-mesh" / "workdir" / "backend" / "repo"
    git_clone.assert_called_once_with(
        "git@example.com:org/repo.git", expected, "git@example.com:org/repo.git"
    )


@pytest.mark.parametrize("alias", ["cloneToAll", "sendFile", "letGo"])
def test_camel_case_command_aliases_are_rejected(alias, capsys):
    with pytest.raises(SystemExit) as exc:
        office_main([alias])

    assert exc.value.code == 2
    assert f"unknown command: {alias}" in capsys.readouterr().err


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
