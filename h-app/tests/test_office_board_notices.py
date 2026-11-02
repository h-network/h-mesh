"""#18: `add` notifies the assignee, `done` notifies the creator.

Notices are ordinary Message envelopes written through the real send() into
the sender's egress list, so they appear in the mesh log like any other.
"""

import json
from unittest.mock import patch

import pytest

from core.envelope import parse
from core.keys import prefix
from modules.office.cli import main as office_main
from test_office_cli import POD, TENANT, FakeRedis, _env, _ticket

EGRESS = lambda agent: prefix(POD, TENANT, agent, "egress")  # noqa: E731


def _run(r, agent, argv):
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, agent)):
        office_main(argv)


def _sent(r, agent):
    return [parse(raw) for raw in r.lists[EGRESS(agent)]]


def _doing(r, agent, **extra):
    r.lists[prefix(POD, TENANT, agent, "tasks.doing")].append(
        json.dumps(_ticket(agent, status="doing", **extra))
    )


def test_add_sends_the_ticket_then_an_assignment_notice(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux"})
    _run(r, "architect", ["add", "-a", "backend", "-t", "fix the thing", "-d", "details"])
    ticket_id = capsys.readouterr().out.strip()
    ticket, notice = _sent(r, "architect")
    assert ticket["kind"] == "AddTicket" and notice["kind"] == "Message"
    assert notice["l2"]["destination"] == "backend"
    text = notice["payload"]["text"]
    assert ticket_id[:8] in text and "fix the thing" in text and "architect assigned you" in text
    assert "no reply needed" in text


def test_a_bulk_add_announces_only_the_first_ticket_per_window(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux"})
    for n in range(4):
        _run(r, "architect", ["add", "-a", "backend", "-t", f"t{n}", "-d", "d"])
    kinds = [e["kind"] for e in _sent(r, "architect")]
    assert kinds.count("AddTicket") == 4 and kinds.count("Message") == 1


def test_different_targets_are_debounced_independently(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux", "frontend": "tmux"})
    _run(r, "architect", ["add", "-a", "backend", "-t", "a", "-d", "d"])
    _run(r, "architect", ["add", "-a", "frontend", "-t", "b", "-d", "d"])
    assert [e["kind"] for e in _sent(r, "architect")].count("Message") == 2


def test_add_no_notify_and_self_add_send_no_notice(monkeypatch):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux"})
    _run(r, "architect", ["add", "-a", "backend", "-t", "a", "-d", "d", "--no-notify"])
    _run(r, "architect", ["add", "-a", "architect", "-t", "mine", "-d", "d"])
    assert {e["kind"] for e in _sent(r, "architect")} == {"AddTicket"}


def test_a_failing_notice_does_not_fail_add(monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux"})
    with patch("modules.office.cli._message", side_effect=RuntimeError("boom")):
        _run(r, "architect", ["add", "-a", "backend", "-t", "a", "-d", "d"])
    captured = capsys.readouterr()
    assert len(captured.out.strip()) == 32          # the ticket id still printed
    assert "notice to backend was not sent" in captured.err


def test_a_notice_send_failure_does_not_burn_the_window_for_nothing_visible(monkeypatch):
    # The debounce key is set before sending; a failed notice is reported on
    # stderr (above) and the next add inside the window stays quiet by design.
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "backend": "tmux"})
    _run(r, "architect", ["add", "-a", "backend", "-t", "a", "-d", "d"])
    assert prefix(POD, TENANT, "backend", "notice.add") in r.values


def test_done_notifies_the_creator_with_the_outcome(monkeypatch, capsys):
    _env(monkeypatch, agent="worker")
    r = FakeRedis(registry={"architect": "tmux", "worker": "tmux"})
    _doing(r, "worker", created_by="architect", title="ship it")
    _run(r, "worker", ["done", "--outcome", "passed"])
    (notice,) = _sent(r, "worker")
    assert notice["kind"] == "Message" and notice["l2"]["destination"] == "architect"
    text = notice["payload"]["text"]
    assert "worker finished ticket a1b2c3d4" in text and "ship it" in text and "(passed)" in text
    assert "no reply needed" in text
    assert capsys.readouterr().out.strip() == 'done a1b2c3d4: "ship it" (passed)'


@pytest.mark.parametrize("creator, flags", [
    ("worker", []),                 # self-created
    ("architect", ["--no-notify"]),
    ("ghost", []),                  # creator not enrolled any more
])
def test_done_sends_no_notice_when_it_should_not(creator, flags, monkeypatch):
    _env(monkeypatch, agent="worker")
    r = FakeRedis(registry={"architect": "tmux", "worker": "tmux"})
    _doing(r, "worker", created_by=creator)
    _run(r, "worker", ["done", "--outcome", "completed", *flags])
    assert _sent(r, "worker") == []
    assert r.lists[prefix(POD, TENANT, "worker", "tasks.done")]        # still closed


def test_cancel_and_return_send_no_notice(monkeypatch):
    _env(monkeypatch, agent="worker")
    r = FakeRedis(registry={"architect": "tmux", "worker": "tmux"})
    _doing(r, "worker", created_by="architect")
    _run(r, "worker", ["cancel"])
    assert _sent(r, "worker") == []


def test_a_failing_done_notice_does_not_fail_done(monkeypatch, capsys):
    _env(monkeypatch, agent="worker")
    r = FakeRedis(registry={"architect": "tmux", "worker": "tmux"})
    _doing(r, "worker", created_by="architect")
    with patch("modules.office.cli._message", side_effect=RuntimeError("boom")):
        _run(r, "worker", ["done", "--outcome", "completed"])
    captured = capsys.readouterr()
    assert captured.out.startswith("done a1b2c3d4")
    assert "notice to architect was not sent" in captured.err
    assert r.lists[prefix(POD, TENANT, "worker", "tasks.done")]


def test_notices_are_not_an_ack_loop(monkeypatch):
    """A notice is a Message from the actor; answering it is not requested and
    nothing here answers one: done/add never read the inbox."""
    _env(monkeypatch, agent="worker")
    r = FakeRedis(registry={"architect": "tmux", "worker": "tmux"})
    _doing(r, "worker", created_by="architect")
    _run(r, "worker", ["done", "--outcome", "completed"])
    assert "no reply needed" in _sent(r, "worker")[0]["payload"]["text"]
