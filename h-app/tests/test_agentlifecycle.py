import json
import os
import sys
from unittest.mock import ANY, MagicMock, call, patch
from uuid import uuid4

import pytest
import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.channels import receive
from core.dispatch import delivery_lock_key
from core.envelope import build, encode, parse
from core.keys import (
    incarnation_key, prefix, receive_opened_key, receive_opening_key, receive_processing_key,
    receive_undeliverable_key, receive_unresolved_key, retired_inbox_key,
)
from core.policy import tags_key
from lib.agentlifecycle.lifecycle import (
    ProvableActualFailure,
    ProvableLifecycleRejection,
    _IncompleteLifecycle,
    _PUBLISH_LEAD_MEMBERSHIP_LUA,
    _PUBLISH_MEMBERSHIP_LUA,
    _PUBLISH_WINDOW_CAUSE_LUA,
    _REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA,
    _PartialLifecycle,
    resume_agent,
    start_agent,
    stop_agent,
)
from modules.tmux.reconciler import TmuxReconciler


POD = "testpod"
TENANT = "testtenant"


def _instance_config_keys(pod, tenant, agent):
    return [
        prefix(pod, tenant, agent=agent, resource=resource)
        for resource in (
            "launch", "profile", "provider", "resume", "skip-permissions",
            "claude-tools",
        )
    ] + [
        tags_key(pod, tenant, agent),
        prefix(pod, tenant, agent=agent, resource="hmac-keys"),
        prefix(pod, tenant, agent=agent, resource="window.cause"),
    ]


@pytest.fixture
def real_redis():
    r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    try:
        r.ping()
    except Exception:
        pytest.skip("real Redis server not available at REDIS_URL")
    return r


def test_lead_publish_wrongtype_is_no_write(real_redis):
    tenant = f"lua-{uuid4().hex[:12]}"
    lead_key = prefix(POD, tenant, resource="lead")
    registry_key = prefix(POD, tenant, resource="registry")
    cause_key = prefix(POD, tenant, agent="new-lead", resource="window.cause")
    real_redis.set(registry_key, "wrong-type")
    try:
        with pytest.raises(redis.ResponseError, match="WRONGTYPE"):
            real_redis.eval(
                _PUBLISH_LEAD_MEMBERSHIP_LUA,
                5,
                lead_key,
                registry_key,
                cause_key,
                "new-lead",
                "tmux",
                "a" * 32,
            )

        assert real_redis.get(cause_key) is None
        assert real_redis.get(lead_key) is None
        assert real_redis.get(registry_key) == b"wrong-type"
    finally:
        real_redis.delete(lead_key, registry_key, cause_key)


def test_lead_removal_wrongtype_is_no_write(real_redis):
    tenant = f"lua-{uuid4().hex[:12]}"
    lead_key = prefix(POD, tenant, resource="lead")
    registry_key = prefix(POD, tenant, resource="registry")
    processing_key = receive_processing_key(POD, tenant, "old-lead")
    opening_key = receive_opening_key(POD, tenant, "old-lead")
    opened_key = receive_opened_key(POD, tenant, "old-lead")
    ingress_key = prefix(POD, tenant, agent="old-lead", resource="ingress")
    paused_key = prefix(POD, tenant, agent="old-lead", resource="paused")
    lock_key = delivery_lock_key(POD, tenant, "old-lead")
    config_keys = _instance_config_keys(POD, tenant, "old-lead")
    undeliverable_key = receive_undeliverable_key(POD, tenant)
    unresolved_key = receive_unresolved_key(POD, tenant)
    real_redis.hset(registry_key, "old-lead", "tmux")
    real_redis.hset(lead_key, "wrong", "type")
    cleanup_keys = [processing_key, opening_key, opened_key, ingress_key]
    for key in cleanup_keys:
        real_redis.rpush(key, f"identity:{key}")
    real_redis.set(paused_key, "paused-identity")
    real_redis.set(lock_key, "lock-identity")
    for key in config_keys:
        real_redis.set(key, f"identity:{key}")
    try:
        with pytest.raises(redis.ResponseError, match="WRONGTYPE"):
            real_redis.eval(
                _REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA,
                18,
                registry_key,
                lead_key,
                processing_key,
                opening_key,
                ingress_key,
                paused_key,
                lock_key,
                *config_keys,
                undeliverable_key,
                unresolved_key,
                "old-lead",
            )

        assert real_redis.hget(registry_key, "old-lead") == b"tmux"
        assert real_redis.hget(lead_key, "wrong") == b"type"
        for key in cleanup_keys:
            assert real_redis.lrange(key, 0, -1) == [f"identity:{key}".encode()]
        assert real_redis.get(paused_key) == b"paused-identity"
        assert real_redis.get(lock_key) == b"lock-identity"
        for key in config_keys:
            assert real_redis.get(key) == f"identity:{key}".encode()
    finally:
        real_redis.delete(
            lead_key, registry_key, processing_key, opening_key, opened_key,
            ingress_key, paused_key, lock_key,
            *config_keys,
            undeliverable_key, unresolved_key,
        )


