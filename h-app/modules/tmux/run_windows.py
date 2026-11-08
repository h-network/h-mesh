"""Observe and remove ``office run`` windows (``__run__NAME``).

A run window is a raw process in the office tmux session: no registry entry, no
presence, never a delivery target. Because it has no registry entry, nothing in
Redis knows it exists, and the isolation guard in ``ops.require_isolated_tmux``
stops an agent pane from asking tmux. So the reconciler (a daemon outside any
pane) publishes what it sees to ``run.windows`` and consumes removal requests
from ``run.remove``; ``office status`` / ``peers -i`` / ``run-rm`` only touch Redis.

This module deliberately leaves ``ops.py`` untouched.
"""

import json
from datetime import datetime, timezone

from core.keys import run_remove_key, run_windows_key
from core.logging import log_record

from . import ops

_FORMAT = "\t".join(
    ("#{window_name}", "#{window_id}", "#{pane_dead}", "#{pane_dead_status}",
     "#{pane_dead_signal}", "#{pane_current_command}")
)


def _text(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def snapshot(session: str, socket: str | None = None) -> dict[str, dict]:
    """name -> {state, exit_status, signal, command, window_id} for every run window.

    ``exited`` means every pane in the window is dead (remain-on-exit keeps it
    visible). A window is never restarted.
    """
    code, out, err = ops.run_tmux("list-panes", "-s", "-t", session, "-F", _FORMAT, socket=socket)
    if code != 0:
        raise ops.TmuxCommandError("list-panes", code, err)
    found: dict[str, dict] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 6 or not ops.is_run_window(parts[0]):
            continue
        name = parts[0][len(ops.RUN_WINDOW_PREFIX):]
        window_id, dead, status, signal, command = parts[1:]
        entry = found.setdefault(name, {"window_id": window_id, "command": command,
                                        "alive": False, "exit_status": None, "signal": None})
        if dead == "1":
            entry["exit_status"] = int(status) if status.isdigit() else None
            entry["signal"] = int(signal) if signal.isdigit() and int(signal) else None
        else:
            entry["alive"] = True
            entry["command"] = command
    for entry in found.values():
        alive = entry.pop("alive")
        entry["state"] = "running" if alive else "exited"
        if alive:
            entry["exit_status"] = entry["signal"] = None
    return found


def publish(r, *, pod: str, tenant: str, session: str, socket: str | None = None) -> None:
    """Mirror the live run windows into ``run.windows``; write only on a change."""
    key = run_windows_key(pod, tenant)
    live = snapshot(session, socket)
    stored: dict[str, dict] = {}
    for raw_name, raw in (r.hgetall(key) or {}).items():
        try:
            record = json.loads(_text(raw))
        except ValueError:
            record = {}
        stored[_text(raw_name)] = record if isinstance(record, dict) else {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for name, info in live.items():
        old = stored.get(name, {})
        fields = ("state", "exit_status", "signal")
        if all(old.get(f) == info[f] for f in fields):
            continue
        since = old.get("since") if old.get("state") == info["state"] and old.get("since") else now
        # The command is recorded as first seen: for a shell, pane_current_command
        # changes with whatever the operator types, which is not worth a write.
        command = old.get("command") or info["command"]
        r.hset(key, name, json.dumps({
            "state": info["state"], "exit_status": info["exit_status"], "signal": info["signal"],
            "command": command, "since": since,
        }, separators=(",", ":")))
    for name in stored.keys() - live.keys():
        r.hdel(key, name)


def process_removals(r, *, pod: str, tenant: str, session: str, socket: str | None = None) -> None:
    """Kill each window named in ``run.remove``, by window id, then forget it.

    By id, never by name: tmux resolves ``-t session:name`` by prefix when no
    exact match exists, so removing ``a`` must not be able to take ``ab`` with it.
    """
    requests = run_remove_key(pod, tenant)
    names = sorted(_text(n) for n in (r.smembers(requests) or ()))
    if not names:
        return
    live = snapshot(session, socket)
    for name in names:
        info = live.get(name)
        if info is not None:
            code, _, err = ops.run_tmux("kill-window", "-t", info["window_id"], socket=socket)
            if code != 0:
                log_record("tmux_reconciler", "error", destination=ops.RUN_WINDOW_PREFIX + name,
                           reason=f"run window removal failed: {err}")
                continue
            log_record("tmux_reconciler", "run_window_removed", destination=ops.RUN_WINDOW_PREFIX + name,
                       reason="office run-rm")
        r.srem(requests, name)
        r.hdel(run_windows_key(pod, tenant), name)
