"""Stream-id delivery trace (core/receipts.py) and `office delivery`.

Real Redis: the scan is a Lua script, which a hand-written double cannot vouch for.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import redis

H_APP = Path(__file__).resolve().parents[1]
if str(H_APP) not in sys.path:
    sys.path.insert(0, str(H_APP))

from core.envelope import build, encode
from core.keys import (
    prefix,
    receive_opened_key,
    receive_opening_key,
    receive_processing_key,
    receive_undeliverable_key,
    receive_unresolved_key,
)
from core.receipts import STAGE_STATE, locate, overall_state, trace
from modules.office.cli import main as office_main

POD = "trace"


@pytest.fixture
def office():
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        r.ping()
    except Exception:
        pytest.skip("real Redis server not available at REDIS_URL")
    tenant = f"t-{uuid4().hex[:12]}"
    registry = prefix(POD, tenant, resource="registry")
    r.hset(registry, mapping={"alice": "tmux", "bob": "tmux", "web": "api"})
    yield r, tenant
    keys = list(r.scan_iter(match=f"pod:{POD}:tenant:{tenant}:*"))
    if keys:
        r.delete(*keys)


def make(tenant, destination="bob", **kwargs):
    envelope = build(
        "Message", "alice", destination, {"text": "hi"}, pod=POD, tenant=tenant, **kwargs
    )
    return envelope, encode(envelope)


def state_of(r, tenant, stream_id):
    return trace(r, pod=POD, tenant=tenant, stream_id=stream_id)


@pytest.mark.parametrize(
    "stage,state",
    [
        ("egress", "queued"),
        ("ingress", "queued"),
        ("processing", "queued"),
        ("opening", "delivering"),
        ("opened", "delivered"),
        ("dead", "failed"),
    ],
    ids=["egress", "ingress", "processing", "opening", "opened", "dead"],
)
def test_each_agent_stage_maps_to_its_state(office, stage, state):
    r, tenant = office
    envelope, raw = make(tenant)
    keys = {
        "egress": prefix(POD, tenant, "alice", "egress"),
        "ingress": prefix(POD, tenant, "bob", "ingress"),
        "processing": receive_processing_key(POD, tenant, "bob"),
        "opening": receive_opening_key(POD, tenant, "bob"),
        "opened": receive_opened_key(POD, tenant, "bob"),
        "dead": prefix(POD, tenant, "alice", "dead"),
    }
    r.rpush(keys[stage], raw)
    result = state_of(r, tenant, envelope["stream_id"])
    assert result["state"] == state
    assert [loc["stage"] for loc in result["locations"]] == [stage]
    assert STAGE_STATE[stage] == state


def test_unresolved_and_undeliverable_records_plain_and_hex(office):
    r, tenant = office
    plain, plain_raw = make(tenant)
    hexed, hex_raw = make(tenant)
    gone, gone_raw = make(tenant)
    r.rpush(receive_unresolved_key(POD, tenant), json.dumps(
        {"agent": "bob", "reason": "x", "envelope": plain_raw}))
    r.rpush(receive_unresolved_key(POD, tenant), json.dumps(
        {"agent": "bob", "reason": "x", "encoding": "hex", "envelope": hex_raw.encode().hex()}))
    r.rpush(receive_undeliverable_key(POD, tenant), json.dumps(
        {"agent": "bob", "reason": "retired", "encoding": "hex", "envelope": gone_raw.encode().hex()}))

    assert state_of(r, tenant, plain["stream_id"])["state"] == "unresolved"
    assert state_of(r, tenant, hexed["stream_id"])["state"] == "unresolved"
    gone_result = state_of(r, tenant, gone["stream_id"])
    assert gone_result["state"] == "failed"
    assert gone_result["locations"] == [{"agent": None, "stage": "undeliverable"}]


def test_api_inbox_entry_means_delivered(office):
    r, tenant = office
    envelope, _ = make(tenant, destination="web")
    r.xadd(prefix(POD, tenant, "web", "inbox"), {"envelope": json.dumps(envelope)})
    result = state_of(r, tenant, envelope["stream_id"])
    assert result["state"] == "delivered"
    assert result["locations"] == [{"agent": "web", "stage": "inbox"}]


def test_unknown_when_nothing_holds_the_frame(office):
    r, tenant = office
    result = state_of(r, tenant, "0" * 32)
    assert result == {"stream_id": "0" * 32, "state": "unknown", "locations": []}


def test_only_the_header_stream_id_matches_not_a_reference_to_it(office):
    r, tenant = office
    target, _ = make(tenant)
    # A reply that merely names the target as in_reply_to / in its text.
    reply, reply_raw = make(tenant, in_reply_to=target["stream_id"])
    r.rpush(prefix(POD, tenant, "bob", "ingress"), reply_raw)
    r.xadd(prefix(POD, tenant, "web", "inbox"), {"envelope": json.dumps(reply)})
    assert state_of(r, tenant, target["stream_id"])["state"] == "unknown"
    assert state_of(r, tenant, reply["stream_id"])["state"] == "partial"  # ingress + inbox copy


def test_broadcast_copies_at_different_stages_are_partial(office):
    r, tenant = office
    envelope, raw = make(tenant, destination="all")
    r.rpush(prefix(POD, tenant, "bob", "ingress"), raw)
    r.rpush(receive_opened_key(POD, tenant, "alice"), raw)
    result = state_of(r, tenant, envelope["stream_id"])
    assert result["state"] == "partial"
    assert {(l["agent"], l["stage"]) for l in result["locations"]} == {
        ("bob", "ingress"), ("alice", "opened"),
    }


def test_wrong_type_key_does_not_blind_the_trace(office):
    r, tenant = office
    envelope, raw = make(tenant)
    r.set(prefix(POD, tenant, "alice", "dead"), "hostile")
    r.set(prefix(POD, tenant, "web", "inbox"), "hostile")
    r.rpush(prefix(POD, tenant, "bob", "ingress"), raw)
    assert state_of(r, tenant, envelope["stream_id"])["state"] == "queued"


def test_retired_sender_still_traced_through_extra_agents(office):
    r, tenant = office
    envelope, raw = make(tenant)
    r.rpush(prefix(POD, tenant, "ghost", "egress"), raw)  # ghost is not registered
    assert locate(r, pod=POD, tenant=tenant, stream_id=envelope["stream_id"]) == []
    found = locate(
        r, pod=POD, tenant=tenant, stream_id=envelope["stream_id"], extra_agents=("ghost",)
    )
    assert [(l.agent, l.stage) for l in found] == [("ghost", "egress")]


def test_confirmation_reports_only_retained_watchdog_evidence(office):
    r, tenant = office
    envelope, raw = make(tenant)
    r.rpush(receive_opened_key(POD, tenant, "bob"), raw)
    assert state_of(r, tenant, envelope["stream_id"])["confirmation"] is None

    r.xadd(prefix(POD, tenant, "bob", "pending.verify"), {"stream_id": envelope["stream_id"]})
    assert state_of(r, tenant, envelope["stream_id"])["confirmation"] == "pending"

    r.hset(prefix(POD, tenant, "bob", "blocked"), mapping={"since": "", "stream_id": envelope["stream_id"]})
    assert state_of(r, tenant, envelope["stream_id"])["confirmation"] == "unverified"


def test_queued_message_has_no_confirmation_field(office):
    r, tenant = office
    envelope, raw = make(tenant)
    r.rpush(prefix(POD, tenant, "bob", "ingress"), raw)
    assert "confirmation" not in state_of(r, tenant, envelope["stream_id"])


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "A" * 32, "g" * 32, "0" * 31, "0" * 33],
    ids=["empty", "short", "upper", "nonhex", "len31", "len33"],
)
def test_malformed_stream_id_is_rejected_before_redis(bad):
    with pytest.raises(ValueError):
        locate(object(), pod=POD, tenant="t", stream_id=bad)


def test_overall_state_is_unknown_for_no_locations():
    assert overall_state([]) == "unknown"


# --- office delivery -------------------------------------------------------


def _cli(r, tenant, argv, monkeypatch):
    monkeypatch.setenv("AGENT_NAME", "alice")
    monkeypatch.setenv("POD", POD)
    monkeypatch.setenv("TENANT", tenant)
    with patch("modules.office.cli.redis.Redis.from_url", return_value=r):
        office_main(argv)


def test_office_delivery_prints_one_json_line(office, monkeypatch, capsys):
    r, tenant = office
    envelope, raw = make(tenant)
    r.rpush(prefix(POD, tenant, "bob", "ingress"), raw)
    _cli(r, tenant, ["delivery", envelope["stream_id"]], monkeypatch)
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "stream_id": envelope["stream_id"],
        "state": "queued",
        "locations": [{"agent": "bob", "stage": "ingress"}],
    }
    assert out.count("\n") == 1
    assert r.llen(prefix(POD, tenant, "bob", "ingress")) == 1  # read-only


def test_office_delivery_wait_returns_once_delivered(office, monkeypatch, capsys):
    r, tenant = office
    envelope, raw = make(tenant)
    r.rpush(prefix(POD, tenant, "bob", "ingress"), raw)
    sleeps = []

    def advance(seconds):
        sleeps.append(seconds)
        r.lrem(prefix(POD, tenant, "bob", "ingress"), 1, raw)
        r.rpush(receive_opened_key(POD, tenant, "bob"), raw)

    with patch("modules.office.cli.time.sleep", side_effect=advance):
        _cli(r, tenant, ["delivery", envelope["stream_id"], "--wait", "30"], monkeypatch)
    assert json.loads(capsys.readouterr().out)["state"] == "delivered"
    assert len(sleeps) == 1


def test_office_delivery_wait_stops_at_the_deadline(office, monkeypatch, capsys):
    r, tenant = office
    envelope, raw = make(tenant)
    r.rpush(prefix(POD, tenant, "bob", "ingress"), raw)
    _cli(r, tenant, ["delivery", envelope["stream_id"], "--wait", "0"], monkeypatch)
    assert json.loads(capsys.readouterr().out)["state"] == "queued"


@pytest.mark.parametrize(
    "argv",
    [["delivery", "nothex"], ["delivery", "0" * 32, "--wait", "-1"]],
    ids=["not-hex", "negative-wait"],
)
def test_office_delivery_rejects_bad_input_without_touching_redis(office, monkeypatch, capsys, argv):
    r, tenant = office
    with pytest.raises(SystemExit) as exc:
        _cli(r, tenant, argv, monkeypatch)
    assert exc.value.code == 1
    assert "office: error:" in capsys.readouterr().err