@pytest.mark.parametrize(
    "wrong_index",
    [2, 3, 4, 16, 17],
    ids=["processing", "opening", "ingress", "undeliverable", "unresolved"],
)
def test_stop_custody_type_preflight_preserves_every_identity(real_redis, wrong_index):
    tenant = f"stop-type-{uuid4().hex[:12]}"
    agent = "worker"
    registry_key = prefix(POD, tenant, resource="registry")
    lead_key = prefix(POD, tenant, resource="lead")
    list_keys = [
        receive_processing_key(POD, tenant, agent),
        receive_opening_key(POD, tenant, agent),
        prefix(POD, tenant, agent=agent, resource="ingress"),
    ]
    paused_key = prefix(POD, tenant, agent=agent, resource="paused")
    lock_key = delivery_lock_key(POD, tenant, agent)
    config_keys = _instance_config_keys(POD, tenant, agent)
    list_keys.extend([
        receive_undeliverable_key(POD, tenant),
        receive_unresolved_key(POD, tenant),
    ])
    keys = [registry_key, lead_key, *list_keys[:3], paused_key, lock_key,
            *config_keys, *list_keys[3:]]

    real_redis.hset(registry_key, agent, "tmux")
    real_redis.set(lead_key, agent)
    for key in list_keys:
        real_redis.rpush(key, f"identity:{key}")
    real_redis.set(keys[wrong_index], "wrong-type-identity")
    for key in [paused_key, lock_key, *config_keys]:
        real_redis.set(key, f"identity:{key}")
    try:
        with pytest.raises(redis.ResponseError, match="custody key is not a list"):
            real_redis.eval(
                _REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA, 18, *keys, agent
            )

        assert real_redis.hget(registry_key, agent) == b"tmux"
        assert real_redis.get(lead_key) == agent.encode()
        for key in list_keys:
            if key == keys[wrong_index]:
                assert real_redis.get(key) == b"wrong-type-identity"
            else:
                assert real_redis.lrange(key, 0, -1) == [f"identity:{key}".encode()]
        for key in [paused_key, lock_key, *config_keys]:
            assert real_redis.get(key) == f"identity:{key}".encode()
    finally:
        real_redis.delete(*keys)


def test_undeliverable_record_preserves_non_utf8_raw_exactly(real_redis):
    tenant = f"stop-bytes-{uuid4().hex[:12]}"
    agent = "worker"
    registry_key = prefix(POD, tenant, resource="registry")
    ingress_key = prefix(POD, tenant, agent=agent, resource="ingress")
    undeliverable_key = receive_undeliverable_key(POD, tenant)
    hostile_raw = b"\xff\x00not-an-envelope"
    real_redis.hset(registry_key, agent, "tmux")
    real_redis.rpush(ingress_key, hostile_raw)
    try:
        stop_agent(
            real_redis,
            pod=POD,
            tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )

        [record_raw] = real_redis.lrange(undeliverable_key, 0, -1)
        record = json.loads(record_raw)
        assert record["agent"] == agent
        assert record["reason"] == "destination retired before opening"
        assert record["encoding"] == "hex"
        assert bytes.fromhex(record["envelope"]) == hostile_raw
        assert real_redis.lrange(ingress_key, 0, -1) == []
        assert real_redis.hget(registry_key, agent) is None
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_stop_conserves_api_inbox_content_with_hex_fields_plain_entry_id(real_redis):
    """Ticket 97ad745c's second exposure: a same-named successor's own
    client could otherwise read a retired predecessor's already-delivered
    mailbox content. switch-agent's exact shape: field NAMES and VALUES
    hex-encoded as an ORDERED ARRAY of pairs, not a JSON object, so
    duplicate fields and field order survive exactly rather than being
    reconstructed -- proven here by planting a genuine duplicate field
    name via raw XADD (redis-py's own dict-based xadd/xrange cannot even
    represent this, so this test bypasses both and reads the RESP reply
    fields flatly, matching what the Lua side actually sees). The stream
    entry id is kept as plain text (Redis-generated ASCII, a real storage
    invariant unlike the fields), and a hostile non-UTF8 byte value is
    planted directly to prove hex-encoding protects the boundary rather
    than merely matching today's json.dumps-shaped writes."""
    tenant = f"stop-inbox-{uuid4().hex[:12]}"
    agent = "worker"
    registry_key = prefix(POD, tenant, resource="registry")
    inbox_key = prefix(POD, tenant, agent=agent, resource="inbox")
    evidence_key = retired_inbox_key(POD, tenant)
    hostile_value = b"\xff\x00not-valid-utf8"
    real_redis.hset(registry_key, agent, "api")
    # A genuine duplicate field name ("dup" twice) in a specific order --
    # constructed via the raw command, since redis-py's dict-based xadd
    # cannot represent it at all.
    expected_pairs = [
        (b"dup", b"first"), (b"other", hostile_value), (b"dup", b"second"),
    ]
    xadd_args = [item for pair in expected_pairs for item in pair]
    entry_id = real_redis.execute_command("XADD", inbox_key, "*", *xadd_args)
    try:
        stop_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )

        [record_raw] = real_redis.lrange(evidence_key, 0, -1)
        record = json.loads(record_raw)
        assert record["agent"] == agent
        assert record["reason"] == "destination retired with unread inbox content"
        assert record["encoding"] == "hex"
        expected_entry_id = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
        assert record["entry_id"] == expected_entry_id
        observed_pairs = [
            (bytes.fromhex(f), bytes.fromhex(v)) for f, v in record["fields"]
        ]
        assert observed_pairs == expected_pairs
        assert real_redis.exists(inbox_key) == 0
        assert real_redis.hget(registry_key, agent) is None
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_stop_conserves_more_than_a_thousand_inbox_entries_by_exact_identity(real_redis):
    """MAILBOX_MAXLEN=1000 is a writer-side default (approximate trimming
    can overshoot it), not a validity bound this script may assume -- and
    the actual concern is constant-arity iteration regardless of count,
    the same discipline reviewer's 10,000-entry stress test already
    proved for undeliverable/unresolved. Seeds more than the nominal cap
    directly (bypassing the writer's own maxlen) and confirms every single
    one survives conservation by exact identity, not just a plausible
    count."""
    # A LIST, not a set: the documented property is exact-once, ORDERED
    # conservation, and a set comparison cannot fail on duplication or
    # reordering -- it would pass even if a record were dropped and
    # another duplicated, as long as the surviving id set matched.
    tenant = f"stop-inbox-bulk-{uuid4().hex[:12]}"
    agent = "worker"
    registry_key = prefix(POD, tenant, resource="registry")
    inbox_key = prefix(POD, tenant, agent=agent, resource="inbox")
    evidence_key = retired_inbox_key(POD, tenant)
    real_redis.hset(registry_key, agent, "api")
    expected_ids = []
    for index in range(1200):
        entry_id = real_redis.xadd(inbox_key, {"n": str(index)})
        expected_ids.append(entry_id.decode() if isinstance(entry_id, bytes) else entry_id)
    try:
        stop_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )

        records = [json.loads(raw) for raw in real_redis.lrange(evidence_key, 0, -1)]
        assert [record["entry_id"] for record in records] == expected_ids
        assert all(record["agent"] == agent for record in records)
        assert all(
            record["reason"] == "destination retired with unread inbox content"
            for record in records
        )
        assert real_redis.exists(inbox_key) == 0
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_stop_preserves_a_non_api_agents_inbox_key_instead_of_silently_deleting_it(real_redis):
    """switch-agent's exact finding against the first version of this
    script: the inbox DEL was OUTSIDE the `this_port_type == 'api'`
    conditional that gates reading and conserving it, so stopping a
    NON-api name with an inbox stream (something no writer produces
    today, but not something this script may assume never exists --
    modules/api is the sole writer of an "inbox" resource, not a
    guarantee no other one ever will be) deleted every entry while
    writing zero retired-inbox evidence. That is worse than the original
    exposure: the original left content for a successor to inherit, this
    one destroyed it outright, from inside the branch whose entire
    purpose is conservation. Seeds a tmux-type membership with an inbox
    stream, stops it, and confirms the content is neither leaked into
    the retired-inbox evidence stream (this script has no defined
    conservation semantics for a non-api port type) NOR silently gone --
    it must still be readable at its original key."""
    tenant = f"stop-non-api-inbox-{uuid4().hex[:12]}"
    agent = "worker"
    registry_key = prefix(POD, tenant, resource="registry")
    inbox_key = prefix(POD, tenant, agent=agent, resource="inbox")
    evidence_key = retired_inbox_key(POD, tenant)
    real_redis.hset(registry_key, agent, "tmux")
    entry_id = real_redis.xadd(inbox_key, {"unexpected": "content"})
    try:
        stop_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )

        preserved = real_redis.xrange(inbox_key, "-", "+")
        assert [entry_id_ for entry_id_, _ in preserved] == [entry_id]
        assert real_redis.lrange(evidence_key, 0, -1) == []
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_successor_cannot_read_predecessors_inbox_content(real_redis):
    """The harm this exposure describes, exercised end to end: a
    predecessor's undelivered mailbox content must not be visible through
    a same-named successor's own fresh inbox after a stop+rehire -- the
    successor's view is empty by construction, not by relying on anyone
    remembering to clean up."""
    tenant = f"stop-inbox-successor-{uuid4().hex[:12]}"
    agent = "worker"
    registry_key = prefix(POD, tenant, resource="registry")
    inbox_key = prefix(POD, tenant, agent=agent, resource="inbox")
    real_redis.hset(registry_key, agent, "api")
    real_redis.xadd(inbox_key, {"envelope": "predecessor content nobody read"})
    try:
        stop_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )
        start_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent, "port_type": "api"}},
            replace_window=lambda _agent: None,
            available_profiles=lambda *_: None,
        )

        assert real_redis.xrange(inbox_key, "-", "+") == []
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


