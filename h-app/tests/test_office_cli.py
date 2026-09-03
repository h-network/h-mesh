import io
import json
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.channels import receive
from core.envelope import build, encode, parse
from core.keys import prefix, receive_undeliverable_key, receive_unresolved_key, retired_inbox_key
from lib.board_interaction import add_ticket
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
    # ⚠ Bare hire (no --wait) now runs a real, short attributable-completion
    # check before returning (operator's call, restoring --wait after its
    # removal) -- a real time.sleep in that poll loop, unaffected by
    # FakeRedis being fake. 0.0 still resolves correctly (the dead-letter
    # scan runs before the deadline check, so a pre-seeded rejection is
    # still caught -- see test_hire_wait_accepts_zero_as_an_immediate_
    # single_check's identical reasoning for --wait=0), it just does it
    # without spending a real second per call across this whole file.
    # Tests of the real 1s ceiling itself override this back explicitly.
    monkeypatch.setattr(office_cli, "_BARE_HIRE_CHECK_TIMEOUT_S", 0.0)


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


def test_send_stdin_identity_reaches_recipient_on_real_redis(monkeypatch, capsys):
    """Pin h-mesh's working boundary by what the recipient opens.

    This is a negative regression for a live legacy-CLI defect, not evidence
    that every implementation named `office` behaves the same way. The sender's
    success line and byte count are deliberately insufficient assertions.
    """
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        r.ping()
    except redis.RedisError:
        pytest.skip("real Redis server not available at REDIS_URL")

    tenant = f"stdin-recipient-{os.urandom(8).hex()}"
    registry = prefix(POD, tenant, resource="registry")
    egress = prefix(POD, tenant, "sender", "egress")
    ingress = prefix(POD, tenant, "recipient", "ingress")
    body = "recipient must open this exact stdin body"
    r.hset(registry, mapping={"sender": "tmux", "recipient": "tmux"})
    try:
        with (
            patch("modules.office.cli._context", return_value=(r, POD, tenant, "sender")),
            patch("modules.office.cli.sys.stdin", io.StringIO(body)),
        ):
            office_main(["send", "-a", "recipient", "--stdin"])

        reported_id = capsys.readouterr().out.rsplit("(", 1)[1].rstrip(")\n")
        raw = r.lpop(egress)
        assert parse(raw)["stream_id"] == reported_id
        r.rpush(ingress, raw)
        opened = []
        receive(
            r,
            pod=POD,
            tenant=tenant,
            agent="recipient",
            openers={"Message": opened.append},
            timeout=0,
            blocking=False,
        )

        assert len(opened) == 1
        assert opened[0]["stream_id"] == reported_id
        assert opened[0]["payload"]["text"] == body
    finally:
        keys = r.keys(prefix(POD, tenant) + ":*")
        if keys:
            r.delete(*keys)


def test_unresolved_names_exact_identity_without_consuming(monkeypatch, capsys):
    r = FakeRedis()
    _env(monkeypatch)
    envelope = build("Command", "alice", "worker-1", {"text": "danger"}, pod=POD, tenant=TENANT)
    record = json.dumps({
        "agent": "worker-1", "reason": "ack outcome unknown", "envelope": encode(envelope),
    })
    key = receive_unresolved_key(POD, TENANT)
    r.rpush(key, record)
    with patch("modules.office.cli.redis.Redis.from_url", return_value=r):
        office_main(["unresolved", "--agent", "worker-1"])
    shown = json.loads(capsys.readouterr().out)
    assert shown == {
        "agent": "worker-1", "stream_id": envelope["stream_id"],
        "kind": "Command", "source": "alice", "reason": "ack outcome unknown",
    }
    assert r.lrange(key, 0, -1) == [record]


def test_undeliverable_names_terminal_identity_without_consuming(monkeypatch, capsys):
    r = FakeRedis()
    _env(monkeypatch)
    envelope = build(
        "Command", "alice", "worker-1", {"text": "never opened"},
        pod=POD, tenant=TENANT,
    )
    record = json.dumps({
        "agent": "worker-1",
        "reason": "destination retired before opening",
        "encoding": "hex",
        "envelope": encode(envelope).encode().hex(),
    })
    key = receive_undeliverable_key(POD, TENANT)
    r.rpush(key, record)
    with patch("modules.office.cli.redis.Redis.from_url", return_value=r):
        office_main(["undeliverable", "--agent", "worker-1"])
    shown = json.loads(capsys.readouterr().out)
    assert shown == {
        "agent": "worker-1", "stream_id": envelope["stream_id"],
        "kind": "Command", "source": "alice",
        "reason": "destination retired before opening",
    }
    assert r.lrange(key, 0, -1) == [record]


