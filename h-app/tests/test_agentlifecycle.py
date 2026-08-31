import os
import sys
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.keys import prefix
from lib.agentlifecycle.lifecycle import stop_agent


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
        call.hdel(prefix(POD, TENANT, resource="registry"), "worker-1"),
        call.delete(prefix(POD, TENANT, agent="worker-1", resource="ingress")),
        call.delete(prefix(POD, TENANT, agent="worker-1", resource="paused")),
        call.hdel(prefix(POD, TENANT, resource="delivering"), "worker-1"),
    ]
    kill_window.assert_called_once_with("worker-1")
