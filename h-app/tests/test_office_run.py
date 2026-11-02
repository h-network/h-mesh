"""Raw office processes are windows, never messaging participants."""

import json
import os
import sys
import time
from pathlib import Path

import pytest
import redis

from core.channels import send
from core.envelope import EnvelopeError, parse
from core.keys import prefix
from modules.office import cli
from modules.tmux import ops, port
from modules.tmux.reconciler import TmuxReconciler


@pytest.fixture
def run_office(managed_tmpdir, monkeypatch):
    root = Path(managed_tmpdir("h_mesh_test_run_"))
    home = root / "home"
    home.mkdir()
    work = root / "work"
    work.mkdir()
    socket = str(root / "s")
    pod, tenant = "run-test", f"t-{os.urandom(4).hex()}"
    for key, value in {
        "HOME": str(home), "H_MESH_STATE_DIR": str(root / "state"),
        "H_MESH_WORKDIR": str(work), "TMUX_SOCKET": socket, "TMUX_SESSION": tenant,
        "POD": pod, "TENANT": tenant, "AGENT_NAME": "architect",
        "TASK_RECORD": "synthetic-record", "H_MESH_CUSTODY_FILE": "synthetic-custody",
        "H_MESH_WRITER": "synthetic-writer",
    }.items():
        monkeypatch.setenv(key, value)
    r = redis.Redis.from_url(os.environ["REDIS_URL"])
    registry = prefix(pod, tenant, resource="registry")
    r.hset(registry, mapping={"architect": "tmux"})
    code, _, error = ops.run_tmux("new-session", "-d", "-s", tenant, "-n", "architect",
                                  "/bin/sleep", "86400", socket=socket)
    assert code == 0, error
    reconciler = TmuxReconciler(pod, tenant, os.environ["REDIS_URL"],
                                 session_name=tenant, socket=socket)
    try:
        yield dict(root=root, work=work, home=home, socket=socket, pod=pod, tenant=tenant,
                   r=r, registry=registry, reconciler=reconciler)
    finally:
        monkeypatch.delenv("TMUX", raising=False)
        ops.run_tmux("kill-server", socket=socket)
        keys = list(r.scan_iter(match=f"pod:{pod}:tenant:{tenant}:*"))
        if keys:
            r.delete(*keys)


def _eventually(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "raw process did not reach the expected state"


def _dead(ctx, window):
    code, output, _ = ops.run_tmux("display-message", "-p", "-t", f"{ctx['tenant']}:{window}",
                                    "#{pane_dead}", socket=ctx["socket"])
    return code == 0 and output == "1"


def test_run_cli_default_shell_without_redis_or_enrollment(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("TMUX_SESSION", "office")
    monkeypatch.setattr(cli, "_context", lambda: pytest.fail("run must not use messaging context"))
    monkeypatch.setattr(ops, "create_run_window", lambda *a, **k: calls.append((a, k)) or "__run__scratch")
    cli.main(["run", "scratch"])
    assert calls[0][0] == ("office", "scratch", None)
    assert "non-addressable window __run__scratch" in capsys.readouterr().out


def test_run_cli_preserves_command_arguments_and_cwd(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setenv("TMUX_SESSION", "office")
    monkeypatch.setattr(ops, "create_run_window", lambda *a, **k: calls.append((a, k)) or "__run__monitor")
    command = ["printf", "%s", "literal ; $(false) 'quote'", "--cwd"]
    cli.main(["run", "monitor", "--cwd", str(tmp_path), "--", *command])
    assert calls[0][0] == ("office", "monitor", command)
    assert calls[0][1]["cwd"] == str(tmp_path)


@pytest.mark.parametrize("args", [[], ["scratch", "--"], ["scratch", "top"], ["scratch", "--unknown"]],
                         ids=["missing-name", "empty-command", "missing-separator", "unknown-option"])
def test_run_cli_refuses_bad_usage(args, monkeypatch):
    monkeypatch.setattr(ops, "create_run_window", lambda *a, **k: pytest.fail("unexpected launch"))
    with pytest.raises(SystemExit) as error:
        cli.main(["run", *args])
    assert error.value.code == 2


@pytest.mark.parametrize("name", ["1", "../bad", "__run__bad", "all"],
                         ids=["numeric", "path", "reserved-prefix", "broadcast"])
def test_run_refuses_invalid_labels_before_tmux(name, monkeypatch):
    monkeypatch.setattr(ops, "run_tmux", lambda *a, **k: pytest.fail("unexpected tmux access"))
    with pytest.raises(ValueError, match="run name"):
        ops.create_run_window("office", name)


def test_run_refuses_missing_directory_before_tmux(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "run_tmux", lambda *a, **k: pytest.fail("unexpected tmux access"))
    with pytest.raises(ValueError, match="directory"):
        ops.create_run_window("office", "scratch", cwd=str(tmp_path / "absent"))


def test_run_cli_reports_tmux_failure(monkeypatch, capsys):
    monkeypatch.setenv("TMUX_SESSION", "office")
    def fail(*args, **kwargs):
        raise ops.TmuxCommandError("new-window", 1, "session unavailable")
    monkeypatch.setattr(ops, "create_run_window", fail)
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "scratch"])
    assert error.value.code == 1
    assert "session unavailable" in capsys.readouterr().err


def test_run_inside_office_keeps_ambient_socket_refusal(run_office, monkeypatch, capsys):
    ctx = run_office
    monkeypatch.setenv("TMUX", f"{ctx['socket']},123,0")
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "scratch"])
    assert error.value.code == 1
    assert "ambient tmux server" in capsys.readouterr().err
    monkeypatch.delenv("TMUX")
    assert ops.list_windows(ctx["tenant"], socket=ctx["socket"]) == {"architect"}