def test_undeliverable_malformed_record_is_reported_without_consuming(monkeypatch, capsys):
    r = FakeRedis()
    _env(monkeypatch)
    record = json.dumps({
        "agent": "worker-1", "encoding": "hex", "envelope": [],
    })
    key = receive_undeliverable_key(POD, TENANT)
    r.rpush(key, record)

    with patch("modules.office.cli.redis.Redis.from_url", return_value=r):
        office_main(["undeliverable"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "unparseable undeliverable custody record\n"
    assert r.lrange(key, 0, -1) == [record]


def test_retired_inbox_shows_hex_decoded_fields_without_consuming(monkeypatch, capsys):
    r = FakeRedis()
    _env(monkeypatch)
    record = json.dumps({
        "agent": "worker-1",
        "reason": "destination retired with unread inbox content",
        "entry_id": "1700000000000-0",
        "encoding": "hex",
        "fields": [["envelope".encode().hex(), '{"text":"hi"}'.encode().hex()]],
    })
    key = retired_inbox_key(POD, TENANT)
    r.rpush(key, record)
    with patch("modules.office.cli.redis.Redis.from_url", return_value=r):
        office_main(["retired-inbox", "--agent", "worker-1"])
    shown = json.loads(capsys.readouterr().out)
    assert shown == {
        "agent": "worker-1",
        "entry_id": "1700000000000-0",
        "reason": "destination retired with unread inbox content",
        "fields": [{
            "field": {"value": "envelope", "encoding": "utf8"},
            "value": {"value": '{"text":"hi"}', "encoding": "utf8"},
        }],
    }
    assert r.lrange(key, 0, -1) == [record]


def test_retired_inbox_shows_non_utf8_fields_as_hex_instead_of_raising(monkeypatch, capsys):
    r = FakeRedis()
    _env(monkeypatch)
    hostile = b"\xff\x00not-valid-utf8"
    record = json.dumps({
        "agent": "worker-1",
        "reason": "destination retired with unread inbox content",
        "entry_id": "1700000000000-0",
        "encoding": "hex",
        "fields": [["envelope".encode().hex(), hostile.hex()]],
    })
    key = retired_inbox_key(POD, TENANT)
    r.rpush(key, record)
    with patch("modules.office.cli.redis.Redis.from_url", return_value=r):
        office_main(["retired-inbox"])
    shown = json.loads(capsys.readouterr().out)
    # Field and value decode INDEPENDENTLY -- "envelope" stays readable
    # even though its paired value does not, unlike an earlier version
    # that fell the whole pair back to hex together.
    assert shown["fields"] == [{
        "field": {"value": "envelope", "encoding": "utf8"},
        "value": {"value": hostile.hex(), "encoding": "hex"},
    }]


def test_retired_inbox_shows_duplicate_field_names_as_distinct_ordered_entries(monkeypatch, capsys):
    # reviewer's exact ask: the same genuine-duplicate-field fixture
    # test_agentlifecycle.py proves at the Lua/storage layer, now proven
    # through the CLI reader too -- a dict would silently keep only the
    # last "dup" value and lose the ordering; this must not.
    r = FakeRedis()
    _env(monkeypatch)
    record = json.dumps({
        "agent": "worker-1",
        "reason": "destination retired with unread inbox content",
        "entry_id": "1700000000000-0",
        "encoding": "hex",
        "fields": [
            ["dup".encode().hex(), "first".encode().hex()],
            ["other".encode().hex(), "x".encode().hex()],
            ["dup".encode().hex(), "second".encode().hex()],
        ],
    })
    key = retired_inbox_key(POD, TENANT)
    r.rpush(key, record)
    with patch("modules.office.cli.redis.Redis.from_url", return_value=r):
        office_main(["retired-inbox"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["fields"] == [
        {"field": {"value": "dup", "encoding": "utf8"}, "value": {"value": "first", "encoding": "utf8"}},
        {"field": {"value": "other", "encoding": "utf8"}, "value": {"value": "x", "encoding": "utf8"}},
        {"field": {"value": "dup", "encoding": "utf8"}, "value": {"value": "second", "encoding": "utf8"}},
    ]
    assert r.lrange(key, 0, -1) == [record]


def test_retired_inbox_malformed_record_is_reported_without_consuming(monkeypatch, capsys):
    r = FakeRedis()
    _env(monkeypatch)
    record = json.dumps({"agent": "worker-1", "encoding": "hex", "fields": "not-a-list"})
    key = retired_inbox_key(POD, TENANT)
    r.rpush(key, record)

    with patch("modules.office.cli.redis.Redis.from_url", return_value=r):
        office_main(["retired-inbox"])

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "unparseable retired-inbox custody record\n"
    assert r.lrange(key, 0, -1) == [record]


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
    assert kwargs["in_reply_to"] is None
    assert "sent to backend: 11 bytes (stream-1)" in capsys.readouterr().out


@patch("modules.office.cli.send")
def test_send_reply_to_threads_through_to_the_wire(mock_send, monkeypatch, capsys):
    _env(monkeypatch)
    mock_send.return_value = "stream-2"
    r = FakeRedis(registry={"backend": "tmux"})
    target = "a" * 32
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["send", "-a", "backend", "--reply-to", target, "hello there"])
    kwargs = mock_send.call_args[1]
    assert kwargs["in_reply_to"] == target


@patch("modules.office.cli.send")
def test_send_reply_to_rejects_malformed_id_before_sending(mock_send, monkeypatch, capsys):
    _env(monkeypatch)
    r = FakeRedis(registry={"backend": "tmux"})
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit):
            office_main(["send", "-a", "backend", "--reply-to", "not-an-id", "hello there"])
    assert "not a 32-character lowercase hex stream_id" in capsys.readouterr().err
    mock_send.assert_not_called()


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


# ---------------------------------------------------------------------------
# hire --wait: distinguish confirmed (CREATED)/failed/unknown, not just that
# the request was accepted. Real incident: setup.sh's own roster-hire loop
# printed "hired" off nothing but this command's exit code, which only ever
# proved the StartAgent envelope was durably enqueued (ADMITTED) -- never
# that the agent actually registered (CREATED). Same shape as a 202 telling
# an operator an agent existed when it did not, in the highest-traffic path.
# ---------------------------------------------------------------------------


def _dead_letter_envelope() -> tuple[bytes, str]:
    """A realistic raw dead-letter entry, built the same way core.channels
    itself would encode one -- not a hand-rolled string that happens to
    parse. Source and destination match a real hire's own shape: sent by
    "host" (setup.sh's AGENT_NAME), addressed to "host" (every StartAgent's
    real destination)."""
    from core.envelope import build, encode

    envelope = build(
        "StartAgent", "host", "host", {"agent": "worker-1", "cli": "claude"}, pod=POD, tenant=TENANT,
    )
    return encode(envelope).encode(), envelope["stream_id"]


@patch("modules.office.cli.port_type")
@patch("modules.office.cli.send")
def test_hire_wait_is_unknown_not_confirmed_even_for_a_clean_new_registration(
    mock_send, mock_port_type, monkeypatch, capsys,
):
    # Reviewer FAILED a version of this function that treated "absent, then
    # tmux" as unambiguous for a never-before-registered agent, reasoning
    # nothing else could cause that transition. Wrong under concurrency: a
    # DIFFERENT, unrelated StartAgent for the same agent name -- already
    # queued, or racing in around the same time -- can register the agent
    # while THIS request is independently rejected, with its dead-letter
    # simply not landed yet by the time of an early poll. Both worlds look
    # identical: no dead-letter match (yet), port_type()=="tmux". So even
    # a clean-looking transition -- registry absent, then present, no
    # dead-letter ever -- must resolve to "unknown", never "confirmed".
    # port_type is mocked and asserted un-called below to prove the
    # function doesn't even look at it anymore, not to simulate a
    # transition it would react to.
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude", "--wait", "0.3"])
    out = capsys.readouterr()
    assert "confirmed" not in out.out
    assert exc_info.value.code == 2
    mock_port_type.assert_not_called()


@patch("modules.office.cli.send")
def test_hire_wait_fails_on_a_matching_dead_letter_not_on_a_bare_timeout(mock_send, monkeypatch, capsys):
    raw, real_stream_id = _dead_letter_envelope()
    mock_send.return_value = real_stream_id  # what send() would really return for this envelope
    r = FakeRedis()
    dead_key = prefix(POD, TENANT, agent="host", resource="dead")
    r.lists[dead_key].append(raw)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude", "--wait", "1"])
    assert exc_info.value.code == 1, "a real rejection must be a distinct exit code from a timeout"
    err = capsys.readouterr().err
    assert "failed: worker-1 was not registered" in err
    # Read-only: the evidence must still be there for a human or another
    # tool to see afterward, not consumed by this check.
    assert r.lists[dead_key] == [raw]


# ── Reviewer FAILED an earlier version of --wait on exactly this. Real
# harm, quoting the operator's own words that started this whole chain:
# "It said it created but didnt." A registry row that already existed
# before this request was ever sent is evidence about the WORLD, not
# evidence about THIS request -- lib.agentlifecycle.start_agent itself
# deliberately does not publish any new marker for an already-registered
# agent's hire ("idempotent starts do not replace this marker: their
# envelope did not cause a new window"), so bare membership cannot tell a
# successful re-hire apart from a rejected one. These three tests assert
# the harm directly -- the caller must never be told "confirmed" here,
# whatever the internal mechanism -- not a specific code path.

@patch("modules.office.cli.send")
def test_hire_wait_never_confirms_an_already_registered_agent_that_was_actually_rejected(
    mock_send, monkeypatch, capsys,
):
    raw, real_stream_id = _dead_letter_envelope()
    mock_send.return_value = real_stream_id
    r = FakeRedis()
    _member(r, "worker-1", "tmux")  # pre-existing, unrelated to this request
    dead_key = prefix(POD, TENANT, agent="host", resource="dead")
    r.lists[dead_key].append(raw)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude", "--wait", "1"])
    out = capsys.readouterr()
    assert "confirmed" not in out.out, (
        "the caller was told confirmed for a request that was actually rejected"
    )
    assert exc_info.value.code == 1


@patch("modules.office.cli.send")
def test_hire_wait_never_confirms_an_already_registered_agent_with_no_evidence_either_way(
    mock_send, monkeypatch, capsys,
):
    # The more basic case, needing no dead-letter at all: a bare
    # pre-existing registry row, on its own, is not proof this specific
    # request succeeded. Reviewer's exact repro (_await_hire_confirmation
    # returning ('confirmed', None) for this state) -- confirmed as
    # 'unknown' now, never 'confirmed', for the identical seeded state.
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    _member(r, "worker-1", "tmux")
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude", "--wait", "0.2"])
    out = capsys.readouterr()
    assert "confirmed" not in out.out
    assert exc_info.value.code == 2


@patch("modules.office.cli.send")
def test_hire_wait_still_catches_a_delayed_rejection_for_an_already_registered_agent(
    mock_send, monkeypatch, capsys,
):
    # A rejection that lands mid-poll, not before the first check -- proves
    # this isn't just a first-iteration ordering trick; the poll loop must
    # keep checking dead-letter evidence on every pass for the whole
    # already-registered lifetime of the wait, not only once at the start.
    raw, real_stream_id = _dead_letter_envelope()
    mock_send.return_value = real_stream_id
    r = FakeRedis()
    _member(r, "worker-1", "tmux")
    dead_key = prefix(POD, TENANT, agent="host", resource="dead")

    def _append_dead_letter_late():
        time.sleep(0.3)
        r.lists[dead_key].append(raw)

    thread = threading.Thread(target=_append_dead_letter_late)
    thread.start()
    try:
        with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
            with pytest.raises(SystemExit) as exc_info:
                office_main(["hire", "worker-1", "--cli", "claude", "--wait", "2"])
    finally:
        thread.join()
    out = capsys.readouterr()
    assert "confirmed" not in out.out
    assert exc_info.value.code == 1


@patch("modules.office.cli.send")
def test_hire_wait_times_out_as_unknown_not_failed_when_neither_happens(mock_send, monkeypatch, capsys):
    mock_send.return_value = "stream-1"
    r = FakeRedis()  # no registry entry, no dead letter -- genuinely unresolved
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude", "--wait", "0.2"])
    assert exc_info.value.code == 2, "a timeout must be its own exit code, distinct from a real failure (1)"
    err = capsys.readouterr().err
    assert "unknown: no proof of failure" in err
    assert "does NOT mean it failed" in err


# ⚠ Reviewer FAILED this branch on exactly this: `type=float` accepted nan/
# inf/-inf/negative unvalidated. With --wait nan, deadline and remaining
# both become NaN; every comparison against NaN is False, so
# `remaining <= 0` never becomes true and `min(POLL_INTERVAL, nan)` returns
# POLL_INTERVAL unchanged -- the poll loop never terminates and the exit-2
# "unknown" outcome the whole three-state design exists to guarantee never
# fires. Confirmed empirically before fixing (min(0.5, float('nan')) ==
# 0.5, float('nan') <= 0 is False) -- reviewer's claim matched exactly, not
# just plausible. +inf is real but unbounded, contradicting "wait up to
# SECONDS"; negative gives an already-past deadline with a nonsensical
# negative duration in the message. All four are now rejected by argparse's
# own type= callback, which runs during parse_args() -- strictly before the
# send() call below it in this file -- so no hire is ever admitted from an
# invalid --wait.
@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "-5"])
@patch("modules.office.cli.send")
def test_hire_wait_rejects_non_finite_and_negative_values_before_any_send(mock_send, monkeypatch, bad_value):
    _env(monkeypatch)
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            # `=` form, not a separate argv token -- "-inf"/"-5" as a bare
            # next token can be swallowed by argparse's own "looks like an
            # option" heuristic for an optional nargs='?' value before ever
            # reaching this option's type= callback; --wait=<value> removes
            # that ambiguity so every one of these four is actually
            # exercising the same validation path, not two different ones.
            office_main(["hire", "worker-1", "--cli", "claude", f"--wait={bad_value}"])
    assert exc_info.value.code == 2
    mock_send.assert_not_called()


