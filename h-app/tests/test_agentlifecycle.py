import os
import sys
from unittest.mock import ANY, MagicMock, call, patch
from uuid import uuid4

import pytest
import redis

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.keys import prefix
from lib.agentlifecycle.lifecycle import (
    _PUBLISH_LEAD_MEMBERSHIP_LUA,
    _REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA,
    start_agent,
    stop_agent,
)


POD = "testpod"
TENANT = "testtenant"


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
                3,
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
    real_redis.hset(registry_key, "old-lead", "tmux")
    real_redis.hset(lead_key, "wrong", "type")
    try:
        with pytest.raises(redis.ResponseError, match="WRONGTYPE"):
            real_redis.eval(
                _REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA,
                2,
                registry_key,
                lead_key,
                "old-lead",
            )

        assert real_redis.hget(registry_key, "old-lead") == b"tmux"
        assert real_redis.hget(lead_key, "wrong") == b"type"
    finally:
        real_redis.delete(lead_key, registry_key)


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
            2,
            prefix(POD, TENANT, resource="registry"),
            prefix(POD, TENANT, resource="lead"),
            "worker-1",
        ),
        call.delete(prefix(POD, TENANT, agent="worker-1", resource="ingress")),
        call.delete(prefix(POD, TENANT, agent="worker-1", resource="paused")),
        call.delete(prefix(POD, TENANT, agent="worker-1", resource="delivering")),
    ]
    kill_window.assert_called_once_with("worker-1")


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
