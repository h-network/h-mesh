import os
import sys
from unittest.mock import ANY, MagicMock, call, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.keys import prefix
from lib.agentlifecycle.lifecycle import start_agent, stop_agent


POD = "testpod"
TENANT = "testtenant"


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