@patch("modules.office.cli.send")
def test_hire_wait_accepts_zero_as_an_immediate_single_check(mock_send, monkeypatch, capsys):
    # 0 is finite and non-negative -- a legitimate "check once now, don't
    # actually wait" mode, not something the nan/inf/negative fix should
    # also reject. send() still gets called (0 isn't rejected the way
    # nan/inf/negative are), and with nothing seeded to prove a rejection,
    # the single check correctly resolves unknown -- not confirmed, since
    # that outcome no longer exists at all.
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude", "--wait", "0"])
    mock_send.assert_called_once()
    out = capsys.readouterr()
    assert "confirmed" not in out.out
    assert exc_info.value.code == 2


@patch("modules.office.cli.send")
def test_hire_without_wait_stays_fire_and_forget(mock_send, monkeypatch, capsys):
    # No --wait flag still runs the short, silent bare-hire check
    # internally (see _BARE_HIRE_CHECK_TIMEOUT_S) -- but it must stay
    # observably fire-and-forget when unresolved: only the stream_id on
    # stdout, nothing on stderr, no nonzero exit. _env() patches the
    # timeout to 0 so this doesn't spend a real second finding that out.
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis()
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hire", "worker-1", "--cli", "claude"])
    assert capsys.readouterr().out.strip() == "stream-1"


