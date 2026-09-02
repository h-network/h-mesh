import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.channels import DeadLetter
from lib.agentlifecycle.lifecycle import _IncompleteLifecycle
from modules.office import port


POD = "testpod"
TENANT = "testtenant"


def test_lifecycle_validation_failure_is_explicit_rejection():
    with pytest.raises(DeadLetter):
        port._lifecycle_opener(
            port.start_agent, r=MagicMock(), pod=POD, tenant=TENANT,
            envelope={"payload": {"agent": "worker", "profile": "bad profile!"}},
            replace_window=MagicMock(), available_profiles=lambda *_: None,
        )


def test_lifecycle_unknown_failure_is_not_misclassified_as_rejection():
    r = MagicMock()
    r.set.side_effect = ValueError("redis encoder rejected after write attempt")
    with pytest.raises(_IncompleteLifecycle, match="outcome UNKNOWN"):
        port._lifecycle_opener(
            port.start_agent, r=r, pod=POD, tenant=TENANT,
            envelope={"payload": {"agent": "worker"}},
            replace_window=MagicMock(), available_profiles=lambda *_: None,
        )


def test_late_validation_rejection_writes_nothing_and_is_dead_letterable():
    r = MagicMock()
    with pytest.raises(DeadLetter, match="payload.resume must be a boolean"):
        port._lifecycle_opener(
            port.start_agent, r=r, pod=POD, tenant=TENANT,
            envelope={"payload": {"agent": "worker", "resume": "yes"}},
            replace_window=MagicMock(), available_profiles=lambda *_: None,
        )

    # The harm is partial desired state followed by a dead-letter verdict.
    # A proven rejection must reach the dead path with no lifecycle writes.
    assert r.method_calls == []


@patch("modules.office.port.receive")
def test_deliver_office_registers_all_lifecycle_openers(mock_receive):
    r = MagicMock()
    port.deliver_office(r, pod=POD, tenant=TENANT, agent="host")

    kwargs = mock_receive.call_args.kwargs
    assert set(kwargs["openers"]) == {
        "StartAgent", "StopAgent", "PauseAgent", "ResumeAgent"
    }
    assert kwargs["module"] == "office"
    assert kwargs["timeout"] == 0
    assert kwargs["blocking"] is False


@patch("modules.office.port.receive")
@patch("modules.office.port.start_agent")
def test_start_opener_injects_catalog_and_replace_window(mock_start, mock_receive):
    r = MagicMock()
    port.deliver_office(
        r, pod=POD, tenant=TENANT, agent="host", session_name="session", socket="sock"
    )
    envelope = {"kind": "StartAgent", "payload": {"agent": "new-agent"}}
    mock_receive.call_args.kwargs["openers"]["StartAgent"](envelope)

    kwargs = mock_start.call_args.kwargs
    assert kwargs["envelope"] is envelope
    assert kwargs["available_profiles"](POD, TENANT) is None
    with patch("modules.office.port.kill_window", return_value=(0, "", "")) as kill:
        kwargs["replace_window"]("new-agent")
    kill.assert_called_once_with("session", "new-agent", socket="sock")


@patch("modules.office.port.receive")
@patch("modules.office.port.run_tmux", return_value=(0, "", ""))
@patch("modules.office.port.resume_agent")
def test_resume_opener_injects_tmux_resume_and_kick(mock_resume, mock_tmux, mock_receive):
    r = MagicMock()
    port.deliver_office(
        r, pod=POD, tenant=TENANT, agent="host", session_name="session", socket="sock"
    )
    envelope = {"kind": "ResumeAgent", "payload": {"agent": "bob"}}
    mock_receive.call_args.kwargs["openers"]["ResumeAgent"](envelope)

    kwargs = mock_resume.call_args.kwargs
    kwargs["resume_window"]("bob")
    mock_tmux.assert_called_once_with(
        "send-keys", "-t", "session:bob", "startAgent --resume", "Enter", socket="sock"
    )
    with patch("modules.office.port.subprocess.Popen") as popen:
        kwargs["kick_agent"]("bob")
    popen.assert_called_once_with([sys.executable, "-m", "modules.tmux.port", "bob"])


def test_nested_kick_preserves_switch_custody_pipe():
    read_fd, write_fd = os.pipe()
    try:
        with (
            patch.dict(os.environ, {"H_MESH_LOG_FILE": f"/proc/self/fd/{write_fd}"}),
            patch("modules.office.port.subprocess.Popen") as popen,
        ):
            port._kick("bob")
        popen.assert_called_once_with(
            [sys.executable, "-m", "modules.tmux.port", "bob"],
            pass_fds=(write_fd,),
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


@patch("modules.office.port.receive")
def test_tmux_callback_failure_is_not_silenced(mock_receive):
    r = MagicMock()
    port.deliver_office(r, pod=POD, tenant=TENANT, agent="host", session_name="session")
    with patch("modules.office.port.kill_window", return_value=(1, "", "missing")):
        with patch("modules.office.port.stop_agent") as mock_stop:
            mock_receive.call_args.kwargs["openers"]["StopAgent"]({"payload": {"agent": "bob"}})
            callback = mock_stop.call_args.kwargs["kill_window"]
            with pytest.raises(RuntimeError, match="kill-window failed: missing"):
                callback("bob")


@patch("modules.office.port.deliver_office")
@patch("modules.office.port.delivery_lock")
@patch("modules.office.port.redis.Redis.from_url")
def test_main_owns_redis_and_delivery_lock(mock_from_url, mock_lock, mock_deliver, monkeypatch):
    r = MagicMock()
    r.get.return_value = None
    mock_from_url.return_value = r
    mock_lock.return_value.__enter__.return_value = None
    monkeypatch.setenv("POD", POD)
    monkeypatch.setenv("TENANT", TENANT)
    monkeypatch.setenv("REDIS_URL", "redis://example/4")

    port.main(["host"])

    mock_from_url.assert_called_once_with("redis://example/4")
    mock_lock.assert_called_once_with(r, pod=POD, tenant=TENANT, agent="host")
    mock_deliver.assert_called_once_with(r, pod=POD, tenant=TENANT, agent="host")


def test_main_requires_agent(capsys):
    with pytest.raises(SystemExit) as exc:
        port.main([])
    assert exc.value.code == 1
    assert "modules.office.port <agent>" in capsys.readouterr().err
