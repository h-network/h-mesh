"""`office run` windows are visible (status / peers -i) and removable (run-rm).

Real tmux and Redis through the same fixture test_office_run.py uses; the
reconciler is stepped by hand so every assertion is deterministic.
"""

import json
import sys

import pytest

from core.channels import DeadLetter
from core.keys import prefix, run_remove_key, run_windows_key
from modules.office import cli
from modules.tmux import ops, port
from test_office_run import _dead, _eventually, _wait_dead_status, run_office  # noqa: F401  (fixture)

SLEEP = ["/bin/sleep", "86400"]


@pytest.fixture
def office(run_office, monkeypatch):
    ctx = run_office
    ctx["r"].set(prefix(ctx["pod"], ctx["tenant"], resource="lead"), "architect")
    monkeypatch.setattr(cli, "_context", lambda: (ctx["r"], ctx["pod"], ctx["tenant"], "architect"))
    return ctx


def _published(ctx):
    return {
        k.decode(): json.loads(v)
        for k, v in ctx["r"].hgetall(run_windows_key(ctx["pod"], ctx["tenant"])).items()
    }


def _open_exited(ctx, name="once", status=7):
    cli.main(["run", name, "--", sys.executable, "-c", f"import sys; sys.exit({status})"])
    _eventually(lambda: _dead(ctx, f"__run__{name}"))
    _wait_dead_status(ctx, f"__run__{name}")   # tmux records the status only when it reaps the child


def test_reconciler_publishes_running_and_exited_run_windows(office):
    cli.main(["run", "monitor", "--", *SLEEP])
    _open_exited(office)
    office["reconciler"].reconcile_once(office["r"])
    published = _published(office)
    assert set(published) == {"monitor", "once"}
    assert published["monitor"]["state"] == "running" and published["monitor"]["exit_status"] is None
    assert published["monitor"]["command"] == "sleep"
    assert published["once"]["state"] == "exited" and published["once"]["exit_status"] == 7


def test_publishing_again_keeps_since_and_a_state_change_moves_it(office):
    cli.main(["run", "job", "--", sys.executable, "-c", "import time; time.sleep(1.5)"])
    office["reconciler"].reconcile_once(office["r"])
    first = _published(office)["job"]
    assert first["state"] == "running"
    office["reconciler"].reconcile_once(office["r"])
    assert _published(office)["job"] == first
    _eventually(lambda: _dead(office, "__run__job"))
    _wait_dead_status(office, "__run__job")
    office["reconciler"].reconcile_once(office["r"])
    after = _published(office)["job"]
    assert after["state"] == "exited" and after["exit_status"] == 0
    assert after["since"] >= first["since"]


def test_a_window_closed_by_hand_disappears_from_the_published_state(office):
    cli.main(["run", "monitor", "--", *SLEEP])
    office["reconciler"].reconcile_once(office["r"])
    assert "monitor" in _published(office)
    ops.run_tmux("kill-window", "-t", f"{office['tenant']}:__run__monitor", socket=office["socket"])
    office["reconciler"].reconcile_once(office["r"])
    assert _published(office) == {}


def test_status_and_peers_list_run_windows_with_their_state(office, capsys):
    cli.main(["run", "monitor", "--", *SLEEP])
    _open_exited(office)
    office["reconciler"].reconcile_once(office["r"])
    capsys.readouterr()
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "run:monitor" in out and "running" in out and "run window, not an agent: sleep" in out
    assert "run:once" in out and "exited 7" in out
    assert out.index("architect") < out.index("run:monitor")
    cli.main(["peers", "-i"])
    out = capsys.readouterr().out
    assert "run windows (non-addressable" in out
    assert "monitor (running)" in out and "once (exited 7)" in out
    cli.main(["peers"])
    assert "monitor" not in capsys.readouterr().out      # plain peers stays colleagues only


def test_a_dead_window_without_a_recorded_status_reads_pending_then_the_code(office, capsys):
    key = run_windows_key(office["pod"], office["tenant"])
    pending = {"state": "exited", "exit_status": None, "signal": None, "command": "sh", "since": "t"}
    office["r"].hset(key, "late", json.dumps(pending))
    cli.main(["status"])
    assert "exited (status pending)" in capsys.readouterr().out
    cli.main(["peers", "-i"])
    assert "late (exited (status pending))" in capsys.readouterr().out
    office["r"].hset(key, "late", json.dumps({**pending, "exit_status": 7}))
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "exited 7" in out and "pending" not in out


def test_publish_rewrites_a_pending_exit_once_the_status_is_known(office, monkeypatch):
    from modules.tmux import run_windows
    info = {"window_id": "@9", "command": "sh", "state": "exited", "exit_status": None, "signal": None}
    kw = dict(pod=office["pod"], tenant=office["tenant"], session=office["tenant"], socket=office["socket"])
    monkeypatch.setattr(run_windows, "snapshot", lambda *a, **k: {"late": dict(info)})
    run_windows.publish(office["r"], **kw)
    assert _published(office)["late"]["exit_status"] is None
    monkeypatch.setattr(run_windows, "snapshot", lambda *a, **k: {"late": {**info, "exit_status": 3}})
    run_windows.publish(office["r"], **kw)
    assert _published(office)["late"]["exit_status"] == 3