@patch("modules.office.cli.send")
def test_hire_bare_check_read_failure_still_prints_the_admitted_stream_id(mock_send, monkeypatch, capsys):
    # Reviewer's exact harm test, this branch's own principle inverted: the
    # check must not imply success it cannot prove, but it must equally
    # not DENY an admission that was already proven. send() has already
    # durably enqueued the envelope and returned a real stream_id before
    # _await_hire_confirmation is ever called -- a Redis error reading the
    # dead-letter list is a failure to CHECK, not evidence the hire
    # failed, and it must not destroy the one thing already known. Before
    # this branch a bare hire touched Redis only once (inside send());
    # this proves the NEW post-admission check degrades to best-effort
    # instead of raising and losing that result (empty stdout AND empty
    # stderr, the exact shape reviewer reproduced).
    _env(monkeypatch)
    mock_send.return_value = "1712345678901-0"
    r = FakeRedis()

    def _raise_connection_error(*args, **kwargs):
        raise redis.exceptions.ConnectionError("simulated: dead-letter read failed")

    monkeypatch.setattr(r, "lrange", _raise_connection_error)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hire", "worker-1", "--cli", "claude"])
    out = capsys.readouterr()
    assert out.out.strip() == "1712345678901-0", (
        f"a read failure during the optional check destroyed the admitted "
        f"stream_id -- stdout:{out.out!r} stderr:{out.err!r}"
    )
    assert out.err == ""


@patch("modules.office.cli.send")
def test_hire_wait_explicit_read_failure_is_reported_as_unknown_not_a_real_timeout(mock_send, monkeypatch, capsys):
    # The deliberate decision for the OTHER path: asking for --wait means
    # wanting to know either way, so a read failure there is reported
    # explicitly (not silently, unlike bare hire) -- but it still resolves
    # to the same "unknown" outcome as a timeout, and the message says
    # the check itself could not run rather than falsely claiming a real
    # SECONDS-long wait happened. Both paths return the identical outcome
    # classification ("unknown") so they cannot diverge in what they
    # believe happened -- only the message differs, and only because
    # bare hire never distinguishes any flavor of "unknown" in its
    # silence to begin with.
    mock_send.return_value = "1712345678901-0"
    r = FakeRedis()

    def _raise_connection_error(*args, **kwargs):
        raise redis.exceptions.ConnectionError("simulated: dead-letter read failed")

    monkeypatch.setattr(r, "lrange", _raise_connection_error)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude", "--wait", "5"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "could not check" in err
    assert "no proof of failure for worker-1 within 5s" not in err, (
        "must not claim a real 5s wait happened when the check never ran"
    )


@patch("modules.office.cli.send")
def test_hire_without_wait_still_speaks_on_a_proven_rejection(mock_send, monkeypatch, capsys):
    # The other half of the same contract: bare hire must not go so quiet
    # that it hides a rejection it can actually prove within its short
    # internal check -- silence is for "unknown", not for "failed". Same
    # dead-letter-seeded setup as the explicit --wait failure tests above,
    # just with no --wait flag at all.
    _env(monkeypatch)
    raw, real_stream_id = _dead_letter_envelope()
    mock_send.return_value = real_stream_id
    r = FakeRedis()
    dead_key = prefix(POD, TENANT, agent="host", resource="dead")
    r.lists[dead_key].append(raw)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude"])
    assert exc_info.value.code == 1
    assert "failed: worker-1 was not registered" in capsys.readouterr().err


