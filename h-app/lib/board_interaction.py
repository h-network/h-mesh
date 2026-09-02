"""Centralized board/ticket operations. Any port type that needs to touch an
agent's board -- write a ticket, and whatever else the board grows to need --
calls into this, rather than reimplementing its own board logic.
"""

import json
import os
from datetime import datetime, timezone

from core.keys import prefix
from core.logging import log_record, record_task_event


class BoardError(ValueError):
    """A stored board entry could not be read as a ticket."""


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)


def normalize_ticket(raw, *, state: str | None = None) -> dict:
    """Normalize a raw board-list entry (or an already-parsed dict) into the
    one ticket shape every board caller reads and writes.

    ``state`` is the fallback ``status`` for an entry that predates that
    field, and (for callers that track it) the board list the entry was read
    from -- callers reading a specific list already know which one.
    """
    if isinstance(raw, dict):
        ticket = raw
    else:
        try:
            ticket = json.loads(_text(raw))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise BoardError("board entry is not a valid ticket") from exc
    if not isinstance(ticket, dict):
        raise BoardError("board entry is not a valid ticket")
    task_id = ticket.get("id")
    title = ticket.get("title")
    if not isinstance(task_id, str) or not task_id:
        raise BoardError("board entry has no valid task id")
    if not isinstance(title, str):
        raise BoardError("board entry has no valid title")
    normalized = {
        "v": ticket.get("v", 1),
        "id": task_id,
        "title": title,
        "description": ticket.get("description", ""),
        "created_by": ticket.get("created_by", ticket.get("from", "unknown")),
        "status": ticket.get("status", state),
        "created_ts": ticket.get("created_ts", ticket.get("created_at", "")),
        "started_ts": ticket.get("started_ts"),
        "done_ts": ticket.get("done_ts"),
        "held_ts": ticket.get("held_ts"),
    }
    if isinstance(ticket.get("hold_reason"), str) and ticket["hold_reason"]:
        normalized["hold_reason"] = ticket["hold_reason"]
    if ticket.get("outcome") in ("completed", "passed", "failed"):
        normalized["outcome"] = ticket["outcome"]
    if ticket.get("priority") is not None:
        normalized["priority"] = ticket["priority"]
    raw_related = ticket.get("related")
    if isinstance(raw_related, list):
        related = [value for value in raw_related if isinstance(value, str)]
        if related:
            normalized["related"] = related
    return normalized


def serialize_ticket(ticket: dict) -> str:
    """Compact JSON for a board-list entry -- not pretty-printed, not a file."""
    return json.dumps(ticket, separators=(",", ":"))


def add_ticket(r, *, pod: str, tenant: str, agent: str, envelope: dict) -> None:
    """Write an AddTicket envelope to the recipient's board.

    An exception from RPUSH is outcome-unknown: Redis may have committed the
    ticket and lost the response. Preserve that generic exception so receive
    custody routes the exact envelope to ``unresolved``. A dead letter is
    reserved for failures proven to reject the envelope before any effect.
    """
    corr_id = envelope.get("correlation_id")
    source = envelope.get("l2", {}).get("source", "unknown")
    payload = envelope.get("payload", {})

    def _related(source_dict) -> list[str]:
        # Stored, never validated: a related id may live on another agent's
        # board entirely, and this has no cross-board lookup.
        raw = source_dict.get("related") if isinstance(source_dict, dict) else None
        return [value for value in raw if isinstance(value, str)] if isinstance(raw, list) else []

    if isinstance(payload, dict) and "v" in payload and "id" in payload:
        ticket_obj = payload
    elif isinstance(payload, dict) and "id" in payload:
        ticket_obj = {
            "v": 1,
            "id": payload.get("id", corr_id or os.urandom(4).hex()),
            "title": payload.get("title", ""),
            "description": payload.get("description", ""),
            "created_by": payload.get("created_by", payload.get("from", source)),
            "status": payload.get("status", "todo"),
            "created_ts": payload.get("created_ts", payload.get("created_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")),
            "started_ts": payload.get("started_ts", ""),
            "done_ts": payload.get("done_ts", ""),
            "priority": payload.get("priority", "normal"),
        }
        related = _related(payload)
        if related:
            ticket_obj["related"] = related
    else:
        title = payload.get("title", "") if isinstance(payload, dict) else str(payload)
        description = payload.get("description", "") if isinstance(payload, dict) else ""
        priority = payload.get("priority", "normal") if isinstance(payload, dict) else "normal"
        task_id = corr_id or os.urandom(4).hex()
        created_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        ticket_obj = {
            "v": 1,
            "id": task_id,
            "title": title,
            "description": description,
            "created_by": source,
            "status": "todo",
            "created_ts": created_ts,
            "started_ts": "",
            "done_ts": "",
            "priority": priority,
        }
        related = _related(payload)
        if related:
            ticket_obj["related"] = related

    todo_key = prefix(pod, tenant, agent=agent, resource="tasks.todo")
    try:
        depth = r.rpush(todo_key, serialize_ticket(ticket_obj))
    except Exception as exc:
        log_record(
            "board_interaction", "board_write_unknown", correlation_id=corr_id,
            destination=agent, reason=f"board write outcome UNKNOWN after {exc}",
            task_id=ticket_obj.get("id", ""),
        )
        raise
    if not isinstance(depth, int) or depth < 1:
        log_record(
            "board_interaction", "board_write_unknown", correlation_id=corr_id,
            destination=agent,
            reason="board write outcome UNKNOWN: RPUSH did not return a positive list length",
            task_id=ticket_obj.get("id", ""),
        )
        raise RuntimeError("board_write_unknown")

    log_record(
        "board_interaction", "board_write_confirmed", correlation_id=corr_id,
        destination=agent, count=depth, task_id=ticket_obj.get("id", ""),
    )

    record_task_event(
        "add",
        id=ticket_obj.get("id", ""),
        title=ticket_obj.get("title", ""),
        agent=agent,
        actor=source,
        timestamp=ticket_obj.get("created_ts"),
    )