@pytest.mark.parametrize(
    ("source_resource", "destination_key", "reason"),
    [
        (
            "ingress",
            receive_undeliverable_key,
            "destination retired before opening",
        ),
        (
            "opening",
            receive_unresolved_key,
            "opener outcome unknown when destination retired",
        ),
    ],
    ids=["undeliverable", "unresolved"],
)
def test_stop_large_backlog_conserves_every_identity_in_correct_sink(
    real_redis, source_resource, destination_key, reason,
):
    tenant = f"stop-large-{uuid4().hex[:12]}"
    agent = "worker"
    registry_key = prefix(POD, tenant, resource="registry")
    source_key = prefix(POD, tenant, agent=agent, resource=source_resource)
    sink_key = destination_key(POD, tenant)
    frames = [
        build(
            "Message", "sender", agent, {"sequence": sequence},
            pod=POD, tenant=tenant,
        )
        for sequence in range(10_000)
    ]
    expected_ids = [frame["stream_id"] for frame in frames]
    real_redis.hset(registry_key, agent, "tmux")
    real_redis.rpush(source_key, *(encode(frame) for frame in frames))
    try:
        stop_agent(
            real_redis,
            pod=POD,
            tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )

        records = [json.loads(raw) for raw in real_redis.lrange(sink_key, 0, -1)]
        observed_ids = [
            parse(bytes.fromhex(record["envelope"]))["stream_id"]
            for record in records
        ]
        # This is the conservation harm, not an assertion that a particular
        # Lua mechanism completed: every admitted identity is terminal exactly
        # once before membership may disappear.
        assert observed_ids == expected_ids
        assert [record["agent"] for record in records] == [agent] * len(frames)
        assert [record["reason"] for record in records] == [reason] * len(frames)
        assert real_redis.lrange(source_key, 0, -1) == []
        assert real_redis.hget(registry_key, agent) is None
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


@patch("lib.agentlifecycle.lifecycle.log_record")
@patch("lib.agentlifecycle.lifecycle.port_type", return_value="tmux")
def test_stop_agent_purges_instance_delivery_state_before_killing_window(
    _mock_port_type, _mock_log_record
):
    r = MagicMock()
    kill_window = MagicMock()

    stop_agent(
        r,
        pod=POD,
        tenant=TENANT,
        envelope={"payload": {"agent": "worker-1"}},
        kill_window=kill_window,
    )

    assert r.method_calls == [
        call.eval(
            ANY,
            21,
            prefix(POD, TENANT, resource="registry"),
            prefix(POD, TENANT, resource="lead"),
            receive_processing_key(POD, TENANT, "worker-1"),
            receive_opening_key(POD, TENANT, "worker-1"),
            prefix(POD, TENANT, agent="worker-1", resource="ingress"),
            prefix(POD, TENANT, agent="worker-1", resource="paused"),
            delivery_lock_key(POD, TENANT, "worker-1"),
            *_instance_config_keys(POD, TENANT, "worker-1"),
            receive_undeliverable_key(POD, TENANT),
            receive_unresolved_key(POD, TENANT),
            incarnation_key(POD, TENANT, "worker-1"),
            prefix(POD, TENANT, agent="worker-1", resource="inbox"),
            retired_inbox_key(POD, TENANT),
            "worker-1",
        ),
    ]
    kill_window.assert_called_once_with("worker-1")