@patch("modules.office.cli.send")
def test_hire_bare_check_polls_again_and_catches_a_rejection_landing_after_the_first_scan(
    mock_send, monkeypatch, capsys,
):
    # Reviewer's exact counterexample: the pre-seeded-rejection test above
    # only proves the FIRST scan works -- a one-scan-then-sleep-to-deadline
    # implementation would pass it identically and still keep every other
    # committed test (including the 1.19s/0.09s timing experiment) green.
    # This proves the window genuinely loops: lrange returns EMPTY on its
    # first call, then the matching dead-letter on a later call, with a
    # real nonzero deadline (not _env()'s 0.0) -- so this can only pass if
    # _await_hire_confirmation actually re-scans, not just sleeps once.
    # time.sleep is mocked to a no-op so the real poll-interval sleeps
    # between iterations cost no wall-clock time -- deterministic and
    # fast, not a timing race against a real background thread.
    monkeypatch.setattr(office_cli, "_BARE_HIRE_CHECK_TIMEOUT_S", 0.3)
    monkeypatch.setattr(office_cli.time, "sleep", lambda seconds: None)
    raw, real_stream_id = _dead_letter_envelope()
    mock_send.return_value = real_stream_id
    r = FakeRedis()
    dead_key = prefix(POD, TENANT, agent="host", resource="dead")
    real_lrange = r.lrange
    calls = []

    def _lrange_empty_then_matching(key, start, end):
        calls.append(1)
        if len(calls) == 1:
            return []
        return real_lrange(key, start, end)

    r.lists[dead_key].append(raw)
    monkeypatch.setattr(r, "lrange", _lrange_empty_then_matching)
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        # The wrapper above returns [] on the very first lrange call
        # regardless of the real underlying list contents (already
        # populated), simulating a rejection that lands after the first
        # scan rather than being visible from the start.
        with pytest.raises(SystemExit) as exc_info:
            office_main(["hire", "worker-1", "--cli", "claude"])
    assert len(calls) >= 2, (
        f"only {len(calls)} scan(s) happened -- the window never polled a "
        "second time, so this cannot distinguish a real loop from a "
        "single scan followed by sleeping to the deadline"
    )
    assert exc_info.value.code == 1
    assert "failed: worker-1 was not registered" in capsys.readouterr().err