def test_run_command_keeps_literal_argv_cwd_and_no_agent_identity(run_office):
    ctx = run_office
    output = ctx["root"] / "result.json"
    script = (
        "import json, os, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'args':sys.argv[2:], 'cwd':os.getcwd(), "
        "'identity':[k for k in ('AGENT_NAME','TASK_RECORD','H_MESH_CUSTODY_FILE','H_MESH_WRITER') if k in os.environ]}))"
    )
    words = ["spaces in one argument", "$(false);literal", 'a"quote', "--model"]
    cli.main(["run", "literal", "--cwd", str(ctx["work"]), "--",
              sys.executable, "-c", script, str(output), *words])
    _eventually(lambda: _dead(ctx, "__run__literal"))
    result = json.loads(output.read_text())
    assert result == {"args": words, "cwd": str(ctx["work"]), "identity": []}
    assert not (ctx["work"] / "AGENTS.md").exists()
    assert not (ctx["work"] / "CLAUDE.md").exists()
    assert ctx["r"].hgetall(ctx["registry"]) == {b"architect": b"tmux"}


def test_run_default_shell_allows_operator_input_without_agent_identity(run_office):
    ctx = run_office
    marker = ctx["work"] / "operator-input"
    cli.main(["run", "scratch", "--cwd", str(ctx["work"])])
    window = "__run__scratch"
    # Direct operator keystrokes are allowed; messaging paste is forbidden.
    command = f'test -z "${{AGENT_NAME-}}" && printf shell-ok > {marker}'
    code, _, error = ops.run_tmux("send-keys", "-t", f"{ctx['tenant']}:{window}", command, "Enter",
                                  socket=ctx["socket"])
    assert code == 0, error
    _eventually(lambda: marker.exists() and marker.read_text() == "shell-ok")
    assert marker.read_text() == "shell-ok"
    assert not _dead(ctx, window)


def test_run_duplicate_name_is_refused_without_replacing_existing_window(run_office):
    ctx = run_office
    cli.main(["run", "monitor", "--", "/bin/sleep", "86400"])
    before = ops.run_tmux("list-windows", "-t", ctx["tenant"], "-F", "#{window_id}:#{window_name}",
                          socket=ctx["socket"])[1]
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "monitor", "--", "/bin/true"])
    assert error.value.code == 1
    assert before == ops.run_tmux("list-windows", "-t", ctx["tenant"], "-F", "#{window_id}:#{window_name}",
                                  socket=ctx["socket"])[1]


@pytest.mark.parametrize("status", [0, 7], ids=["success", "failure"])
def test_run_exits_once_retains_output_and_is_not_restarted(run_office, status):
    ctx = run_office
    marker = ctx["work"] / "runs"
    script = "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.write_text(p.read_text()+'x' if p.exists() else 'x'); print('one-shot-output'); sys.exit(int(sys.argv[2]))"
    cli.main(["run", "once", "--", sys.executable, "-c", script, str(marker), str(status)])
    window = "__run__once"
    _eventually(lambda: _dead(ctx, window))
    for _ in range(3):
        ctx["reconciler"].reconcile_once(ctx["r"])
    assert marker.read_text() == "x"
    assert window in ops.list_windows(ctx["tenant"], socket=ctx["socket"])
    assert window not in ctx["reconciler"]._failure_counts
    assert window not in ctx["reconciler"]._spawned_agents
    assert window not in ctx["reconciler"]._known_windows
    assert ops.run_tmux("display-message", "-p", "-t", f"{ctx['tenant']}:{window}",
                         "#{pane_dead_status}", socket=ctx["socket"])[1] == str(status)
    # Reconciliation can resize an unattached window and move output out of
    # the visible screen. Retention includes scrollback, not just the viewport.
    assert "one-shot-output" in ops.run_tmux("capture-pane", "-p", "-J", "-S", "-", "-t", f"{ctx['tenant']}:{window}",
                                               socket=ctx["socket"])[1]