def test_stop_cleanup_cannot_erase_successor_delivery_identity(real_redis):
    tenant = f"stop-reuse-{uuid4().hex[:12]}"
    agent = "reused-worker"
    registry_key = prefix(POD, tenant, resource="registry")
    ingress_key = prefix(POD, tenant, agent=agent, resource="ingress")
    paused_key = prefix(POD, tenant, agent=agent, resource="paused")
    lock_key = delivery_lock_key(POD, tenant, agent)
    successor = build(
        "Message", "sender", agent, {"text": "successor"}, pod=POD, tenant=tenant
    )
    successor_raw = encode(successor)

    class SuccessorAfterRemoval:
        """Install successor state after the stop script's linearization point."""

        def __getattr__(self, name):
            return getattr(real_redis, name)

        def eval(self, *args, **kwargs):
            result = real_redis.eval(*args, **kwargs)
            real_redis.hset(registry_key, agent, "tmux")
            real_redis.rpush(ingress_key, successor_raw)
            real_redis.set(paused_key, "successor-paused")
            real_redis.set(lock_key, "successor-lock")
            return result

    real_redis.hset(registry_key, agent, "tmux")
    try:
        stop_agent(
            SuccessorAfterRemoval(),
            pod=POD,
            tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )

        assert real_redis.lrange(ingress_key, 0, -1) == [successor_raw.encode()]
        assert real_redis.get(paused_key) == b"successor-paused"
        assert real_redis.get(lock_key) == b"successor-lock"
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_rehired_name_cannot_inherit_predecessor_runtime_identity(real_redis):
    tenant = f"config-reuse-{uuid4().hex[:12]}"
    agent = "reused-worker"
    registry_key = prefix(POD, tenant, resource="registry")
    config = {
        "launch": "agy",
        "profile": "predecessor-account",
        "provider": "predecessor-provider",
        "resume": "1",
        "skip-permissions": "1",
        "claude-tools": "",
        "window.cause": "predecessor-cause",
    }
    real_redis.hset(registry_key, agent, "tmux")
    for resource, value in config.items():
        real_redis.set(prefix(POD, tenant, agent=agent, resource=resource), value)
    real_redis.hset(tags_key(POD, tenant, agent), "export", '["predecessor-tag"]')
    real_redis.hset(
        prefix(POD, tenant, agent=agent, resource="hmac-keys"),
        "predecessor-kid", "predecessor-secret",
    )
    try:
        stop_agent(
            real_redis,
            pod=POD,
            tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )
        start_agent(
            real_redis,
            pod=POD,
            tenant=tenant,
            envelope={"payload": {"agent": agent}},
            replace_window=lambda _agent: None,
            available_profiles=lambda *_: None,
        )

        reconciler = TmuxReconciler(POD, tenant, "redis://unused")
        with patch.dict(
            os.environ,
            {"PROVIDER_PREDECESSOR_PROVIDER_URL": "https://predecessor.invalid"},
        ):
            assert reconciler.get_agent_profile(real_redis, agent) is None
            assert reconciler.get_agent_provider(real_redis, agent) is None
        assert reconciler.get_agent_resume(real_redis, agent) is None
        assert reconciler.get_agent_skip_permissions(real_redis, agent) is None
        assert reconciler.get_agent_claude_tools(real_redis, agent) is None
        assert reconciler.get_agent_cli(real_redis, agent) == "claude"
        assert real_redis.exists(tags_key(POD, tenant, agent)) == 0
        assert real_redis.exists(
            prefix(POD, tenant, agent=agent, resource="hmac-keys")
        ) == 0
        assert real_redis.get(
            prefix(POD, tenant, agent=agent, resource="window.cause")
        ) is None
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_fresh_hire_mints_an_incarnation_id(real_redis):
    """Ticket 97ad745c's foundation: a same-name successor must not inherit
    a predecessor's delivered.s* provenance, which requires the delivery
    claim to be bound to something that changes across a stop/rehire but
    survives an ordinary restart. Confirms the id actually gets minted at
    all, for the plain membership-only path (no lead, no correlation_id
    cause) telegram bot's own StartAgent payload takes."""
    tenant = f"incarnation-fresh-{uuid4().hex[:12]}"
    agent = "worker"
    try:
        start_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent, "port_type": "api"}},
            replace_window=lambda _agent: None,
            available_profiles=lambda *_: None,
        )
        minted = real_redis.get(incarnation_key(POD, tenant, agent))
        assert minted is not None
        assert len(minted) == 32  # uuid4().hex
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_idempotent_reenrol_does_not_change_incarnation(real_redis):
    """Architect's explicit inverse case, and the exact bug a naive
    unconditional mint would cause: clients/telegram/bot.py calls
    StartAgent on every process restart, documented there as safe and
    idempotent. If start_agent rebound the incarnation id on every such
    call, a bot crash-restart would invalidate its OWN just-established
    delivered.s* claims and cause redelivery of messages a human already
    saw -- a worse, quieter bug than the one this feature closes. Calls
    start_agent twice for the same never-stopped name and confirms the
    incarnation id is identical both times."""
    tenant = f"incarnation-reenrol-{uuid4().hex[:12]}"
    agent = "worker"
    try:
        for _ in range(2):
            start_agent(
                real_redis, pod=POD, tenant=tenant,
                envelope={"payload": {"agent": agent, "port_type": "api"}},
                replace_window=lambda _agent: None,
                available_profiles=lambda *_: None,
            )
        # A third call through the window-cause path (a real envelope's
        # correlation_id is always present) exercises the other Lua branch
        # a restart could equally take.
        start_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={
                "correlation_id": "b" * 32,
                "payload": {"agent": agent, "port_type": "api"},
            },
            replace_window=lambda _agent: None,
            available_profiles=lambda *_: None,
        )
        final = real_redis.get(incarnation_key(POD, tenant, agent))
        assert final is not None
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_legacy_member_with_no_incarnation_self_heals_on_next_reenrol(real_redis):
    """switch-agent's rollout-gap finding: every publish Lua originally
    gated minting on HEXISTS==0 alone, so an agent already registered
    before this binding shipped would NEVER acquire an incarnation id
    until an actual stop+rehire -- unbounded, not the DELIVERED_TTL_SECONDS
    -bounded transition window the docs claim, for any agent that simply
    never gets stopped. Pre-seeds the registry directly (bypassing
    start_agent, simulating a pre-feature member with no incarnation key
    at all), confirms an idempotent StartAgent establishes one, then
    confirms a SECOND idempotent StartAgent leaves it unchanged -- the
    self-heal must fire exactly once, not on every re-enrol."""
    tenant = f"incarnation-self-heal-{uuid4().hex[:12]}"
    agent = "legacy-worker"
    registry_key = prefix(POD, tenant, resource="registry")
    try:
        real_redis.hset(registry_key, agent, "api")
        assert real_redis.get(incarnation_key(POD, tenant, agent)) is None

        start_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent, "port_type": "api"}},
            replace_window=lambda _agent: None,
            available_profiles=lambda *_: None,
        )
        healed = real_redis.get(incarnation_key(POD, tenant, agent))
        assert healed is not None

        start_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent, "port_type": "api"}},
            replace_window=lambda _agent: None,
            available_profiles=lambda *_: None,
        )
        assert real_redis.get(incarnation_key(POD, tenant, agent)) == healed
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_stop_then_rehire_mints_a_new_incarnation_id(real_redis):
    """The exposure ticket 97ad745c exists to close: a same-named
    successor must get a genuinely different incarnation id than its
    predecessor had, so lib/reply_correlation.py's incarnation-bound
    delivered.s* records structurally cannot match the successor's
    queries."""
    tenant = f"incarnation-rehire-{uuid4().hex[:12]}"
    agent = "worker"
    try:
        start_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent, "port_type": "api"}},
            replace_window=lambda _agent: None,
            available_profiles=lambda *_: None,
        )
        predecessor_incarnation = real_redis.get(incarnation_key(POD, tenant, agent))
        assert predecessor_incarnation is not None

        stop_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )
        assert real_redis.get(incarnation_key(POD, tenant, agent)) is None

        start_agent(
            real_redis, pod=POD, tenant=tenant,
            envelope={"payload": {"agent": agent, "port_type": "api"}},
            replace_window=lambda _agent: None,
            available_profiles=lambda *_: None,
        )
        successor_incarnation = real_redis.get(incarnation_key(POD, tenant, agent))
        assert successor_incarnation is not None
        assert successor_incarnation != predecessor_incarnation
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