def test_hire_wait_bare_flag_defaults_to_five_seconds_not_thirty(monkeypatch, capsys):
    # Pin the operator's actual number, not just its shape -- --wait with
    # no value used to default to 30s (and was in --help as such, which is
    # part of why agents used it and burned that much time per hire).
    # Restored --wait defaults to 5s; a bare hire (no --wait at all) still
    # only ever waits 1s (_BARE_HIRE_CHECK_TIMEOUT_S), tested separately.
    with pytest.raises(SystemExit):
        office_main(["hire", "--help"])
    out = capsys.readouterr().out
    assert "default 5" in out
    assert "default 30" not in out


def test_bare_hire_check_timeout_is_exactly_one_second():
    # The real module constant, not a docstring's claim about it -- _env()
    # patches this to 0 everywhere else in this file for speed, so nothing
    # else in this file would catch it drifting from what the operator
    # actually asked for.
    assert office_cli._BARE_HIRE_CHECK_TIMEOUT_S == 1.0


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


def test_hold_parks_queued_ticket_without_displacing_active_work(monkeypatch, capsys):
    """Parking queued work changes the board signal without inventing activity.

    The active ticket must stay exactly where it is, while the selected queued
    ticket leaves the unpicked queue and remains visible on hold with its reason.
    """
    _env(monkeypatch)
    r = FakeRedis()
    keys = office_cli._task_keys(POD, TENANT, "architect")
    active = json.dumps(
        _ticket("architect", task_id="active-task", status="doing", title="active work")
    )
    queued = json.dumps(
        _ticket("architect", task_id="queued-task", status="todo", title="queued work")
    )
    r.lists[keys["doing"]].append(active)
    r.lists[keys["todo"]].append(queued)

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["hold", "queued", "--reason", "waiting for dependency"])
        capsys.readouterr()
        office_main(["list"])

    assert r.lists[keys["doing"]] == [active]
    assert r.lists[keys["todo"]] == []
    parked = json.loads(r.lists[keys["hold"]][0])
    assert parked["id"] == "queued-task"
    assert parked["status"] == "hold"
    assert parked["hold_reason"] == "waiting for dependency"
    listed = capsys.readouterr().out
    assert "queued-t  queued work" in listed
    assert "reason:waiting for dependency" in listed


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
# delete / add
# ---------------------------------------------------------------------------


def test_delete_miss_names_own_board_scope_and_preserves_assignees_ticket(monkeypatch, capsys):
    """A raiser cannot silently mistake delegated creation for delete authority."""
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "reviewer": "tmux"})
    reviewer_todo = prefix(POD, TENANT, "reviewer", "tasks.todo")
    raw = json.dumps(_ticket("architect", task_id="delegated-ticket", status="todo"))
    r.lists[reviewer_todo].append(raw)

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        with pytest.raises(SystemExit) as exc:
            office_main(["delete", "delegated-ticket"])

    assert exc.value.code == 1
    assert r.lists[reviewer_todo] == [raw]
    error = capsys.readouterr().err
    assert "delete searches only your own board" in error
    assert "cannot withdraw a task assigned to another agent" in error


@patch("modules.office.cli.send")
def test_add_sends_envelope_and_prints_the_ticket_id(mock_send, monkeypatch, capsys):
    _env(monkeypatch)
    mock_send.return_value = "stream-1"
    r = FakeRedis(registry={"backend": "tmux"})
    allocated_bytes = bytes.fromhex("0123456789abcdef" * 2)
    with (
        patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")),
        patch("modules.office.cli.os.urandom", return_value=allocated_bytes) as urandom,
    ):
        office_main(["add", "-a", "backend", "-t", "title", "-d", "desc"])
    kwargs = mock_send.call_args[1]
    assert kwargs["kind"] == "AddTicket"
    assert kwargs["payload"]["title"] == "title"
    ticket_id = kwargs["payload"]["id"]
    urandom.assert_called_once_with(16)
    assert ticket_id == "0123456789abcdef" * 2
    assert len(ticket_id) == 32
    assert all(character in "0123456789abcdef" for character in ticket_id)
    assert capsys.readouterr().out.strip() == ticket_id
    assert ticket_id != "stream-1"
    todo_key = prefix(POD, TENANT, "backend", "tasks.todo")
    assert r.lists[todo_key] == []


