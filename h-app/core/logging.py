"""Contract-shaped JSON line logging, plus the stdlib logging threshold.

⚠ **Two different systems live here, and they are not interchangeable.**
`log_record`/`emit` write the custody contract: one JSON object per line, on
stdout, mirrored durably, parsed by conservation checks. `configure_logging`
sets the level for `logging.getLogger(...)` diagnostics — human prose, on
stderr, read by whoever is looking at a daemon. A custody record must never be
demoted to a `logger.debug`, and a diagnostic must never be dressed up as a
JSON record. They share this file because it is where anyone looks for
"logging", not because they are one mechanism.

⚠ `import logging` below is the STDLIB module, not this one. Absolute imports
mean a module named `core.logging` importing `logging` gets the standard
library — no cycle, no self-import.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

from .config import state_path

_WRITER = os.environ.get("H_MESH_WRITER")

# ── stdlib logging threshold (diagnostics, stderr) ───────────────────────────
# ⚠ Kept deliberately identical to `clients/telegram/bot.py`'s own copy, down
# to the variable name and the format string, so one `H_MESH_LOG_LEVEL` means
# one thing everywhere. The duplication is the price of the telegram client
# importing nothing from `core` — it talks to a tenant over HTTP and runs from
# a bare checkout — so change both or neither.
LOG_LEVEL_ENV_VAR = "H_MESH_LOG_LEVEL"
# The five standard names, plus the two stdlib aliases, so a deploy that writes
# the obvious WARN is not silently demoted to INFO for a spelling.
LOG_LEVEL_NAMES = ("CRITICAL", "FATAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def resolve_log_level(raw: str | None) -> int:
    """The threshold `H_MESH_LOG_LEVEL` asks for, INFO when it says nothing usable.

    Level NAMES only, case- and whitespace-insensitive; a numeric string is not
    a name and falls back like any other unrecognised value.
    """
    name = (raw or "").strip().upper()
    return getattr(logging, name) if name in LOG_LEVEL_NAMES else logging.INFO


def configure_logging() -> int:
    """`basicConfig` at `H_MESH_LOG_LEVEL`, returning the level applied.

    ⚠ Call this from a process ENTRY POINT (a port's `main()`, a service's
    startup) and never at import of a library module. `core.dispatch` and every
    other module that logs is imported by processes this package does not own —
    including the test suite — and a library that reconfigures the root logger
    on import decides verbosity for all of them.

    Without this, `logging`'s lastResort handler prints WARNING and above as a
    bare message on stderr — no timestamp, no logger name — and drops
    everything below it with no way to turn it up. That is the blind spot this
    closes for the ports; `clients/telegram/bot.py` has the same knob.

    Never raises: an unrecognised value falls back to INFO and says so, because
    verbosity is not worth failing a delivery process at startup over.
    """
    raw = os.environ.get(LOG_LEVEL_ENV_VAR)
    level = resolve_log_level(raw)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    if (raw or "").strip() and (raw or "").strip().upper() not in LOG_LEVEL_NAMES:
        # Loud on purpose, and emitted at the fallback level so it survives it:
        # a mistyped DEGUB that quietly resolved to INFO would rebuild the exact
        # blind spot this knob exists to remove — someone believing they are
        # running at DEBUG while every debug line is still dropped on the floor.
        logging.getLogger(__name__).warning(
            "%s=%r is not a level name (%s); logging at INFO instead",
            LOG_LEVEL_ENV_VAR, raw, ", ".join(LOG_LEVEL_NAMES),
        )
    return level

_ENVELOPE_EVENTS = {
    "sent",
    "send_unknown",
    "popped",
    "forwarded",
    "forward_unknown",
    "source_stamped",
    "kick_started",
    "kick_deferred",
    "kick_unknown",
    "dead_lettered",
    "received",
    "opened",
    "delivery_unjudged",
    "delivery_unverified",
}


def mirror(line: str) -> None:
    """Append one already-formatted record to the durable evidence file.

    ⚠ **Container stdout is deleted with the container.** Docker's `json-file`
    driver goes with `docker compose down`, so this file is the only durable
    evidence that says a run happened once the tenant is gone.

    ⚠ **Call this ONLY where the same line is printed to stdout**, so the file
    stays a byte copy of what `docker logs` shows for that container's lifetime.
    A record written here *instead* of stdout is invisible to `docker logs`; a
    record written here *as well as* by another path is a duplicate, and a
    duplicated custody record is indistinguishable from a duplicated delivery to
    every conservation check we have.

    Never raises. A full or read-only evidence volume must not fail a command.
    """
    path = os.environ.get("H_MESH_CUSTODY_FILE")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as evidence:
            evidence.write(line + "\n")
    except Exception:
        pass


def log_record(
    module: str,
    event: str,
    *,
    stream_id: str | None = None,
    correlation_id: str | None = None,
    source: str | None = None,
    destination: str | None = None,
    reason: str | None = None,
    count: int | None = None,
    task_id: str | None = None,
    waited: int | float | None = None,
    byte_count: int | None = None,
) -> None:
    """One JSON object per line on stdout. Fields absent when not known.

    `stream_id` belongs to envelope events only — it is the join key for one
    envelope's life, and a synthetic value on a lifecycle event makes the six
    records of a real envelope harder to find.

    ⚠ This said "four" until 2026-08-22. A delivered unicast leaves SIX —
    `sent, popped, forwarded, kick_started, received, opened` — and the count in
    this file has now been stale at four, five and six in turn.
    """
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "module": module,
        "event": event,
        "writer": _WRITER or module,
    }
    if event in _ENVELOPE_EVENTS:
        # An envelope event ALWAYS carries the field, so a missing id reads as
        # `unknown` rather than as an absent key that analysis silently skips.
        record["stream_id"] = stream_id or "unknown"
    elif stream_id is not None:
        # ⚠ Any other event keeps an id the caller actually passed. This used to
        # be dropped: the allowlist gated the FIELD rather than only its default,
        # so a new event name lost its identity with no error. Ten records from a
        # test adapter collapsed into one `None` and cost a day to find. Every
        # other optional field below is "include if not None"; this now matches.
        record["stream_id"] = stream_id
    for field, value in (
        ("correlation_id", correlation_id),
        ("source", source),
        ("destination", destination),
        ("reason", reason),
        ("count", count),
        ("task_id", task_id),
        ("waited", waited),
        ("bytes", byte_count),
    ):
        if value is not None:
            record[field] = value
    line = json.dumps(record, separators=(",", ":"))
    # ⚠ Not to stdout when we are inside an agent's window. `office` runs in a
    # pane, so its stdout IS the agent's screen, and printing an envelope record
    # there hands the agent module names, stream ids and correlation ids it has
    # no use for. Measured: an agent read `{"module":"port",...}` out of its
    # own terminal, reasoned that envelope ids imply a broker, went looking, and
    # found Redis. These records reach the log through the window file the
    # switch tails — the print was redundant as well as a signpost. A daemon
    # has no H_MESH_LOG_FILE and still prints to its stdout.
    # ⚠ `office` sets H_MESH_LOG_QUIET because it runs in an agent's PANE: its
    # stdout is the agent's screen. Printing an envelope record there hands the
    # agent module names, stream ids and correlation ids it has no use for.
    # Measured: an agent read {"module":"port",...} out of its own terminal,
    # reasoned that envelope ids imply a broker, went looking and found Redis.
    # The record still reaches the log through the window file the switch tails
    # through the switch's window-file tailer, so nothing is lost. Daemons do
    # not set this and keep printing.
    path = os.environ.get("H_MESH_LOG_FILE")
    if os.environ.get("H_MESH_LOG_QUIET") != "1":
        # One syscall-sized write, newline included. Container daemons share
        # stdout, and print() writes the text and newline separately under
        # PYTHONUNBUFFERED; another process can land its record between them and
        # turn two valid JSON objects into one unparsable line. Records stay
        # below PIPE_BUF, so this single write is atomic against peer writers.
        # Flush separately after the complete-record write: it emits no second
        # record bytes, and keeps timely observation when PYTHONUNBUFFERED is
        # absent instead of making Dockerfile configuration part of this API.
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
        # ⚠ A DURABLE MIRROR OF STDOUT, and deliberately nothing more. Container
        # stdout is Docker's `json-file`, which is deleted with the container —
        # so without this mirror, `docker compose down` destroys the only
        # evidence a run ever happened.
        # ⚠ Gated on the SAME condition as the stdout write, so the file is a
        # byte-for-byte copy of what `docker logs` shows. A pane record is
        # QUIET here and reaches the log once, when the switch re-emits the
        # window file it tails. Mirroring it directly as well would
        # write it TWICE, and a duplicate custody record is indistinguishable
        # from a duplicate delivery to every conservation check we have.
        mirror(line)
    try:
        agent_only = os.environ.get("H_MESH_LOG_FILE_AGENT_ONLY")
        if path and (not agent_only or os.environ.get("AGENT_NAME")):
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        # A central observation failing must never turn into a failed command.
        pass


def emit(
    module: str,
    event: str,
    envelope: dict,
    reason: str | None = None,
    count: int | None = None,
) -> None:
    """`log_record` for the case where the fields come off an envelope."""
    log_record(
        module,
        event,
        stream_id=envelope.get("stream_id"),
        correlation_id=envelope.get("correlation_id"),
        source=envelope.get("l2", {}).get("source"),
        destination=envelope.get("l2", {}).get("destination"),
        reason=reason,
        count=count,
    )


def record_task_event(
    event: str,
    *,
    id: str,
    title: str,
    agent: str,
    actor: str,
    timestamp: str | None = None,
) -> None:
    """Append one board-history event without ever breaking its command."""
    try:
        path = os.environ.get("TASK_RECORD") or state_path("tasks.jsonl")
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        record = {
            "event": event,
            "id": id,
            "title": title,
            "agent": agent,
            "actor": actor,
            "timestamp": timestamp
            or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass
