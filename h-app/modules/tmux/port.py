"""Tmux port: terminal delivery handlers, ingress dispatch, and executable entrypoint."""

import os
import signal
import sys
from datetime import datetime, timezone

import redis

from core.channels import DeadLetter, receive
from core.dispatch import delivery_lock
from core.keys import prefix
from core.registry import port_type
from lib.attachment_schema import validate_attachment_payload
from lib.board_interaction import add_ticket
from lib.paths import get_workdir_root
from .ops import list_windows, submit_text

# The CLIs that write a session file the switch can tail. An agent running
# anything else — a bare shell — produces no activity, so a delivery to it can
# never be confirmed and must not be marked.
#
# ⚠ `agy` joined this set once `~/.gemini/antigravity-cli/history.jsonl` was
# confirmed live and wired into `ActivityTailer` (`watchdog/activity.py`'s
# `_agy_events`) — it records every submitted input, including a paste, so the
# same "input after the marker" aliveness check that verifies claude/codex now
# applies to agy too.
VERIFIABLE_CLIS = frozenset({"claude", "codex", "agy"})


def mark_delivery_pending(
    r,
    pod: str,
    tenant: str,
    agent: str,
    stream_id: str,
    correlation_id: str | None = None,
) -> None:
    """Record a pending delivery verification marker for a verifiable tmux CLI."""
    try:
        if not stream_id:
            return
        # ⚠ An allowlist, not "everything except agy". A marker is only useful
        # for a CLI whose activity we can tail, and anything else can never be
        # confirmed — so it would report unverified forever.
        #
        # Measured: a denylist marked bash windows too (an agent with no launch
        # key at all), and three of the first four unverified records in a live
        # run were those. A CLI we cannot tail must be skipped by default, not
        # by having been remembered.
        launch_key = prefix(pod, tenant, agent=agent, resource="launch")
        raw_cli = r.get(launch_key)
        cli = (raw_cli.decode() if isinstance(raw_cli, bytes) else str(raw_cli)) if raw_cli else ""
        if cli not in VERIFIABLE_CLIS:
            return

        verify_key = prefix(pod, tenant, agent=agent, resource="pending.verify")
        markers_key = prefix(pod, tenant, agent=agent, resource="delivery.markers")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        entry = {"stream_id": stream_id, "ts": ts}
        if correlation_id:
            entry["correlation_id"] = correlation_id
        r.xadd(
            verify_key,
            entry,
            maxlen=100,
            approximate=True,
        )
        # ⚠ 500 IS A SAFETY NET, NOT A POLICY, AND IT CAN LOSE ATTRIBUTION.
        # A marker trimmed here yields a usage record with no stream_id, which is
        # the degradation BUILD-82 §3 specifies — omit rather than guess — so the
        # loss is acceptable and bounded. It is NOT observable: a counter that
        # fired on every uncorrelated record was removed in review because 9 of
        # 27 uncorrelated in the live run were the normal case, and a signal
        # dominated by the normal case is the delivery_unverified defect again.
        # ⚠ Do not "fix" this with an XDEL on attribution. That was built once
        # and deleted the marker BEFORE the claim, turning a retryable XADD miss
        # into permanent loss and letting a duplicate delete a newer marker.
        r.xadd(
            markers_key,
            entry,
            maxlen=500,
            approximate=True,
        )
    except Exception:
        pass


def message_opener(
    r,
    pod: str,
    tenant: str,
    agent: str,
    envelope: dict,
    session_name: str,
    socket: str | None = None,
) -> None:
    windows = list_windows(session_name, socket=socket)
    if agent not in windows:
        raise DeadLetter("window_missing")

    source = envelope.get("l2", {}).get("source", "unknown")
    payload = envelope.get("payload", {})
    text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
    blocks = [f"[message from {source}] {text}\n"]
    try:
        pt = port_type(r, pod=pod, tenant=tenant, agent=source)
    except Exception:
        pt = None
    if pt == "api":
        blocks.append(f"[reply to {source}]\n")

    msg = "".join(blocks)
    stream_id = envelope.get("stream_id", "")
    corr_id = envelope.get("correlation_id")
    mark_delivery_pending(r, pod, tenant, agent, stream_id, correlation_id=corr_id)
    submit_text(session_name, agent, msg, stream_id=stream_id, socket=socket)


def messages_opener(
    r,
    pod: str,
    tenant: str,
    agent: str,
    envelopes: list[dict],
    session_name: str,
    socket: str | None = None,
) -> None:
    for envelope in envelopes:
        message_opener(
            r=r,
            pod=pod,
            tenant=tenant,
            agent=agent,
            envelope=envelope,
            session_name=session_name,
            socket=socket,
        )


