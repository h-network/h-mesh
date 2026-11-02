"""`office reopen` and `office note`: durable ticket lifecycle (#22)."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.keys import prefix
from lib.board_interaction import NOTE_MAX_CHARS, NOTES_MAX, normalize_ticket, serialize_ticket
from modules.office.cli import main as office_main
from test_office_cli import POD, TENANT, FakeRedis, _env, _ticket

ME = "architect"


def key(state):
    return prefix(POD, TENANT, ME, f"tasks.{state}")


def run(r, argv, monkeypatch):
    _env(monkeypatch)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, ME)):
        office_main(argv)


def done_ticket(task_id="done0001", **extra):
    fields = {
        "status": "done", "done_ts": "2026-11-01T00:00:00.000Z",
        "started_ts": "2026-10-31T00:00:00.000Z", "outcome": "failed",
    }
    fields.update(extra)
    return _ticket(ME, task_id=task_id, **fields)


# --- reopen ----------------------------------------------------------------


def test_reopen_moves_done_to_doing_and_keeps_the_old_verdict_as_a_note(monkeypatch, capsys):
    r = FakeRedis()
    r.lists[key("done")].append(json.dumps(done_ticket()))
    run(r, ["reopen", "done0001"], monkeypatch)

    assert r.lists[key("done")] == []
    saved = json.loads(r.lists[key("doing")][0])
    assert saved["status"] == "doing"
    assert saved["done_ts"] is None
    assert "outcome" not in saved
    assert saved["started_ts"] not in (None, "2026-10-31T00:00:00.000Z")  # a fresh doing age
    assert [n["text"] for n in saved["notes"]] == ["reopened; was done (failed)"]
    assert saved["notes"][0]["by"] == ME
    assert json.loads(capsys.readouterr().out)["id"] == "done0001"


def test_reopen_todo_queues_it_without_starting(monkeypatch):
    r = FakeRedis()
    r.lists[key("done")].append(json.dumps(done_ticket()))
    r.lists[key("doing")].append(json.dumps(_ticket(ME, task_id="busy", status="doing")))
    run(r, ["reopen", "done0001", "--todo"], monkeypatch)

    saved = json.loads(r.lists[key("todo")][0])
    assert saved["status"] == "todo" and saved["started_ts"] is None
    assert len(r.lists[key("doing")]) == 1  # the one-doing invariant is untouched


def test_reopen_refuses_when_something_is_already_doing_and_changes_nothing(monkeypatch, capsys):
    r = FakeRedis()
    original = json.dumps(done_ticket())
    r.lists[key("done")].append(original)
    r.lists[key("doing")].append(json.dumps(_ticket(ME, task_id="busy", status="doing")))
    with pytest.raises(SystemExit) as exc:
        run(r, ["reopen", "done0001"], monkeypatch)
    assert exc.value.code == 1
    assert "reopen with --todo" in capsys.readouterr().err
    assert r.lists[key("done")] == [original]
    assert len(r.lists[key("doing")]) == 1


def test_reopen_a_cancelled_ticket_records_that_it_was_cancelled(monkeypatch):
    r = FakeRedis()
    r.lists[key("done")].append(json.dumps(done_ticket(status="cancelled", outcome=None)))
    run(r, ["reopen", "done0001"], monkeypatch)
    saved = json.loads(r.lists[key("doing")][0])
    assert saved["notes"][0]["text"] == "reopened; was cancelled"


def test_reopen_twice_accumulates_history_instead_of_replacing_it(monkeypatch):
    r = FakeRedis()
    r.lists[key("done")].append(json.dumps(done_ticket(outcome="failed")))
    run(r, ["reopen", "done0001"], monkeypatch)
    run(r, ["done", "--outcome", "passed"], monkeypatch)
    run(r, ["reopen", "done0001"], monkeypatch)
    texts = [n["text"] for n in json.loads(r.lists[key("doing")][0])["notes"]]
    assert texts == ["reopened; was done (failed)", "reopened; was done (passed)"]


def test_reopen_only_sees_finished_tickets_on_your_own_board(monkeypatch, capsys):
    r = FakeRedis()
    r.lists[key("todo")].append(json.dumps(_ticket(ME, task_id="todo0001")))
    with pytest.raises(SystemExit):
        run(r, ["reopen", "todo0001"], monkeypatch)
    assert "no finished task matches" in capsys.readouterr().err
    assert len(r.lists[key("todo")]) == 1


def test_reopen_ambiguous_prefix_is_refused(monkeypatch, capsys):
    r = FakeRedis()
    r.lists[key("done")].extend(
        [json.dumps(done_ticket("aaaa0001")), json.dumps(done_ticket("aaaa0002"))]
    )
    with pytest.raises(SystemExit):
        run(r, ["reopen", "aaaa"], monkeypatch)
    assert "ambiguous" in capsys.readouterr().err
    assert len(r.lists[key("done")]) == 2


# --- note ------------------------------------------------------------------


def test_note_appends_to_the_doing_ticket_in_place(monkeypatch, capsys):
    r = FakeRedis()
    r.lists[key("doing")].append(json.dumps(_ticket(ME, task_id="work0001", status="doing")))
    run(r, ["note", "tried A; found B"], monkeypatch)
    run(r, ["note", "second finding"], monkeypatch)

    saved = json.loads(r.lists[key("doing")][0])
    assert [n["text"] for n in saved["notes"]] == ["tried A; found B", "second finding"]
    assert all(n["by"] == ME and n["ts"].endswith("Z") for n in saved["notes"])
    assert len(r.lists[key("doing")]) == 1
    assert "noted work0001 (2 on ticket)" in capsys.readouterr().out


def test_note_preserves_every_unknown_field_and_the_queue_position(monkeypatch):
    r = FakeRedis()
    first = _ticket(ME, task_id="first", external_ref="keep", future={"a": [1, 2]})
    second = _ticket(ME, task_id="second")
    r.lists[key("todo")].extend([json.dumps(first), json.dumps(second)])
    run(r, ["note", "-i", "first", "context"], monkeypatch)

    saved = json.loads(r.lists[key("todo")][0])
    assert saved["external_ref"] == "keep" and saved["future"] == {"a": [1, 2]}
    assert saved["notes"][0]["text"] == "context"
    assert json.loads(r.lists[key("todo")][1])["id"] == "second"


def test_note_survives_take_done_and_return_round_trips(monkeypatch):
    r = FakeRedis()
    r.lists[key("todo")].append(json.dumps(_ticket(ME, task_id="trip0001")))
    run(r, ["note", "-i", "trip0001", "before taking"], monkeypatch)
    run(r, ["take"], monkeypatch)
    run(r, ["note", "while doing"], monkeypatch)
    run(r, ["return"], monkeypatch)
    run(r, ["take"], monkeypatch)
    run(r, ["done", "--outcome", "completed"], monkeypatch)
    notes = json.loads(r.lists[key("done")][0])["notes"]
    assert [n["text"] for n in notes] == ["before taking", "while doing"]


def test_note_needs_an_open_ticket_and_says_how_to_reach_a_finished_one(monkeypatch, capsys):
    r = FakeRedis()
    r.lists[key("done")].append(json.dumps(done_ticket()))
    with pytest.raises(SystemExit):
        run(r, ["note", "x"], monkeypatch)
    assert "you have no open task" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run(r, ["note", "-i", "done0001", "x"], monkeypatch)
    assert "office reopen" in capsys.readouterr().err


def test_note_from_stdin_keeps_multiline_text(monkeypatch):
    import io

    r = FakeRedis()
    r.lists[key("doing")].append(json.dumps(_ticket(ME, task_id="work0001", status="doing")))
    monkeypatch.setattr(sys, "stdin", io.StringIO("line one\nline two\n"))
    run(r, ["note", "--stdin"], monkeypatch)
    assert json.loads(r.lists[key("doing")][0])["notes"][0]["text"] == "line one\nline two"


@pytest.mark.parametrize("text", ["", "   \n"], ids=["empty", "blank"])
def test_empty_note_is_refused(monkeypatch, capsys, text):
    r = FakeRedis()
    r.lists[key("doing")].append(json.dumps(_ticket(ME, task_id="work0001", status="doing")))
    with pytest.raises(SystemExit):
        run(r, ["note", text], monkeypatch)
    # The shared text resolver (since the lead branch's change) rejects an empty
    # body itself, with its own wording; either refusal is the contract.
    err = capsys.readouterr().err
    assert "message text is empty" in err or "note cannot be empty" in err
    assert "notes" not in json.loads(r.lists[key("doing")][0])


def test_oversized_note_is_refused_not_truncated(monkeypatch, capsys):
    r = FakeRedis()
    stored = json.dumps(_ticket(ME, task_id="work0001", status="doing"))
    r.lists[key("doing")].append(stored)
    with pytest.raises(SystemExit):
        run(r, ["note", "x" * (NOTE_MAX_CHARS + 1)], monkeypatch)
    assert f"the limit is {NOTE_MAX_CHARS}" in capsys.readouterr().err
    assert r.lists[key("doing")] == [stored]


def test_note_cap_refuses_instead_of_dropping_history(monkeypatch, capsys):
    r = FakeRedis()
    notes = [{"ts": "t", "by": ME, "text": str(i)} for i in range(NOTES_MAX)]
    stored = json.dumps(_ticket(ME, task_id="work0001", status="doing", notes=notes))
    r.lists[key("doing")].append(stored)
    with pytest.raises(SystemExit):
        run(r, ["note", "one too many"], monkeypatch)
    assert "nothing was added or dropped" in capsys.readouterr().err
    assert r.lists[key("doing")] == [stored]


def test_note_refuses_a_ticket_with_duplicate_fields(monkeypatch, capsys):
    r = FakeRedis()
    stored = '{"v":1,"id":"dup0001","title":"a","title":"b","status":"doing"}'
    r.lists[key("doing")].append(stored)
    with pytest.raises(SystemExit):
        run(r, ["note", "x"], monkeypatch)
    assert "duplicate fields" in capsys.readouterr().err
    assert r.lists[key("doing")] == [stored]


# --- shared shape ----------------------------------------------------------


def test_normalize_keeps_only_well_formed_notes():
    ticket = _ticket(ME, notes=[
        {"ts": "t", "by": "a", "text": "ok", "extra": 1},
        {"ts": "t", "by": "a"},
        {"ts": 1, "by": "a", "text": "bad ts"},
        "not a dict",
    ])
    assert normalize_ticket(serialize_ticket(ticket))["notes"] == [{"ts": "t", "by": "a", "text": "ok"}]
    assert "notes" not in normalize_ticket(serialize_ticket(_ticket(ME, notes="nope")))


def test_list_shows_the_note_count(monkeypatch, capsys):
    r = FakeRedis()
    r.lists[key("doing")].append(json.dumps(_ticket(ME, task_id="work0001", status="doing")))
    run(r, ["note", "one"], monkeypatch)
    capsys.readouterr()
    run(r, ["list"], monkeypatch)
    assert "notes:1" in capsys.readouterr().out