def test_add_printed_id_is_created_and_takeable_on_assignees_board(monkeypatch, capsys):
    """The address handed to the raiser must be the assignee's task address."""
    _env(monkeypatch)
    r = FakeRedis(registry={"architect": "tmux", "reviewer": "tmux"})
    allocated_bytes = bytes.fromhex("fedcba9876543210" * 2)
    with (
        patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")),
        patch("modules.office.cli.os.urandom", return_value=allocated_bytes),
    ):
        office_main(["add", "-a", "reviewer", "-t", "review me", "-d", "full context"])

    printed_id = capsys.readouterr().out.strip()
    envelope = parse(r.lists[prefix(POD, TENANT, "architect", "egress")].pop(0))
    add_ticket(r, pod=POD, tenant=TENANT, agent="reviewer", envelope=envelope)
    stored = json.loads(r.lists[prefix(POD, TENANT, "reviewer", "tasks.todo")][0])

    assert printed_id == "fedcba9876543210" * 2
    assert envelope["payload"]["id"] == printed_id
    assert stored["id"] == printed_id
    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "reviewer")):
        office_main(["take", printed_id])
    taken = json.loads(r.lists[prefix(POD, TENANT, "reviewer", "tasks.doing")][0])
    assert taken["id"] == printed_id


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

    expected = tmp_path / "h-mesh" / "backend" / "repo"
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


def test_status_does_not_report_active_empty_board_as_blocked_by_delivery_marker(
    monkeypatch, capsys
):
    """Delivery uncertainty must not silently remove an available agent."""
    _env(monkeypatch)
    r = FakeRedis(registry={"ci-agent": "tmux"})
    now = datetime.now(timezone.utc)
    r.hashes[prefix(POD, TENANT, "ci-agent", "presence")] = {
        "state": "idle",
        "since": (now - timedelta(minutes=1)).isoformat(),
        "last_activity": (now - timedelta(seconds=8)).isoformat(),
    }
    r.hashes[prefix(POD, TENANT, "ci-agent", "blocked")] = {
        "since": (now - timedelta(minutes=2)).isoformat(),
        "stream_id": "a" * 32,
    }

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["status", "ci-agent"])

    row = capsys.readouterr().out
    assert "ci-agent    idle" in row
    assert "ci-agent    blocked" not in row
    assert "—" in row
    assert "last activity 8s ago" in row
    assert "delivery unverified for 2m" in row


def test_status_keeps_unknown_presence_unknown_with_delivery_marker(monkeypatch, capsys):
    """Missing presence is not silently promoted to availability or blockage."""
    _env(monkeypatch)
    r = FakeRedis(registry={"ci-agent": "tmux"})
    r.hashes[prefix(POD, TENANT, "ci-agent", "blocked")] = {
        "since": "2026-09-02T09:59:00.000Z",
        "stream_id": "a" * 32,
    }

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["status", "ci-agent"])

    row = capsys.readouterr().out
    assert "ci-agent    unknown" in row
    assert "ci-agent    idle" not in row
    assert "no activity feed; delivery unverified" in row


def test_status_keeps_delivery_marker_visible_when_its_age_is_malformed(monkeypatch, capsys):
    """Bad marker age degrades to explicit unknown context, not silence or blockage."""
    _env(monkeypatch)
    r = FakeRedis(registry={"ci-agent": "tmux"})
    r.hashes[prefix(POD, TENANT, "ci-agent", "presence")] = {"state": "idle"}
    r.hashes[prefix(POD, TENANT, "ci-agent", "blocked")] = {
        "since": "not-a-timestamp",
        "stream_id": "a" * 32,
    }

    with patch("modules.office.cli._context", return_value=(r, POD, TENANT, "architect")):
        office_main(["status", "ci-agent"])

    row = capsys.readouterr().out
    assert "ci-agent    idle" in row
    assert "delivery unverified (age unknown)" in row


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