def test_reconciler_preserves_run_window_when_roster_is_empty(run_office):
    ctx = run_office
    cli.main(["run", "monitor", "--", "/bin/sleep", "86400"])
    ctx["r"].delete(ctx["registry"])
    ops.run_tmux("kill-window", "-t", f"{ctx['tenant']}:architect", socket=ctx["socket"])
    ctx["reconciler"].reconcile_once(ctx["r"])
    assert ops.list_windows(ctx["tenant"], socket=ctx["socket"]) == {"__init__", "__run__monitor"}


def test_closed_run_window_is_not_recreated(run_office):
    ctx = run_office
    cli.main(["run", "monitor", "--", "/bin/sleep", "86400"])
    ops.run_tmux("kill-window", "-t", f"{ctx['tenant']}:__run__monitor", socket=ctx["socket"])
    ctx["reconciler"].reconcile_once(ctx["r"])
    assert "__run__monitor" not in ops.list_windows(ctx["tenant"], socket=ctx["socket"])


@pytest.mark.parametrize("kind", ["Message", "Command", "Attachment"])
def test_run_window_cannot_be_an_envelope_destination(run_office, kind):
    ctx = run_office
    cli.main(["run", "scratch"])
    for address in ("__run__scratch", f"{ctx['pod']}:{ctx['tenant']}:__run__scratch"):
        with pytest.raises(EnvelopeError):
            send(ctx["r"], pod=ctx["pod"], tenant=ctx["tenant"], source="architect",
                 destination=address, kind=kind, payload={"text": "must never be pasted"})
    assert ctx["r"].llen(prefix(ctx["pod"], ctx["tenant"], "architect", "egress")) == 0


@pytest.mark.parametrize("opener", ["message_opener", "command_opener"], ids=["message", "command"])
def test_tmux_openers_cannot_paste_into_run_windows(run_office, opener, monkeypatch):
    ctx = run_office
    cli.main(["run", "scratch"])
    marker = ctx["root"] / "must-not-execute"
    real_tmux = ops.run_tmux
    def forbid_paste(*args, **kwargs):
        assert args[0] not in ("load-buffer", "paste-buffer", "send-keys"), "attempted raw-window delivery"
        return real_tmux(*args, **kwargs)
    monkeypatch.setattr(ops, "run_tmux", forbid_paste)
    with pytest.raises(ValueError, match="do not accept message delivery"):
        getattr(port, opener)(ctx["r"], ctx["pod"], ctx["tenant"], "__run__scratch",
                              {"payload": {"text": f"\ntouch {marker}\n"}}, ctx["tenant"], ctx["socket"])
    assert not marker.exists()


def test_send_alias_refused_and_broadcast_excludes_run_window(run_office, monkeypatch):
    ctx = run_office
    monkeypatch.setattr(cli, "_context", lambda: (ctx["r"], ctx["pod"], ctx["tenant"], "architect"))
    cli.main(["run", "monitor", "--", "/bin/sleep", "86400"])
    with pytest.raises(SystemExit) as error:
        cli.main(["send", "-a", "monitor", "not an agent"])
    assert error.value.code == 1
    ctx["r"].hset(ctx["registry"], "bob", "tmux")
    cli.main(["broadcast", "hello peers"])
    queued = ctx["r"].lrange(prefix(ctx["pod"], ctx["tenant"], "architect", "egress"), 0, -1)
    assert len(queued) == 1
    assert parse(queued[0])["l2"]["destination"] == "bob"


@pytest.mark.parametrize("failure", ["set-window-option", "respawn-pane"], ids=["configure", "launch"])
def test_run_failure_cleanup_targets_only_the_created_window(monkeypatch, tmp_path, failure):
    calls = []
    monkeypatch.setattr(ops, "list_windows", lambda *a, **k: set())
    def fake_tmux(*args, **kwargs):
        calls.append(args)
        if args[0] == "new-window":
            return 0, "@42 %57", ""
        if args[0] == "list-windows":
            return 0, "architect\n__run__monitor", ""
        if args[0] == failure:
            return 1, "", "synthetic failure"
        return 0, "", ""
    monkeypatch.setattr(ops, "run_tmux", fake_tmux)
    with pytest.raises(ops.TmuxCommandError):
        ops.create_run_window("office", "monitor", ["top"], cwd=str(tmp_path))
    assert calls[-1] == ("kill-window", "-t", "@42")
    if failure == "set-window-option":
        assert not any(call[0] == "respawn-pane" for call in calls)


def test_concurrent_run_name_is_refused_before_launch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(ops, "list_windows", lambda *a, **k: set())
    def fake_tmux(*args, **kwargs):
        calls.append(args)
        if args[0] == "new-window":
            return 0, "@42 %57", ""
        if args[0] == "list-windows":
            return 0, "__run__monitor\n__run__monitor", ""
        return 0, "", ""
    monkeypatch.setattr(ops, "run_tmux", fake_tmux)
    with pytest.raises(ValueError, match="concurrently"):
        ops.create_run_window("office", "monitor", ["top"], cwd=str(tmp_path))
    assert calls[-1] == ("kill-window", "-t", "@42")
    assert not any(call[0] == "respawn-pane" for call in calls)