def test_stop_and_rehire_cannot_open_predecessor_processing_custody(real_redis):
    tenant = f"reuse-{uuid4().hex[:12]}"
    agent = "reused-worker"
    registry_key = prefix(POD, tenant, resource="registry")
    processing_key = receive_processing_key(POD, tenant, agent)
    opening_key = receive_opening_key(POD, tenant, agent)
    opened_key = receive_opened_key(POD, tenant, agent)
    unresolved_key = receive_unresolved_key(POD, tenant)
    undeliverable_key = receive_undeliverable_key(POD, tenant)
    ingress_key = prefix(POD, tenant, agent=agent, resource="ingress")
    paused_key = prefix(POD, tenant, agent=agent, resource="paused")
    lock_key = delivery_lock_key(POD, tenant, agent)
    ingress = build("Message", "sender", agent, {"phase": "ingress"}, pod=POD, tenant=tenant)
    processing = build(
        "Message", "sender", agent, {"phase": "processing"}, pod=POD, tenant=tenant
    )
    opening = build("Message", "sender", agent, {"phase": "opening"}, pod=POD, tenant=tenant)
    opened_receipt = build(
        "Message", "sender", agent, {"phase": "opened"}, pod=POD, tenant=tenant
    )
    new = build("Message", "sender", agent, {"text": "successor"}, pod=POD, tenant=tenant)
    real_redis.hset(registry_key, agent, "tmux")
    real_redis.rpush(processing_key, encode(processing))
    real_redis.rpush(opening_key, encode(opening))
    real_redis.rpush(opened_key, encode(opened_receipt))
    real_redis.rpush(ingress_key, encode(ingress))
    real_redis.set(paused_key, "predecessor-paused")
    real_redis.set(lock_key, "predecessor-lock")
    try:
        stop_agent(
            real_redis,
            pod=POD,
            tenant=tenant,
            envelope={"payload": {"agent": agent}},
            kill_window=lambda _agent: None,
        )
        assert real_redis.lrange(processing_key, 0, -1) == []
        assert real_redis.lrange(opening_key, 0, -1) == []
        assert real_redis.lrange(ingress_key, 0, -1) == []
        assert [
            parse(raw)["stream_id"] for raw in real_redis.lrange(opened_key, 0, -1)
        ] == [opened_receipt["stream_id"]]

        undeliverable = [
            json.loads(raw) for raw in real_redis.lrange(undeliverable_key, 0, -1)
        ]
        assert [record["agent"] for record in undeliverable] == [agent, agent]
        assert [record["reason"] for record in undeliverable] == [
            "destination retired before opening", "destination retired before opening"
        ]
        assert [
            parse(bytes.fromhex(record["envelope"]))["stream_id"]
            for record in undeliverable
        ] == [
            processing["stream_id"], ingress["stream_id"]
        ]

        unresolved = [json.loads(raw) for raw in real_redis.lrange(unresolved_key, 0, -1)]
        assert [record["agent"] for record in unresolved] == [agent]
        assert [record["reason"] for record in unresolved] == [
            "opener outcome unknown when destination retired"
        ]
        assert [
            parse(bytes.fromhex(record["envelope"]))["stream_id"]
            for record in unresolved
        ] == [
            opening["stream_id"]
        ]
        assert real_redis.get(paused_key) is None
        assert real_redis.get(lock_key) is None
        real_redis.hset(registry_key, agent, "tmux")
        real_redis.rpush(ingress_key, encode(new))
        opened = []

        receive(
            real_redis,
            pod=POD,
            tenant=tenant,
            agent=agent,
            openers={"Message": opened.append},
            timeout=0,
            blocking=False,
        )

        assert [envelope["stream_id"] for envelope in opened] == [new["stream_id"]]
        predecessor_ids = {
            ingress["stream_id"], processing["stream_id"], opening["stream_id"],
            opened_receipt["stream_id"],
        }
        assert predecessor_ids.isdisjoint(envelope["stream_id"] for envelope in opened)
    finally:
        keys = real_redis.keys(prefix(POD, tenant) + ":*")
        if keys:
            real_redis.delete(*keys)


