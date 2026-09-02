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
    prefix, receive_opened_key, receive_opening_key, receive_processing_key,
    receive_undeliverable_key, receive_unresolved_key,
)
from core.policy import tags_key
from lib.agentlifecycle.lifecycle import (
    ProvableActualFailure,
    ProvableLifecycleRejection,
    _IncompleteLifecycle,
    _PUBLISH_LEAD_MEMBERSHIP_LUA,
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
            18,
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
            3,
            prefix(POD, TENANT, resource="lead"),
            prefix(POD, TENANT, resource="registry"),
            prefix(POD, TENANT, agent="new-lead", resource="window.cause"),
            "new-lead",
            "tmux",
            "a" * 32,
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
    r.hset.assert_called_once_with(
        prefix(POD, TENANT, resource="registry"), "worker", "tmux"
    )