def command_opener(
    r,
    pod: str,
    tenant: str,
    agent: str,
    envelope: dict,
    session_name: str,
    socket: str | None = None,
) -> None:
    stream_id = envelope.get("stream_id", "")
    corr_id = envelope.get("correlation_id")
    source = envelope.get("l2", {}).get("source", "unknown")
    payload = envelope.get("payload", {})

    windows = list_windows(session_name, socket=socket)
    if agent not in windows:
        raise DeadLetter("window_missing")

    text = payload.get("text", "") if isinstance(payload, dict) else str(payload)
    formatted_msg = f"{text}\n"
    mark_delivery_pending(r, pod, tenant, agent, stream_id, correlation_id=corr_id)
    submit_text(session_name, agent, formatted_msg, stream_id=stream_id, socket=socket)


def attachment_opener(
    r,
    pod: str,
    tenant: str,
    agent: str,
    envelope: dict,
    session_name: str,
    socket: str | None = None,
    workdir_root: str | None = None,
) -> None:
    if not isinstance(envelope, dict):
        raise DeadLetter("envelope must be a dict")

    stream_id = envelope.get("stream_id") or envelope.get("l2", {}).get("stream_id")
    if not stream_id or not isinstance(stream_id, str):
        raise DeadLetter("missing stream_id for attachment delivery")

    corr_id = envelope.get("correlation_id") or envelope.get("l2", {}).get("correlation_id")
    source = envelope.get("l2", {}).get("source", "unknown")

    payload = envelope.get("payload")
    validated = validate_attachment_payload(payload)

    # Check recipient tmux window exists
    windows = list_windows(session_name, socket=socket)
    if agent not in windows:
        raise DeadLetter("window_missing")

    # Create <workdir_root>/<recipient>/attachments/<stream_id>/
    root = workdir_root or get_workdir_root()
    target_dir = os.path.join(root, agent, "attachments", stream_id)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as exc:
        raise DeadLetter(f"failed to create attachment directory: {exc}") from exc

    final_path = os.path.join(target_dir, validated.filename)
    temp_path = os.path.join(target_dir, f".tmp.{os.urandom(8).hex()}")

    try:
        with open(temp_path, "wb") as f:
            f.write(validated.data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, final_path)
    except Exception as exc:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        raise DeadLetter(f"attachment file write failed: {exc}") from exc

    # Deferred custody: write pending.verify / delivery.markers AFTER durable file write, BEFORE paste
    mark_delivery_pending(r, pod, tenant, agent, stream_id, correlation_id=corr_id)

    # Paste notice into window
    notice = f"[attachment from {source}] saved to {final_path} ({validated.mime_type}, {len(validated.data)} bytes)\n"
    if validated.caption:
        notice += f"[attachment caption] {validated.caption}\n"

    submit_text(session_name, agent, notice, stream_id=stream_id, socket=socket)


def deliver_tmux(
    r,
    pod: str,
    tenant: str,
    agent: str,
    session_name: str | None = None,
    socket: str | None = None,
    timeout: int = 0,
    blocking: bool = False,
    **kwargs,
) -> None:
    session_name = session_name or os.environ.get("TMUX_SESSION") or tenant
    socket = socket or os.environ.get("TMUX_SOCKET")

    openers = {
        "Message": lambda env: message_opener(
            r=r,
            pod=pod,
            tenant=tenant,
            agent=agent,
            envelope=env,
            session_name=session_name,
            socket=socket,
        ),
        "Command": lambda env: command_opener(
            r=r,
            pod=pod,
            tenant=tenant,
            agent=agent,
            envelope=env,
            session_name=session_name,
            socket=socket,
        ),
        "AddTicket": lambda env: add_ticket(
            r=r,
            pod=pod,
            tenant=tenant,
            agent=agent,
            envelope=env,
        ),
        "Attachment": lambda env: attachment_opener(
            r=r,
            pod=pod,
            tenant=tenant,
            agent=agent,
            envelope=env,
            session_name=session_name,
            socket=socket,
        ),
    }

    receive(
        r,
        pod=pod,
        tenant=tenant,
        agent=agent,
        openers=openers,
        timeout=timeout,
        blocking=blocking,
        module="tmux",
    )


def main(argv: list[str] | None = None) -> None:
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m modules.tmux.port <agent>", file=sys.stderr)
        sys.exit(1)
    agent = args[0]
    pod = os.environ["POD"]
    tenant = os.environ["TENANT"]
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

    r = redis.Redis.from_url(redis_url)
    with delivery_lock(r, pod=pod, tenant=tenant, agent=agent):
        paused_key = prefix(pod, tenant, agent=agent, resource="paused")
        if r.get(paused_key):
            return
        deliver_tmux(r, pod=pod, tenant=tenant, agent=agent)


if __name__ == "__main__":
    main()