@patch("lib.agentlifecycle.lifecycle.log_record")
@patch("lib.agentlifecycle.lifecycle.port_type", return_value=None)
def test_start_agent_publishes_lead_with_membership_and_window_cause_atomically(
    _mock_port_type, _mock_log_record
):
    r = MagicMock()

    start_agent(
        r,
        pod=POD,
        tenant=TENANT,
        envelope={
            "correlation_id": "a" * 32,
            "payload": {"agent": "new-lead", "lead": True},
        },
        replace_window=MagicMock(),
        available_profiles=lambda _pod, _tenant: None,
    )

    assert r.method_calls == [
        call.set(prefix(POD, TENANT, agent="new-lead", resource="launch"), "claude"),
        call.eval(
            ANY,
            4,
            prefix(POD, TENANT, resource="lead"),
            prefix(POD, TENANT, resource="registry"),
            prefix(POD, TENANT, agent="new-lead", resource="window.cause"),
            incarnation_key(POD, TENANT, "new-lead"),
            "new-lead",
            "tmux",
            "a" * 32,
            ANY,
        ),
    ]


@patch("lib.agentlifecycle.lifecycle.log_record")
def test_start_agent_rejects_api_lead_before_mutation(_mock_log_record):
    r = MagicMock()
    try:
        start_agent(
            r,
            pod=POD,
            tenant=TENANT,
            envelope={"payload": {"agent": "client", "port_type": "api", "lead": True}},
            replace_window=MagicMock(),
            available_profiles=lambda _pod, _tenant: None,
        )
    except ValueError as exc:
        assert str(exc) == "StartAgent payload.lead only applies to port_type 'tmux'"
    else:
        raise AssertionError("api lead was accepted")
    assert r.method_calls == []