def test_status_without_run_windows_prints_no_run_line(office, capsys):
    cli.main(["status"])
    cli.main(["peers", "-i"])
    out = capsys.readouterr().out
    assert "run:" not in out and "run windows" not in out


def test_status_of_one_name_says_a_run_window_is_not_an_agent(office, capsys):
    cli.main(["run", "monitor", "--", *SLEEP])
    office["reconciler"].reconcile_once(office["r"])
    with pytest.raises(SystemExit) as error:
        cli.main(["status", "monitor"])
    assert error.value.code == 1
    assert "run window, not an agent" in capsys.readouterr().err


def test_run_rm_asks_the_reconciler_and_removes_only_that_window(office, capsys):
    cli.main(["run", "a", "--", *SLEEP])
    cli.main(["run", "ab", "--", *SLEEP])
    office["reconciler"].reconcile_once(office["r"])
    with pytest.raises(SystemExit) as error:
        cli.main(["run-rm", "a", "--wait", "0"])
    assert error.value.code == 2                      # requested, not yet confirmed
    assert "not yet confirmed" in capsys.readouterr().err
    assert office["r"].smembers(run_remove_key(office["pod"], office["tenant"])) == {b"a"}
    assert {"__run__a", "__run__ab"} <= ops.list_windows(office["tenant"], socket=office["socket"])
    office["reconciler"].reconcile_once(office["r"])
    windows = ops.list_windows(office["tenant"], socket=office["socket"])
    assert "__run__a" not in windows and "__run__ab" in windows       # prefix-safe: by window id
    assert set(_published(office)) == {"ab"}
    assert office["r"].scard(run_remove_key(office["pod"], office["tenant"])) == 0


def test_run_rm_confirms_once_the_reconciler_has_acted(office, monkeypatch, capsys):
    _open_exited(office)                              # an exited window is removable too
    office["reconciler"].reconcile_once(office["r"])
    monkeypatch.setattr(cli.time, "sleep", lambda _s: office["reconciler"].reconcile_once(office["r"]))
    capsys.readouterr()
    cli.main(["run-rm", "once", "--wait", "5"])
    assert "removed run window once" in capsys.readouterr().out
    assert "__run__once" not in ops.list_windows(office["tenant"], socket=office["socket"])


def test_run_rm_is_lead_only_and_refuses_unknown_or_invalid_names(office, monkeypatch, capsys):
    cli.main(["run", "monitor", "--", *SLEEP])
    office["reconciler"].reconcile_once(office["r"])
    queue = run_remove_key(office["pod"], office["tenant"])
    for name in ("ghost", "__run__monitor", "../x", "all"):
        with pytest.raises(SystemExit) as error:
            cli.main(["run-rm", name, "--wait", "0"])
        assert error.value.code == 1
    monkeypatch.setattr(cli, "_context", lambda: (office["r"], office["pod"], office["tenant"], "worker"))
    with pytest.raises(SystemExit) as error:
        cli.main(["run-rm", "monitor", "--wait", "0"])
    assert error.value.code == 1 and "lead-only" in capsys.readouterr().err
    assert office["r"].scard(queue) == 0
    assert "__run__monitor" in ops.list_windows(office["tenant"], socket=office["socket"])


def test_a_removal_request_for_a_vanished_window_is_cleared(office):
    queue = run_remove_key(office["pod"], office["tenant"])
    office["r"].sadd(queue, "ghost")
    office["reconciler"].reconcile_once(office["r"])
    assert office["r"].scard(queue) == 0


def test_a_run_window_fault_never_stops_agent_reconciliation(office, monkeypatch):
    from modules.tmux import run_windows

    def boom(*_a, **_k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(run_windows, "publish", boom)
    ops.run_tmux("new-window", "-d", "-t", f"{office['tenant']}:", "-n", "keepalive", "/bin/sleep", "86400",
                 socket=office["socket"])
    ops.run_tmux("kill-window", "-t", f"{office['tenant']}:architect", socket=office["socket"])
    office["reconciler"].reconcile_once(office["r"])           # must not raise
    assert "architect" in ops.list_windows(office["tenant"], socket=office["socket"])   # recreated


def test_nothing_can_deliver_to_a_run_window_even_with_a_forged_registry_entry(office, monkeypatch):
    # Defence in depth: if someone enrols the bare name `monitor` as a tmux agent
    # the window it would target is `monitor`, which does not exist -- the
    # `__run__monitor` pane is never the target, so nothing is pasted into it.
    cli.main(["run", "monitor", "--", *SLEEP])
    office["r"].hset(office["registry"], "monitor", "tmux")
    marker = office["root"] / "must-not-execute"
    real_tmux = ops.run_tmux

    def forbid_paste(*args, **kwargs):
        assert args[0] not in ("load-buffer", "paste-buffer", "send-keys"), "attempted delivery"
        return real_tmux(*args, **kwargs)

    monkeypatch.setattr(ops, "run_tmux", forbid_paste)
    with pytest.raises(DeadLetter):
        port.message_opener(office["r"], office["pod"], office["tenant"], "monitor",
                            {"payload": {"text": f"touch {marker}"}}, office["tenant"], office["socket"])
    assert not marker.exists()