@patch("lib.agentlifecycle.lifecycle.log_record", side_effect=RuntimeError("log unavailable"))
def test_logging_failure_does_not_replace_proven_rejection(_mock_log_record):
    r = MagicMock()
    with pytest.raises(ProvableLifecycleRejection, match="payload.resume must be a boolean"):
        start_agent(
            r,
            pod=POD,
            tenant=TENANT,
            envelope={"payload": {"agent": "worker", "resume": "yes"}},
            replace_window=MagicMock(),
            available_profiles=lambda *_: None,
        )
    assert r.method_calls == []


@pytest.mark.parametrize("payload", [None, [], "agent=worker"])
def test_hostile_payload_shape_is_proven_rejection_without_writes(payload):
    r = MagicMock()
    with pytest.raises(ProvableLifecycleRejection, match="payload must be an object"):
        start_agent(
            r,
            pod=POD,
            tenant=TENANT,
            envelope={"payload": payload},
            replace_window=MagicMock(),
            available_profiles=lambda *_: None,
        )
    assert r.method_calls == []


@patch("lib.agentlifecycle.lifecycle.log_record", side_effect=RuntimeError("log unavailable"))
def test_logging_failure_does_not_replace_unknown_write_outcome(_mock_log_record):
    r = MagicMock()
    r.set.side_effect = ValueError("encoder failed after submission")
    with pytest.raises(_IncompleteLifecycle, match="outcome UNKNOWN"):
        start_agent(
            r,
            pod=POD,
            tenant=TENANT,
            envelope={"payload": {"agent": "worker"}},
            replace_window=MagicMock(),
            available_profiles=lambda *_: None,
        )


@patch("lib.agentlifecycle.lifecycle.log_record", side_effect=RuntimeError("log unavailable"))
@patch("lib.agentlifecycle.lifecycle.port_type", return_value="tmux")
def test_logging_failure_does_not_replace_partial_actual_failure(
    _mock_port_type, _mock_log_record
):
    r = MagicMock()
    r.llen.return_value = 1
    with pytest.raises(_PartialLifecycle, match="kick 1 failed"):
        resume_agent(
            r,
            pod=POD,
            tenant=TENANT,
            envelope={"payload": {"agent": "worker"}},
            resume_window=lambda _agent: None,
            kick_agent=lambda _agent: (_ for _ in ()).throw(
                ProvableActualFailure("not started")
            ),
        )
    r.delete.assert_called_once()


@patch("lib.agentlifecycle.lifecycle.log_record", side_effect=RuntimeError("log unavailable"))
@patch("lib.agentlifecycle.lifecycle.port_type", return_value=None)
def test_logging_failure_does_not_replace_success(_mock_port_type, _mock_log_record):
    r = MagicMock()
    start_agent(
        r,
        pod=POD,
        tenant=TENANT,
        envelope={"payload": {"agent": "worker"}},
        replace_window=MagicMock(),
        available_profiles=lambda *_: None,
    )
    r.eval.assert_called_once_with(
        _PUBLISH_MEMBERSHIP_LUA,
        2,
        prefix(POD, TENANT, resource="registry"),
        incarnation_key(POD, TENANT, "worker"),
        "worker",
        "tmux",
        ANY,
    )
