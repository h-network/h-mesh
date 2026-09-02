"""office: the agent-facing CLI -- sending half only.

port.py is the receiving half: a `deliver_office`
entrypoint, shaped like `modules.tmux.port.deliver_tmux`, that calls into
`agentlifecycle` for hire/retire/pause/resume envelopes addressed back to the
fixed lifecycle participant. This file only builds and sends envelopes, or
reads/mutates Redis state the caller owns directly -- it never calls
agentlifecycle itself, the same separation `office/cli.py` and
`control/openers.py` had in the previous system.
"""

import argparse
import base64
import json
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import redis

from core.channels import send
from core.envelope import EnvelopeError, parse
from core.keys import prefix, receive_undeliverable_key, receive_unresolved_key, retired_inbox_key
from core.logging import log_record, record_task_event
from core.registry import is_member, members, port_type
from lib.attachment_schema import ATTACHMENT_MAX_BYTES, MIME_TYPE_REGEX
from lib.board_interaction import BoardError, normalize_ticket, serialize_ticket
from lib.paths import get_workdir_root
from lib.reply_correlation import is_valid_reply_id

from .pricing import calculate_cost, load_pricing

# ⚠ A tenant with a Redis password exports REDIS_URL carrying it. Without one
# this is unchanged, and an agent window still has no REDIS_URL to find.
_REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
ATTACHMENT_MAX_CAPTION_BYTES = 65_536


class OfficeError(ValueError):
    """A user-facing command error."""


def _context():
    source = os.environ.get("AGENT_NAME")
    if not source:
        raise OfficeError("AGENT_NAME environment variable not set")
    pod = os.environ.get("POD", "default")
    tenant = os.environ.get("TENANT", "default")
    return redis.Redis.from_url(_REDIS_URL), pod, tenant, source


def _operation_parser(command: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=f"office {command}", description=description)


# ---------------------------------------------------------------------------
# send, send-file, broadcast, peers, profiles, status
# ---------------------------------------------------------------------------


def _message(
    r, *, pod: str, tenant: str, source: str, destination: str, text: str,
    in_reply_to: str | None = None,
) -> str:
    return send(
        r,
        pod=pod,
        tenant=tenant,
        source=source,
        destination=destination,
        payload={"text": text},
        kind="Message",
        module="office",
        in_reply_to=in_reply_to,
    )


def _send_command(argv: list[str]) -> None:
    parser = _operation_parser("send", "Send a message to one agent.")
    parser.add_argument("-a", "--agent", metavar="AGENT", help="destination agent")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--stdin", action="store_true", help="read message text from stdin")
    sources.add_argument("--file", type=Path, metavar="PATH", help="read message text from a file")
    parser.add_argument(
        "--reply-to",
        metavar="STREAM_ID",
        help=(
            "opt in to exact reply correlation: name the stream_id shown in the "
            "'[message <id> from ...]' line you are answering. Best-effort -- "
            "silently dropped by the recipient's door if it doesn't validate, "
            "same as not passing it at all."
        ),
    )
    parser.add_argument(
        "text",
        nargs="?",
        help="one quoted message argument; use -- before text that begins with a dash",
    )
    args = parser.parse_args(argv)

    if not args.agent:
        raise OfficeError("office send requires -a <agent>")
    if (args.stdin or args.file is not None) and args.text is not None:
        raise OfficeError("office send accepts exactly one of positional text, --stdin, or --file")
    if args.stdin:
        text = sys.stdin.read()
        if text == "":
            raise OfficeError("office send --stdin received no message text")
    elif args.file is not None:
        try:
            text = args.file.read_text(encoding="utf-8")
        except OSError as exc:
            raise OfficeError(f"cannot read message file {str(args.file)!r}: {exc}") from exc
    elif args.text is not None:
        text = args.text
    else:
        raise OfficeError("office send requires message text, --stdin, or --file")

    if args.reply_to is not None and not is_valid_reply_id(args.reply_to):
        raise OfficeError(
            f"--reply-to {args.reply_to!r} is not a 32-character lowercase hex stream_id"
        )

    r, pod, tenant, source = _context()
    if not is_member(r, pod=pod, tenant=tenant, agent=args.agent):
        raise OfficeError(f"unknown destination agent {args.agent!r}")
    stream_id = _message(
        r, pod=pod, tenant=tenant, source=source, destination=args.agent, text=text,
        in_reply_to=args.reply_to,
    )
    print(f"sent to {args.agent}: {len(text.encode('utf-8'))} bytes ({stream_id})")


def _validate_filename(filename: str) -> None:
    if not filename:
        raise OfficeError("attachment filename cannot be empty")
    try:
        encoded = filename.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OfficeError(f"attachment filename {filename!r} is not valid UTF-8: {exc}") from exc
    if len(encoded) > 255:
        raise OfficeError(
            f"attachment filename exceeds maximum length of 255 bytes ({len(encoded)} bytes)"
        )
    if filename in (".", ".."):
        raise OfficeError(f"attachment filename cannot be {filename!r}")
    for char in filename:
        cp = ord(char)
        if char in ("/", "\\") or cp < 32 or cp == 0x7F:
            raise OfficeError(
                f"attachment filename {filename!r} contains forbidden character {char!r}"
            )


def _validate_mime_type(mime_type: str) -> None:
    try:
        raw = mime_type.encode("ascii")
    except UnicodeEncodeError as exc:
        raise OfficeError(f"invalid mime-type {mime_type!r}: must be ASCII") from exc
    if not raw or len(raw) > 255:
        raise OfficeError(f"invalid mime-type {mime_type!r}: length must be 1-255 bytes")
    if not MIME_TYPE_REGEX.match(mime_type):
        raise OfficeError(
            f"invalid mime-type {mime_type!r}: must match type/subtype format"
        )


def _send_file_command(argv: list[str]) -> None:
    parser = _operation_parser("send-file", "Send a file attachment to one agent.")
    parser.add_argument("-a", "--agent", metavar="AGENT", help="destination agent")
    parser.add_argument("path", type=Path, metavar="PATH", help="path to regular file")
    parser.add_argument("--caption", metavar="TEXT", help="optional caption")
    parser.add_argument(
        "--mime-type", metavar="TYPE", help="MIME type (guessed from extension if omitted)"
    )
    args = parser.parse_args(argv)

    if not args.agent:
        raise OfficeError("office send-file requires -a <agent>")
    if args.agent == "all":
        raise OfficeError(
            "office send-file does not support broadcast ('all'); attachments are unicast only"
        )

    path = args.path
    if not path.exists():
        raise OfficeError(f"attachment file does not exist: {str(path)!r}")
    if not path.is_file():
        raise OfficeError(f"attachment path {str(path)!r} is not a regular file")

    try:
        file_size = path.stat().st_size
    except OSError as exc:
        raise OfficeError(f"cannot stat attachment file {str(path)!r}: {exc}") from exc

    if file_size > ATTACHMENT_MAX_BYTES:
        raise OfficeError(
            f"attachment file size ({file_size} bytes) exceeds maximum allowed size of {ATTACHMENT_MAX_BYTES} bytes"
        )

    filename = path.name
    _validate_filename(filename)

    if args.mime_type is not None:
        _validate_mime_type(args.mime_type)
        mime_type = args.mime_type
    else:
        guessed, _ = mimetypes.guess_type(filename)
        if (
            guessed
            and MIME_TYPE_REGEX.match(guessed)
            and len(guessed.encode("ascii", errors="ignore")) <= 255
        ):
            mime_type = guessed
        else:
            mime_type = "application/octet-stream"

    if args.caption is not None:
        try:
            caption_bytes = args.caption.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise OfficeError(f"attachment caption is not valid UTF-8: {exc}") from exc
        if len(caption_bytes) > ATTACHMENT_MAX_CAPTION_BYTES:
            raise OfficeError(
                f"attachment caption exceeds maximum length of {ATTACHMENT_MAX_CAPTION_BYTES} bytes ({len(caption_bytes)} bytes)"
            )

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise OfficeError(f"cannot read attachment file {str(path)!r}: {exc}") from exc

    if len(raw_bytes) > ATTACHMENT_MAX_BYTES:
        raise OfficeError(
            f"attachment file size ({len(raw_bytes)} bytes) exceeds maximum allowed size of {ATTACHMENT_MAX_BYTES} bytes"
        )

    content_base64 = base64.b64encode(raw_bytes).decode("ascii")

    r, pod, tenant, source = _context()
    if not is_member(r, pod=pod, tenant=tenant, agent=args.agent):
        raise OfficeError(f"unknown destination agent {args.agent!r}")

    payload = {
        "filename": filename,
        "mime_type": mime_type,
        "content_base64": content_base64,
    }
    if args.caption is not None:
        payload["caption"] = args.caption

    stream_id = send(
        r,
        pod=pod,
        tenant=tenant,
        source=source,
        destination=args.agent,
        payload=payload,
        kind="Attachment",
        module="office",
    )
    print(f"sent to {args.agent}: {len(raw_bytes)} bytes ({stream_id})")


def _broadcast_command(argv: list[str]) -> None:
    parser = _operation_parser("broadcast", "Send a message to every peer agent.")
    parser.add_argument("text", nargs=argparse.REMAINDER, help="message text")
    if argv in (["-h"], ["--help"]):
        parser.parse_args(argv)
    if not argv:
        raise OfficeError("office broadcast requires message text")

    r, pod, tenant, source = _context()
    recipients = sorted(
        agent
        for agent in members(r, pod=pod, tenant=tenant)
        if agent != source and port_type(r, pod=pod, tenant=tenant, agent=agent) == "tmux"
    )
    for destination in recipients:
        print(_message(r, pod=pod, tenant=tenant, source=source, destination=destination, text=" ".join(argv)))


def _peers_command(argv: list[str]) -> None:
    parser = _operation_parser("peers", "List peer agents in this office.")
    parser.add_argument("-v", "--verbose", action="store_true", help="show framework, profile, and current task")
    parser.add_argument(
        "-i",
        "--interfaces",
        action="store_true",
        help="also list api/control registry members, labeled apart from tmux colleagues",
    )
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    raw_lead = r.get(prefix(pod, tenant, resource="lead"))
    lead = raw_lead.decode() if isinstance(raw_lead, bytes) else str(raw_lead) if raw_lead else None
    all_agents = sorted(members(r, pod=pod, tenant=tenant))
    if lead and lead not in all_agents:
        print(
            f"office: warning: configured lead {lead!r} is not an enrolled agent",
            file=sys.stderr,
        )
    peer_names = [
        agent
        for agent in all_agents
        if agent != source and port_type(r, pod=pod, tenant=tenant, agent=agent) == "tmux"
    ]
    formatted = [f"{agent} (lead)" if agent == lead else agent for agent in peer_names]

    if not args.verbose and not args.interfaces:
        print(", ".join(formatted))
        return

    if args.verbose:
        for agent, display_name in zip(peer_names, formatted):
            framework = _text(r.get(prefix(pod, tenant, agent=agent, resource="launch"))) or "unknown"
            profile = _text(r.get(prefix(pod, tenant, agent=agent, resource="profile")))
            raw_ticket = next(
                iter(r.lrange(prefix(pod, tenant, agent=agent, resource="tasks.doing"), 0, 0)),
                None,
            )
            fields = [f"framework={framework}"]
            if profile:
                fields.append(f"profile={profile}")
            if raw_ticket is not None:
                fields.append(f"task={json.dumps(normalize_ticket(raw_ticket, state='doing')['title'])}")
            print(f"{display_name}: {', '.join(fields)}")
    else:
        print(", ".join(formatted))

    if args.interfaces:
        labeled = sorted(
            f"{agent} ({port_type(r, pod=pod, tenant=tenant, agent=agent)})"
            for agent in all_agents
            if agent != source and port_type(r, pod=pod, tenant=tenant, agent=agent) in ("api", "office")
        )
        print(f"interfaces (api/office -- recognized, not tmux colleagues): {', '.join(labeled) or '(none)'}")


def _timestamp(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _age(value, *, now: datetime) -> str | None:
    timestamp = _timestamp(value)
    if timestamp is None:
        return None
    seconds = max(0, int((now - timestamp).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 60 * 60:
        return f"{seconds // 60}m"
    if seconds < 24 * 60 * 60:
        return f"{seconds // (60 * 60)}h"
    return f"{seconds // (24 * 60 * 60)}d"


def _status_row(r, *, pod: str, tenant: str, agent: str, now: datetime) -> str:
    presence = r.hgetall(prefix(pod, tenant, agent=agent, resource="presence")) or {}
    decoded_presence = {_text(field): _text(value) for field, value in presence.items()}

    # The watchdog owns this key. Status only observes it, and absence is the
    # normal case until a watchdog module exists in h-mesh.
    #
    # ⚠ It is a HASH -- {since, stream_id} -- and this must not crash if it is
    # anything else. A key it cannot make sense of means "not blocked".
    try:
        blocked = r.hgetall(prefix(pod, tenant, agent=agent, resource="blocked")) or None
    except Exception:
        blocked = None
    presence_state = decoded_presence.get("state") or "unknown"
    state = "blocked" if blocked is not None else presence_state

    doing_key = prefix(pod, tenant, agent=agent, resource="tasks.doing")
    raw_ticket = next(iter(r.lrange(doing_key, 0, 0)), None)
    if raw_ticket is None:
        task = "—"
    else:
        ticket = normalize_ticket(raw_ticket, state="doing")
        opened = _age(ticket.get("started_ts"), now=now)
        task = f'"{ticket["title"]}"' + (f" {opened}" if opened else "")

    if presence_state == "unknown":
        activity = "no activity feed"
    else:
        last = _age(decoded_presence.get("last_activity"), now=now)
        activity = f"last activity {last} ago" if last else "no activity yet"
    return f"  {agent:<12}{state:<10}{task:<35}{activity}"


def _status_command(argv: list[str]) -> None:
    parser = _operation_parser("status", "Show agent presence and open work.")
    parser.add_argument("agent", nargs="?", help="one tmux agent (default: all)")
    args = parser.parse_args(argv)
    r, pod, tenant, _ = _context()
    tmux_agents = sorted(
        agent
        for agent in members(r, pod=pod, tenant=tenant)
        if port_type(r, pod=pod, tenant=tenant, agent=agent) == "tmux"
    )
    if args.agent is not None:
        if args.agent not in tmux_agents:
            raise OfficeError(f"unknown tmux agent {args.agent!r}")
        tmux_agents = [args.agent]
    now = datetime.now(timezone.utc)
    for agent in tmux_agents:
        print(_status_row(r, pod=pod, tenant=tenant, agent=agent, now=now))


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode() if isinstance(value, bytes) else str(value)


def _fields(raw: dict) -> dict[str, str]:
    return {_text(key): _text(value) for key, value in raw.items()}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# lifecycle commands
#
# ⚠ Named `_lifecycle_command`, not `_control_command`. "Control" now names a
# different layer -- the fixed participant these envelopes reach is a
# `agentlifecycle` caller, not a module called "control" -- so a function
# here with that name would collide with vocabulary that means something
# else, the exact confusion this file's own `send`/`core.channels.send`
# distinction already has to call out separately.
#
# ⚠ No --export/--import flags. agentlifecycle.start_agent accepts both in
# the payload, but nothing here surfaces them client-side -- skepticism from
# the operator (an unused flag is not worth adding) stands until there's a
# concrete need.
# ---------------------------------------------------------------------------


def _wait_seconds(raw: str) -> float:
    """argparse type= for --wait -- rejects anything that would break the
    bounded-wait contract, before parse_args() returns and long before any
    send() call.

    ⚠ Bare `float()` accepts "nan"/"inf"/"-inf" without complaint. NaN is
    the dangerous one: every comparison against NaN is False, so `remaining
    <= 0` in _await_hire_confirmation's poll loop never becomes true and
    `min(POLL_INTERVAL, nan)` returns POLL_INTERVAL unchanged -- the loop
    polls forever and the exit-2 "unknown" outcome the whole three-state
    design exists to guarantee never fires. +inf is a real, finite-looking
    value that is unbounded by definition, contradicting "wait up to
    SECONDS". A negative value would make the deadline already-past,
    returning "unknown" immediately with a nonsensical negative duration in
    the message. All four are rejected here, not handled downstream.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid SECONDS value: {raw!r}") from exc
    if math.isnan(value) or math.isinf(value) or value < 0:
        raise argparse.ArgumentTypeError(
            f"--wait SECONDS must be a finite, non-negative number, got {raw!r}"
        )
    return value


def _lifecycle_command(command: str, argv: list[str]) -> None:
    descriptions = {
        "hire": "Enrol a new agent.",
        "letGo": "Retire an agent.",
        "pause": "Pause an agent's CLI while preserving its state.",
        "resume": "Resume an agent's CLI and queued inbox.",
    }
    kinds = {
        "hire": "StartAgent",
        "letGo": "StopAgent",
        "pause": "PauseAgent",
        "resume": "ResumeAgent",
    }
    parser = _operation_parser(command, descriptions[command])
    parser.add_argument("agent", help="target agent")
    if command == "hire":
        # ⚠ `choices` rather than a free string. An unknown value would be
        # accepted, stored, and fail inside the window at `h-agent <typo>` --
        # indistinguishable from a login prompt. A typo should fail here.
        parser.add_argument("--cli", default="claude", choices=("claude", "codex", "agy"),
                            help="CLI to start (default: claude)")
        # ⚠ No client-side check against configured accounts here -- see the
        # `available_profiles()` note further down, near where that check
        # would run. The flag and its payload passthrough are unaffected.
        parser.add_argument("--profile", metavar="ACCOUNT",
                            help="account whose config dir and credential this agent uses "
                                 "(default: the tenant's default account)")
        parser.add_argument("--provider", metavar="NAME",
                            help="provider used to start this agent")
        parser.add_argument(
            "--lead", action="store_true",
            help="make this agent the office lead (transfers leadership)",
        )
        mode_group = parser.add_mutually_exclusive_group()
        mode_group.add_argument("--resume", action="store_true", default=None,
                                help="resume prior session history (explicit opt-in)")
        mode_group.add_argument("--fresh", action="store_true", default=None,
                                help="start a clean session ignoring prior history (default)")
        # ⚠ These are h-agent's own permission knobs (base image), not
        # h-mesh's. Omitted means h-agent decides; neither flag changes its
        # default behavior.
        permission_group = parser.add_mutually_exclusive_group()
        permission_group.add_argument(
            "--skip-permissions", action="store_true", default=None,
            help="bypass CLI approval prompts (default: on)",
        )
        permission_group.add_argument(
            "--no-skip-permissions", action="store_true", default=None,
            help="keep real CLI approval prompts instead of bypassing them",
        )
        parser.add_argument(
            "--claude-tools", metavar="LIST", default=None,
            help="claude's --tools list, space-separated (default: Bash Read Write Edit "
                 "Glob Grep); '' means unrestricted. claude only, ignored by codex/agy",
        )
        # ⚠ Opt-in, not the default -- a StartAgent envelope is fire-and-forget
        # by design (see core.channels.send's own docstring), and a caller that
        # wants that stays fire-and-forget. This exists because printing the
        # word "hired" off nothing but this command's exit code is ADMITTED
        # (the envelope was durably enqueued) reported as CREATED (the agent
        # actually exists) -- exactly the shape of a real incident where an
        # operator was told an agent existed when it did not.
        #
        # ⚠ Cannot currently report success, only the ABSENCE of a proven
        # failure -- read this before scripting against exit 0. "hire" is
        # really two operations sharing one name: identity creation (a
        # genuinely new agent, where CREATED could in principle be
        # observed once an attributable signal exists) and idempotent
        # re-hire/reconfiguration of an agent that already exists (where
        # bare registry membership says NOTHING about whether THIS
        # request did anything -- no amount of polling membership will
        # ever be conclusive for it, even in principle, since a
        # different, unrelated request for the same agent name is
        # observationally identical). Nothing in this codebase today
        # writes a signal tying a *successful* StartAgent back to the
        # specific stream_id that caused it, for either case -- so
        # neither can be safely confirmed, only proven failed or left
        # unknown. See ticket ff53e7e9 for the attributable-completion
        # signal (success AND failure, keyed by stream_id, covering both
        # operations) this would need to safely report a real success.
        parser.add_argument(
            "--wait", nargs="?", const=30.0, type=_wait_seconds, default=None, metavar="SECONDS",
            help="wait up to SECONDS (default 30) and report what can be proven about "
                 "this request, instead of returning as soon as it's merely accepted. "
                 "Two outcomes today, not three: exit 1 = failed (a real, stream_id-"
                 "matched rejection, seen in the destination's dead-letter list); "
                 "exit 2 = unknown -- no proof of failure within SECONDS. Exit 0 is "
                 "NOT currently reachable: bare registry membership cannot be "
                 "attributed to this specific request under concurrency (a different "
                 "request for the same agent name could be the one that actually "
                 "registered it, or already had), so this never claims success -- "
                 "only the absence of a proven failure. A timeout is not a failure: "
                 "a stranded request can still complete later once switch recovery "
                 "re-kicks it (see switch-agent's drain/recovery fix).",
        )
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    # ⚠ No client-side --profile validation here. `available_profiles()` (an
    # accounts-set read) has no h-mesh equivalent yet -- `--profile` still
    # goes straight into the payload; a mistyped account name surfaces
    # wherever the far side eventually validates it instead of here.
    payload = {"agent": args.agent}
    if command == "hire":
        payload["cli"] = args.cli
        if args.profile:
            payload["profile"] = args.profile
        if args.provider:
            payload["provider"] = args.provider
        if args.lead:
            payload["lead"] = True
        # A hire envelope is consumed asynchronously, so it must never leave an
        # interactive choice waiting in the new pane. h-agent's bare --resume
        # can open a picker when this name has multiple prior sessions. Make a
        # clean launch the deterministic default; restoring history remains an
        # explicit, easy opt-in.
        payload["resume"] = bool(args.resume)
        if args.no_skip_permissions:
            payload["skip_permissions"] = False
        elif args.skip_permissions:
            payload["skip_permissions"] = True
        if args.claude_tools is not None:
            payload["claude_tools"] = args.claude_tools

    stream_id = send(
        r,
        pod=pod,
        tenant=tenant,
        source=source,
        destination="host",
        payload=payload,
        kind=kinds[command],
        module="office",
    )
    if command == "hire" and args.wait is not None:
        outcome, detail = _await_hire_confirmation(
            r, pod=pod, tenant=tenant, stream_id=stream_id, timeout=args.wait,
        )
        if outcome == "failed":
            print(f"failed: {args.agent} was not registered -- {detail}", file=sys.stderr)
            raise SystemExit(1)
        print(
            f"unknown: no proof of failure for {args.agent} within {args.wait:.0f}s -- "
            "this does NOT mean it failed, it may well have succeeded; there is "
            "currently no way to prove success for this request, only to disprove "
            "it. Check 'office status' or the registry directly if you want to "
            "look for yourself.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(stream_id)


_HIRE_CONFIRMATION_POLL_INTERVAL_S = 0.5


def _await_hire_confirmation(
    r, *, pod: str, tenant: str, stream_id: str, timeout: float,
) -> tuple[str, str | None]:
    """Poll for the only outcome this can currently prove: a real,
    stream_id-matched rejection. Deliberately does NOT check registry
    membership as evidence of success, in any case -- not even a
    genuinely-first-ever hire of a brand-new agent name.

    An earlier version of this function DID check bare registry
    port_type()=="tmux" as confirmation once the target had never been
    seen before, reasoning that "absent, then tmux" was unambiguous since
    nothing else could cause that transition. Reviewer FAILED that
    version too: it is not unambiguous under concurrency. A DIFFERENT,
    unrelated StartAgent for the same agent name -- already queued, or
    racing in around the same time -- can register the agent while THIS
    request is independently rejected, with its dead-letter simply not
    landed yet by the time of an early poll. Both worlds -- "this request
    succeeded" and "a different request succeeded while this one failed"
    -- look identical from here: no dead-letter match (yet) and
    port_type()=="tmux". An earlier attempt to fix this by requiring the
    agent to have been absent at a pre-send baseline (this function's own
    previous revision) does not close it either: another same-name hire
    can already be sitting in ingress, unprocessed, when that baseline is
    read -- the baseline sees absence, this request is enqueued behind
    the other one, the other one registers the agent, this one is
    rejected, and the poll still observes tmux before the rejection lands.

    "hire" is really two operations sharing one name and this one --wait
    flag: identity creation (a genuinely new agent -- CREATED could in
    principle be observed once an attributable signal exists) and
    idempotent re-hire/reconfiguration of an agent that already exists
    (where bare membership can never be conclusive, even in principle,
    since a different request is observationally identical). Neither
    currently has a success signal keyed by stream_id anywhere in this
    codebase, so neither can be safely confirmed -- only proven failed,
    or left honestly unknown. See ticket ff53e7e9 for the real fix shape:
    stream-id-attributable lifecycle completion, covering explicit
    idempotent-success and reconfiguration-success signals, not just a
    reordered or better-gated registry check.

    ⚠ Read-only against the dead-letter list (LRANGE, never pop) -- popping
    it here would consume evidence a human or another tool still needs to
    see, for a check this command has no ownership over.

    ⚠ Only the destination ("host") ever dead-letters a StartAgent it
    itself rejected -- the tmux-source-only feedback path
    (_notify_dead_letter_sender in core.channels) never reaches a caller
    like this one (source is usually "host" itself, not a tmux agent), so
    this reads the recipient's dead list directly instead of waiting for a
    reply message that would never arrive.
    """
    dead_key = prefix(pod, tenant, agent="host", resource="dead")
    deadline = time.monotonic() + timeout
    while True:
        for raw in r.lrange(dead_key, 0, -1):
            try:
                envelope = parse(raw)
            except EnvelopeError:
                continue
            if envelope.get("stream_id") == stream_id:
                return "failed", "rejected by host -- see host's activity log for the reason"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "unknown", None
        time.sleep(min(_HIRE_CONFIRMATION_POLL_INTERVAL_S, remaining))


# ---------------------------------------------------------------------------
# add, list, take, done, cancel, hold, delete
#
# Ticket normalization/serialization comes from lib.board_interaction, not a
# local copy -- add_ticket (the AddTicket receiving side) and every command
# below that reads or writes a board entry now share one shape, instead of
# each maintaining its own.
# ---------------------------------------------------------------------------


def _task_keys(pod: str, tenant: str, agent: str) -> dict[str, str]:
    return {
        state: prefix(pod, tenant, agent=agent, resource=f"tasks.{state}")
        for state in ("todo", "doing", "hold", "done", "invalid")
    }


_PREFLIGHTED_TRANSITION = """
-- office preflighted task transition v2
local source_type = redis.call('TYPE', KEYS[1]).ok
if source_type == 'none' then
    return {0, 'changed'}
end
if source_type ~= 'list' then
    return {0, 'source_type'}
end
local destination_type = redis.call('TYPE', KEYS[2]).ok
if destination_type ~= 'none' and destination_type ~= 'list' then
    return {0, 'destination_type'}
end
if ARGV[3] == '1' and redis.call('LLEN', KEYS[2]) > 0 then
    return {0, 'busy'}
end
if redis.call('LREM', KEYS[1], 1, ARGV[1]) == 0 then
    return {0, 'changed'}
end
redis.call('RPUSH', KEYS[2], ARGV[2])
return {1, 'ok'}
"""

_ATOMIC_REWRITE = """
-- office atomic task rewrite v1
local entries = redis.call('LRANGE', KEYS[1], 0, -1)
for index, value in ipairs(entries) do
    if value == ARGV[1] then
        redis.call('LSET', KEYS[1], index - 1, ARGV[2])
        return 1
    end
end
return 0
"""

def _quarantine_invalid(r, *, source_key: str, invalid_key: str, raw) -> None:
    """Preserve an unreadable entry through the preflighted board transition."""
    _transition_selected(
        r,
        source_key=source_key,
        destination_key=invalid_key,
        raw=raw,
        replacement=raw,
    )


def _take_selected(r, *, source_key: str, doing_key: str, raw, ticket: dict) -> None:
    _transition_selected(
        r,
        source_key=source_key,
        destination_key=doing_key,
        raw=raw,
        replacement=serialize_ticket(ticket),
        require_destination_empty=True,
    )


def _transition_selected(
    r,
    *,
    source_key: str,
    destination_key: str,
    raw,
    replacement,
    require_destination_empty: bool = False,
) -> None:
    """Move one entry after preflighting every key-type command error.

    Source and destination types are checked before the first mutation. Take's
    destination-empty guard also runs before LREM. Once LREM begins, RPUSH has
    a preflighted list-or-absent destination and fixed valid arguments.
    """
    result = r.eval(
        _PREFLIGHTED_TRANSITION,
        2,
        source_key,
        destination_key,
        raw,
        replacement,
        "1" if require_destination_empty else "0",
    )
    code = int(result[0])
    reason = result[1].decode() if isinstance(result[1], bytes) else result[1]
    if code:
        return
    if reason == "busy":
        raise OfficeError("you already have one open task")
    if reason == "source_type":
        raise OfficeError("source task list has wrong Redis type; no changes made")
    if reason == "destination_type":
        raise OfficeError("destination task list has wrong Redis type; no changes made")
    raise OfficeError("task changed while the command was running; try again")


def _rewrite_selected(r, *, key: str, raw, replacement) -> None:
    """Rewrite one exact entry without a partial multi-command state.

    LRANGE preflights list type and finds the index before the only mutation,
    LSET. Nothing mutating follows LSET, so a later command cannot expose a
    partially applied rewrite.
    """
    if not r.eval(_ATOMIC_REWRITE, 1, key, raw, replacement):
        raise OfficeError("task changed while the command was running; try again")


def _replace_top_level_title(raw, title: str):
    """Replace exactly one top-level title token, preserving every other byte."""
    was_bytes = isinstance(raw, bytes)
    text = raw.decode() if was_bytes else raw
    decoder = json.JSONDecoder()

    def skip_space(position: int) -> int:
        while position < len(text) and text[position].isspace():
            position += 1
        return position

    position = skip_space(0)
    if position >= len(text) or text[position] != "{":
        raise OfficeError("stored ticket is not a JSON object")
    position += 1
    title_spans = []
    while True:
        position = skip_space(position)
        if position < len(text) and text[position] == "}":
            position += 1
            break
        try:
            key, position = decoder.raw_decode(text, position)
        except json.JSONDecodeError as exc:
            raise OfficeError("cannot safely locate stored ticket title") from exc
        if not isinstance(key, str):
            raise OfficeError("cannot safely locate stored ticket title")
        position = skip_space(position)
        if position >= len(text) or text[position] != ":":
            raise OfficeError("cannot safely locate stored ticket title")
        value_start = skip_space(position + 1)
        try:
            value, value_end = decoder.raw_decode(text, value_start)
        except json.JSONDecodeError as exc:
            raise OfficeError("cannot safely locate stored ticket title") from exc
        if key == "title":
            title_spans.append((value_start, value_end, value))
        position = skip_space(value_end)
        if position < len(text) and text[position] == ",":
            position += 1
            continue
        if position < len(text) and text[position] == "}":
            position += 1
            break
        raise OfficeError("cannot safely locate stored ticket title")

    if skip_space(position) != len(text) or len(title_spans) != 1:
        raise OfficeError("stored ticket must contain exactly one top-level title")
    value_start, value_end, old_title = title_spans[0]
    if not isinstance(old_title, str):
        raise OfficeError("stored ticket title is not text")
    replacement_token = json.dumps(title, ensure_ascii=False)
    rewritten = text[:value_start] + replacement_token + text[value_end:]

    expected = json.loads(text)
    expected["title"] = title
    if json.loads(rewritten) != expected:
        # Refusal is the safety behavior. Never fall back to re-serializing a
        # document whose one-field edit could not be proved.
        raise OfficeError("could not verify title-only ticket rewrite")
    return rewritten.encode() if was_bytes else rewritten


def _entries(r, keys: dict[str, str], states: Sequence[str]):
    for state in states:
        for raw in r.lrange(keys[state], 0, -1):
            yield state, raw, normalize_ticket(raw, state=state)


def _select(r, keys: dict[str, str], states: Sequence[str], reference: str | None):
    try:
        entries = list(_entries(r, keys, states))
    except BoardError as exc:
        raise OfficeError(str(exc)) from exc
    if reference is None:
        if not entries:
            raise OfficeError("you have no open task")
        if len(entries) != 1:
            raise OfficeError("more than one task matches; specify an id")
        return entries[0]
    matches = [entry for entry in entries if entry[2]["id"].startswith(reference)]
    if not matches:
        raise OfficeError(f"no task matches id {reference!r}")
    if len(matches) != 1:
        raise OfficeError(f"task id {reference!r} is ambiguous")
    return matches[0]


def _remove(r, key: str, raw) -> None:
    if not r.lrem(key, 1, raw):
        raise OfficeError("task changed while the command was running; try again")


def _log_task(event: str, *, agent: str, ticket: dict) -> None:
    log_record(
        "office",
        event,
        destination=agent,
        task_id=ticket["id"],
        outcome=ticket.get("outcome"),
    )


# One timestamp field per state, naming "age" consistently as "time in the
# state this line is printed under" rather than mixing in a different notion
# (e.g. done's total created->done duration) that would need its own label to
# not be misread as the others.
_AGE_FIELD = {"todo": "created_ts", "doing": "started_ts", "hold": "held_ts", "done": "done_ts"}


def _ticket_age(ticket: dict, *, state: str, now: datetime) -> str | None:
    value = ticket.get(_AGE_FIELD[state])
    if not value and state == "hold":
        # A ticket held before held_ts existed (or by an older client) has
        # nothing there yet; created_ts is the next best "how long has this
        # been sitting" signal rather than showing nothing at all.
        value = ticket.get("created_ts")
    return _age(value, now=now)


def _ticket_line(ticket: dict, *, state: str, now: datetime) -> str:
    line = f"{ticket['id'][:8]}  {ticket['title']}"
    priority = ticket.get("priority")
    if priority:
        line += f"  p:{priority}"
    related = ticket.get("related")
    if related:
        line += f"  rel:{','.join(item[:8] for item in related)}"
    age = _ticket_age(ticket, state=state, now=now)
    if age:
        line += f"  age:{age}"
    if state == "hold" and ticket.get("hold_reason"):
        line += f"  reason:{' '.join(ticket['hold_reason'].split())}"
    if state == "done" and ticket.get("outcome"):
        line += f"  outcome:{ticket['outcome']}"
    return line


def _list_one(r, *, pod: str, tenant: str, agent: str, heading: bool, now: datetime) -> None:
    if heading:
        print(f"{agent}:")
    keys = _task_keys(pod, tenant, agent)
    indent = "  " if heading else ""
    for state in ("todo", "doing", "hold", "done"):
        print(f"{indent}{state}:")
        tickets = [normalize_ticket(raw, state=state) for raw in r.lrange(keys[state], 0, -1)]
        if tickets:
            for ticket in tickets:
                print(f"{indent}  {_ticket_line(ticket, state=state, now=now)}")
        else:
            print(f"{indent}  (empty)")
    invalid = r.lrange(keys["invalid"], 0, -1)
    print(f"{indent}invalid:")
    if invalid:
        for raw in invalid:
            size = len(raw if isinstance(raw, bytes) else str(raw).encode("utf-8"))
            print(f"{indent}  malformed ticket preserved ({size} bytes)")
    else:
        print(f"{indent}  (empty)")


def _list_command(argv: list[str]) -> None:
    parser = _operation_parser("list", "Show task-board titles.")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("-a", "--agent", metavar="AGENT")
    target.add_argument("--all", action="store_true", help="show every agent board")
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    if args.all:
        agents = sorted(agent for agent in members(r, pod=pod, tenant=tenant) if port_type(r, pod=pod, tenant=tenant, agent=agent) == "tmux")
    else:
        agents = [args.agent or source]
    now = datetime.now(timezone.utc)
    for index, agent in enumerate(agents):
        if index:
            print()
        _list_one(r, pod=pod, tenant=tenant, agent=agent, heading=args.all, now=now)


def _unresolved_command(argv: list[str]) -> None:
    parser = _operation_parser(
        "unresolved",
        "Read unresolved delivery outcomes without changing or retrying them.",
    )
    parser.add_argument("-a", "--agent", metavar="AGENT")
    args = parser.parse_args(argv)
    r, pod, tenant, _ = _context()
    for stored in r.lrange(receive_unresolved_key(pod, tenant), 0, -1):
        try:
            record = json.loads(_text(stored))
            raw = (
                bytes.fromhex(record["envelope"])
                if record.get("encoding") == "hex" else record["envelope"]
            )
            envelope = parse(raw)
            agent = record["agent"]
            if args.agent and agent != args.agent:
                continue
            print(json.dumps({
                "agent": agent,
                "stream_id": envelope["stream_id"],
                "kind": envelope["kind"],
                "source": envelope["l2"]["source"],
                "reason": record.get("reason", "open outcome unknown"),
            }, separators=(",", ":")))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, EnvelopeError):
            print("unparseable unresolved custody record", file=sys.stderr)


def _undeliverable_command(argv: list[str]) -> None:
    parser = _operation_parser(
        "undeliverable",
        "Read messages whose destination retired before opening them.",
    )
    parser.add_argument("-a", "--agent", metavar="AGENT")
    args = parser.parse_args(argv)
    r, pod, tenant, _ = _context()
    for stored in r.lrange(receive_undeliverable_key(pod, tenant), 0, -1):
        try:
            record = json.loads(_text(stored))
            raw = (
                bytes.fromhex(record["envelope"])
                if record.get("encoding") == "hex" else record["envelope"]
            )
            envelope = parse(raw)
            agent = record["agent"]
            if args.agent and agent != args.agent:
                continue
            print(json.dumps({
                "agent": agent,
                "stream_id": envelope["stream_id"],
                "kind": envelope["kind"],
                "source": envelope["l2"]["source"],
                "reason": record.get("reason", "destination retired before opening"),
            }, separators=(",", ":")))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, EnvelopeError):
            print("unparseable undeliverable custody record", file=sys.stderr)


def _decoded_component(raw: bytes) -> dict:
    """One field or value, decoded independently of its pair partner: a
    duplicate-field or hostile-byte scheme can put a cleanly-decodable
    name next to an undecodable value (or vice versa), and collapsing
    them as a matched pair would throw away the half that DID decode.
    Each component carries its own encoding tag rather than being folded
    into a shared key -- see _retired_inbox_command's docstring for why a
    dict was wrong for this."""
    try:
        return {"value": raw.decode(), "encoding": "utf8"}
    except UnicodeDecodeError:
        return {"value": raw.hex(), "encoding": "hex"}


def _retired_inbox_command(argv: list[str]) -> None:
    """Read api-type agents' inbox content conserved at retirement.

    Same shape as unresolved/undeliverable: read-only, no replay/delete/
    expiry verb -- a durable sink nobody can inspect is hiding, not
    conserving, so this exists in the same commit as the write path
    that fills it. Records here are not envelopes (deliver_api's inbox
    entries aren't wire frames), so this decodes each hex field/value
    pair directly rather than going through core.envelope.parse().

    ⚠ `fields` PRINTS AS AN ORDERED LIST OF PAIR RECORDS, NEVER A DICT.
    A first version collapsed it to a dict for readability and broke the
    one property the stored record exists to preserve: a duplicate field
    name silently kept only its last value, and two DIFFERENT raw field
    names could even collide onto the same displayed key under a mixed
    text/hex fallback scheme (a UTF-8 field literally named "ff" and a
    binary field whose hex form is "ff" both became key "ff"). The
    durable record already preserves exact order and duplicates; a
    reader that cannot show the same is not actually inspectable, it
    just looks like it is. Each field and value is decoded
    INDEPENDENTLY (not as a matched pair) via _decoded_component, so an
    undecodable value never hides an otherwise-readable field name.
    """
    parser = _operation_parser(
        "retired-inbox",
        "Read api agents' inbox content still unread when their destination retired.",
    )
    parser.add_argument("-a", "--agent", metavar="AGENT")
    args = parser.parse_args(argv)
    r, pod, tenant, _ = _context()
    for stored in r.lrange(retired_inbox_key(pod, tenant), 0, -1):
        try:
            record = json.loads(_text(stored))
            agent = record["agent"]
            if args.agent and agent != args.agent:
                continue
            fields = [
                {
                    "field": _decoded_component(bytes.fromhex(field_hex)),
                    "value": _decoded_component(bytes.fromhex(value_hex)),
                }
                for field_hex, value_hex in record["fields"]
            ]
            print(json.dumps({
                "agent": agent,
                "entry_id": record["entry_id"],
                "reason": record.get("reason", "destination retired with unread inbox content"),
                "fields": fields,
            }, separators=(",", ":")))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            print("unparseable retired-inbox custody record", file=sys.stderr)


def _take_command(argv: list[str]) -> None:
    parser = _operation_parser("take", "Move a todo or held task into doing.")
    parser.add_argument("id", nargs="?", help="ticket id or unique prefix")
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    # Fast-path the common refusal for a useful error even when todo is empty.
    # _take_selected's Lua check remains the authoritative race-free guard.
    if r.llen(keys["doing"]):
        raise OfficeError("you already have one open task")
    if args.id is None:
        raw = r.lindex(keys["todo"], 0)
        if raw is None:
            raise OfficeError("your todo is empty")
        try:
            ticket = normalize_ticket(raw, state="todo")
        except BoardError as exc:
            _quarantine_invalid(
                r, source_key=keys["todo"], invalid_key=keys["invalid"], raw=raw,
            )
            raise OfficeError(str(exc)) from exc
        state = "todo"
    else:
        entries = []
        for candidate_state in ("todo", "hold"):
            for candidate_raw in r.lrange(keys[candidate_state], 0, -1):
                try:
                    candidate = normalize_ticket(candidate_raw, state=candidate_state)
                except BoardError:
                    _quarantine_invalid(
                        r, source_key=keys[candidate_state],
                        invalid_key=keys["invalid"], raw=candidate_raw,
                    )
                    continue
                if candidate["id"].startswith(args.id):
                    entries.append((candidate_state, candidate_raw, candidate))
        if not entries:
            raise OfficeError(f"no task matches id {args.id!r}")
        if len(entries) != 1:
            raise OfficeError(f"task id {args.id!r} is ambiguous")
        state, raw, ticket = entries[0]
    ticket["status"] = "doing"
    ticket["started_ts"] = _now()
    ticket["done_ts"] = None
    _take_selected(
        r, source_key=keys[state], doing_key=keys["doing"], raw=raw, ticket=ticket,
    )
    record_task_event("take", id=ticket["id"], title=ticket["title"], agent=source, actor=source)
    _log_task("task_taken", agent=source, ticket=ticket)
    print(serialize_ticket(ticket))


def _done_command(argv: list[str]) -> None:
    parser = _operation_parser("done", "Finish your open task and record its outcome.")
    parser.add_argument("id", nargs="?", help="ticket id or unique prefix")
    parser.add_argument(
        "--outcome",
        choices=("completed", "passed", "failed"),
        help="completed work, or the verdict from review work",
    )
    args = parser.parse_args(argv)
    if args.outcome is None:
        if sys.stdin.isatty():
            args.outcome = input("outcome required (completed, passed, or failed): ").strip()
            if args.outcome not in ("completed", "passed", "failed"):
                raise OfficeError("outcome must be completed, passed, or failed")
        else:
            raise OfficeError(
                "done requires --outcome {completed,passed,failed}; "
                "use completed for ordinary work"
            )
    _finish_command("done", args.id, outcome=args.outcome)


def _cancel_command(argv: list[str]) -> None:
    parser = _operation_parser("cancel", "Cancel your open task.")
    parser.add_argument("id", nargs="?", help="ticket id or unique prefix")
    args = parser.parse_args(argv)
    _finish_command("cancel", args.id)


def _finish_command(action: str, task_id: str | None, *, outcome: str | None = None) -> None:
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    _, raw, ticket = _select(r, keys, ("doing",), task_id)
    ticket["status"] = "done" if action == "done" else "cancelled"
    ticket["done_ts"] = _now()
    if outcome is not None:
        ticket["outcome"] = outcome
    _transition_selected(
        r,
        source_key=keys["doing"],
        destination_key=keys["done"],
        raw=raw,
        replacement=serialize_ticket(ticket),
    )
    record_task_event(
        action,
        id=ticket["id"],
        title=ticket["title"],
        agent=source,
        actor=source,
        outcome=outcome,
    )
    log_event = "task_done" if action == "done" else "task_cancelled"
    _log_task(log_event, agent=source, ticket=ticket)
    print(serialize_ticket(ticket))


def _return_command(argv: list[str]) -> None:
    parser = _operation_parser("return", "Return your open task to todo.")
    parser.add_argument("id", nargs="?", help="ticket id or unique prefix")
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    _, raw, ticket = _select(r, keys, ("doing",), args.id)
    ticket["status"] = "todo"
    ticket["started_ts"] = None
    ticket["done_ts"] = None
    ticket["held_ts"] = None
    ticket.pop("hold_reason", None)
    ticket.pop("outcome", None)
    _transition_selected(
        r,
        source_key=keys["doing"],
        destination_key=keys["todo"],
        raw=raw,
        replacement=serialize_ticket(ticket),
    )
    record_task_event(
        "return", id=ticket["id"], title=ticket["title"], agent=source, actor=source
    )
    _log_task("task_returned", agent=source, ticket=ticket)
    print(serialize_ticket(ticket))


def _show_command(argv: list[str]) -> None:
    parser = _operation_parser("show", "Read one ticket without changing its board state.")
    parser.add_argument("id", help="ticket id or unique prefix")
    parser.add_argument("-a", "--agent", metavar="AGENT", help="board owner (default: you)")
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    agent = args.agent or source
    if agent != source and not is_member(r, pod=pod, tenant=tenant, agent=agent):
        raise OfficeError(f"unknown board owner {agent!r}")
    keys = _task_keys(pod, tenant, agent)
    _, raw, _ = _select(r, keys, ("todo", "doing", "hold", "done"), args.id)
    print(raw.decode() if isinstance(raw, bytes) else raw)


def _retitle_command(argv: list[str]) -> None:
    parser = _operation_parser("retitle", "Correct the title of your open task.")
    parser.add_argument("id", nargs="?", help="ticket id or unique prefix")
    parser.add_argument("--title", required=True, help="replacement title")
    args = parser.parse_args(argv)
    title = args.title.strip()
    if not title:
        raise OfficeError("replacement title cannot be empty")
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    state, raw, normalized = _select(r, keys, ("todo", "doing", "hold"), args.id)
    # A targeted edit owns exactly one field. Unknown extensions and legacy
    # spellings are preservation obligations, not migration opportunities, so
    # rewrite the original object rather than normalize_ticket's projection.
    old_title = normalized["title"]
    replacement = _replace_top_level_title(raw, title)
    _rewrite_selected(r, key=keys[state], raw=raw, replacement=replacement)
    record_task_event(
        "retitle",
        id=normalized["id"],
        title=title,
        old_title=old_title,
        agent=source,
        actor=source,
    )
    log_record(
        "office",
        "task_retitled",
        destination=source,
        task_id=normalized["id"],
        title=title,
        old_title=old_title,
    )
    print(replacement.decode() if isinstance(replacement, bytes) else replacement)


def _hold_command(argv: list[str]) -> None:
    parser = _operation_parser("hold", "Put your active or queued task on hold.")
    parser.add_argument("id", nargs="?", help="ticket id or unique prefix")
    parser.add_argument("--reason", required=True, help="why the task cannot proceed")
    args = parser.parse_args(argv)
    if not args.reason.strip():
        raise OfficeError("hold reason cannot be empty")
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    # Preserve the established no-ID shorthand for the active ticket. Queued
    # parking is explicit: when an ID is supplied, it may select either the
    # active ticket or queued work without first moving that work into doing.
    # Holds therefore do not consume the one-doing slot; they remain separately
    # visible, with their reason, in tasks.hold.
    states = ("doing",) if args.id is None else ("doing", "todo")
    state, raw, ticket = _select(r, keys, states, args.id)
    ticket["status"] = "hold"
    ticket["held_ts"] = _now()
    ticket["hold_reason"] = args.reason
    _transition_selected(
        r,
        source_key=keys[state],
        destination_key=keys["hold"],
        raw=raw,
        replacement=serialize_ticket(ticket),
    )
    record_task_event("hold", id=ticket["id"], title=ticket["title"], agent=source, actor=source)
    _log_task("task_held", agent=source, ticket=ticket)
    print(serialize_ticket(ticket))


def _delete_command(argv: list[str]) -> None:
    parser = _operation_parser("delete", "Permanently remove a task.")
    parser.add_argument("id", help="ticket id or unique prefix")
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    state, raw, ticket = _select(r, keys, ("todo", "doing", "hold", "done"), args.id)
    _remove(r, keys[state], raw)
    record_task_event("delete", id=ticket["id"], title=ticket["title"], agent=source, actor=source)
    _log_task("task_deleted", agent=source, ticket=ticket)
    print(serialize_ticket(ticket))


def _add_command(argv: list[str]) -> None:
    parser = _operation_parser("add", "Add a task to another agent's board.")
    parser.add_argument("-a", "--agent", required=True, metavar="AGENT")
    parser.add_argument("-t", "--title", required=True, metavar="TITLE")
    parser.add_argument("-d", "--description", required=True, metavar="DESCRIPTION")
    parser.add_argument("-p", "--priority", metavar="PRIORITY")
    parser.add_argument("-r", "--related", metavar="ID[,ID...]",
                         help="ticket ids this one references -- stored, never validated "
                              "(a related ticket may live on another agent's board)")
    args = parser.parse_args(argv)

    r, pod, tenant, source = _context()
    if not is_member(r, pod=pod, tenant=tenant, agent=args.agent):
        raise OfficeError(f"unknown destination agent {args.agent!r}")
    # Allocate the established 32-lowercase-hex identity locally. This state is
    # ALLOCATED; send returning below advances the envelope to ADMITTED, while
    # only downstream custody can prove the board ticket was CREATED.
    ticket_id = os.urandom(16).hex()
    payload = {
        "v": 1,
        "id": ticket_id,
        "title": args.title,
        "description": args.description,
        "created_by": source,
        "status": "todo",
        "created_ts": _now(),
        "started_ts": None,
        "done_ts": None,
        "held_ts": None,
        "priority": args.priority,
    }
    if args.related:
        related = list(dict.fromkeys(value.strip() for value in args.related.split(",") if value.strip()))
        if related:
            payload["related"] = related
    send(
        r,
        pod=pod,
        tenant=tenant,
        source=source,
        destination=args.agent,
        payload=payload,
        kind="AddTicket",
        module="office",
    )
    print(ticket_id)


# ---------------------------------------------------------------------------
# clone-to-all
# ---------------------------------------------------------------------------


def _repo_name(repo_url: str) -> str:
    tail = repo_url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    if not tail or tail in (".", ".."):
        raise OfficeError(f"cannot determine repository name from {repo_url!r}")
    return tail


def _clone_agents(r, *, pod: str, tenant: str, requested: str | None) -> list[str]:
    tmux_agents = {
        agent
        for agent in members(r, pod=pod, tenant=tenant)
        if port_type(r, pod=pod, tenant=tenant, agent=agent) == "tmux"
    }
    if requested is None:
        return sorted(tmux_agents)

    selected = list(dict.fromkeys(name.strip() for name in requested.split(",") if name.strip()))
    if not selected:
        raise OfficeError("-a requires at least one agent")
    invalid = [name for name in selected if name not in tmux_agents]
    if invalid:
        raise OfficeError(f"not a tmux agent: {', '.join(invalid)}")
    return selected


def _git_clone(source: str, target: Path, upstream: str) -> tuple[bool, str]:
    try:
        clone = subprocess.run(
            ["git", "clone", source, str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if clone.returncode:
            detail = clone.stderr.strip() or clone.stdout.strip() or "git clone failed"
            return False, detail
        remote = subprocess.run(
            ["git", "-C", str(target), "remote", "set-url", "origin", upstream],
            capture_output=True,
            text=True,
            check=False,
        )
        if remote.returncode:
            detail = remote.stderr.strip() or remote.stdout.strip() or "could not set origin"
            return False, detail
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _clone_to_all_command(argv: list[str]) -> None:
    parser = _operation_parser("clone-to-all", "Clone one repository into agent workspaces.")
    parser.add_argument("repo_url", metavar="REPO-URL")
    parser.add_argument("-a", "--agents", metavar="AGENT,...", help="comma-separated tmux agents")
    parser.add_argument("--dry-run", action="store_true", help="show actions without writing")
    args = parser.parse_args(argv)

    r, pod, tenant, _ = _context()
    agents = _clone_agents(r, pod=pod, tenant=tenant, requested=args.agents)
    repo_name = _repo_name(args.repo_url)
    workdir_root = Path(get_workdir_root())
    targets = [(agent, workdir_root / agent / repo_name) for agent in agents]

    if args.dry_run:
        skipped = 0
        for agent, target in targets:
            if target.exists():
                skipped += 1
                print(f"{agent}: exists, would skip")
            else:
                print(f"{agent}: would clone")
        print(f"summary: cloned=0 skipped={skipped} failed=0")
        return

    cloned = skipped = failed = 0
    # A source is only ever a target CLONED DURING THIS CALL (set below on
    # success), never a target that already existed before it started -- a
    # pre-existing agent mixed into the same -a list as fresh ones must not
    # become the source those fresh clones are made from.
    local_source = None
    for agent, target in targets:
        if target.exists():
            skipped += 1
            print(f"{agent}: exists, skipped")
            continue

        source = str(local_source) if local_source is not None else args.repo_url
        ok, detail = _git_clone(source, target, args.repo_url)
        if ok:
            cloned += 1
            local_source = local_source or target
            print(f"{agent}: cloned")
        else:
            failed += 1
            if target.exists():
                shutil.rmtree(target)
            print(f"{agent}: failed: {detail}")
    print(f"summary: cloned={cloned} skipped={skipped} failed={failed}")
    if failed:
        raise OfficeError(f"{failed} clone operation(s) failed")


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def _format_token_count(n: int) -> str:
    if n <= 0:
        return "-"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        val = n / 1000.0
        if val >= 100 or val.is_integer():
            return f"{int(round(val))}k"
        return f"{val:.1f}k"
    val = n / 1_000_000.0
    if val.is_integer():
        return f"{int(round(val))}M"
    return f"{val:.2f}M"


def _usage_command(argv: list[str]) -> None:
    parser = _operation_parser("usage", "Show token usage and estimated cost.")
    parser.add_argument("-a", "--agent", metavar="AGENT", help="filter by agent name")
    parser.add_argument("--since", metavar="ISO", help="filter records since ISO timestamp")
    parser.add_argument("--json", action="store_true", help="output JSON format")
    args = parser.parse_args(argv)

    r, pod, tenant, _ = _context()
    usage_key = prefix(pod, tenant, resource="usage")
    entries = r.xrange(usage_key, min="-", max="+")

    since_dt = _timestamp(args.since) if args.since else None

    import collections
    aggregates = collections.defaultdict(lambda: {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0})
    latest_rate_limits = {}

    for item in entries:
        raw_fields = item[1] if isinstance(item, tuple) and len(item) == 2 else item
        fields = _fields(raw_fields) if hasattr(raw_fields, "items") else {}
        raw_usage = fields.get("usage")
        if not raw_usage:
            continue
        try:
            rec = json.loads(raw_usage)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue

        agent = rec.get("agent", "unknown")
        if args.agent is not None and agent != args.agent:
            continue

        if since_dt is not None:
            ts = _timestamp(rec.get("ts"))
            if ts is not None and ts < since_dt:
                continue

        cli = rec.get("cli", "unknown")
        model = rec.get("model", "unknown")
        key = (agent, cli, model)
        aggregates[key]["input"] += int(rec.get("input", 0) or 0)
        aggregates[key]["cache_read"] += int(rec.get("cache_read", 0) or 0)
        aggregates[key]["cache_write"] += int(rec.get("cache_write", 0) or 0)
        aggregates[key]["output"] += int(rec.get("output", 0) or 0)
        if rec.get("rate_limits"):
            latest_rate_limits[key] = rec["rate_limits"]

    # Check for enrolled agents running unmeasurable CLIs (agy)
    try:
        all_members = sorted(members(r, pod=pod, tenant=tenant))
    except Exception:
        all_members = []
    agy_agents = set()
    for ag in all_members:
        if args.agent is not None and ag != args.agent:
            continue
        launch_val = _text(r.get(prefix(pod, tenant, agent=ag, resource="launch")))
        if launch_val == "agy":
            agy_agents.add(ag)

    pricing = load_pricing()
    rows = []
    total_usd = 0.0

    for (agent, cli, model), counts in sorted(aggregates.items()):
        cost, is_priced = calculate_cost(
            model,
            input_tokens=counts["input"],
            cache_read=counts["cache_read"],
            cache_write=counts["cache_write"],
            output_tokens=counts["output"],
            pricing=pricing,
        )
        if is_priced and cost is not None:
            total_usd += cost
        row_dict = {
            "agent": agent,
            "cli": cli,
            "model": model,
            "input": counts["input"],
            "cache_read": counts["cache_read"],
            "cache_write": counts["cache_write"],
            "output": counts["output"],
            "usd": round(cost, 4) if (is_priced and cost is not None) else None,
            "unpriced": not is_priced,
        }
        if (agent, cli, model) in latest_rate_limits:
            row_dict["rate_limits"] = latest_rate_limits[(agent, cli, model)]
        rows.append(row_dict)

    for ag in sorted(agy_agents):
        rows.append({
            "agent": ag,
            "cli": "agy",
            "model": "not collected",
            "input": 0,
            "cache_read": 0,
            "cache_write": 0,
            "output": 0,
            "usd": None,
            "unpriced": True,
            "collected": False,
        })

    if args.json:
        print(json.dumps({"rows": rows, "total_usd": round(total_usd, 2)}, indent=2))
        return

    has_rate_limits = any("rate_limits" in r for r in rows)
    header = f"{'agent':<10}{'cli':<8}{'model':<23}{'input':>7}{'cache_r':>10}{'cache_w':>9}{'output':>9}{'USD':>10}"
    if has_rate_limits:
        header += f"{'limit':>15}"
    print(header)
    for row in rows:
        is_uncollected = row.get("collected") is False or row.get("measurable") is False
        in_str = "-" if is_uncollected else _format_token_count(row["input"])
        cr_str = "-" if is_uncollected else _format_token_count(row["cache_read"])
        cw_str = "-" if is_uncollected else _format_token_count(row["cache_write"])
        out_str = "-" if is_uncollected else _format_token_count(row["output"])
        usd_str = "unpriced" if row["unpriced"] else f"{row['usd']:.2f}"
        line = (
            f"{row['agent']:<10}{row['cli']:<8}{row['model']:<23}"
            f"{in_str:>7}{cr_str:>10}{cw_str:>9}{out_str:>9}{usd_str:>10}"
        )
        if has_rate_limits:
            rl = row.get("rate_limits") or {}
            prim = rl.get("primary") or {}
            used_pct = prim.get("used_percent")
            plan = rl.get("plan_type")
            if used_pct is not None:
                rl_str = f"{int(used_pct)}% ({plan})" if plan else f"{int(used_pct)}%"
            else:
                rl_str = "-"
            line += f"{rl_str:>15}"
        print(line)
    if rows:
        print(f"{'':>66}{'------':>10}")
        print(f"{'':>66}{total_usd:>10.2f}")


# ---------------------------------------------------------------------------
# Dispatch: one table, not three hand-synced places.
#
# The previous system kept a command-name tuple, a descriptions dict, and an
# if/elif dispatch chain in sync by hand across every alias -- and its own
# CONTRACTS doc recorded that list drifting twice from exactly that. Building
# fresh, `_COMMAND_TABLE` is the only place a command and its aliases are
# named; `_COMMANDS`, `_DESCRIPTIONS`, and `_DISPATCH` are all derived from it
# below, so adding an alias means editing one tuple, not three files' worth
# of independently-maintained lists.
#
# `profiles` has no entry here yet: it needs an `available_profiles()`
# (configured-accounts) read that has no h-mesh equivalent anywhere -- core,
# lib, or elsewhere -- so there is nothing correct to build it against yet.
# ---------------------------------------------------------------------------

_COMMAND_TABLE: tuple[tuple[tuple[str, ...], str, "callable"], ...] = (
    (("send",), "send a message to one agent", _send_command),
    (("send-file",), "send a file attachment to one agent", _send_file_command),
    (("broadcast",), "send a message to every peer agent", _broadcast_command),
    (("peers",), "list peer agents", _peers_command),
    (("status",), "show agent presence and open work", _status_command),
    (("hire",), "enrol a new agent", lambda argv: _lifecycle_command("hire", argv)),
    (("let-go",), "retire an agent", lambda argv: _lifecycle_command("letGo", argv)),
    (("pause",), "pause an agent's CLI", lambda argv: _lifecycle_command("pause", argv)),
    (("resume",), "resume an agent's CLI and inbox", lambda argv: _lifecycle_command("resume", argv)),
    (("list",), "show a task board", _list_command),
    (("unresolved",), "show unresolved delivery outcomes", _unresolved_command),
    (("undeliverable",), "show messages not opened before retirement", _undeliverable_command),
    (("retired-inbox",), "show api agents' inbox content conserved at retirement", _retired_inbox_command),
    (("take",), "take your next todo task", _take_command),
    (("done",), "finish your open task and record its outcome", _done_command),
    (("cancel",), "cancel your open task", _cancel_command),
    (("return",), "return your open task to todo", _return_command),
    (("show",), "read one ticket without changing it", _show_command),
    (("retitle",), "correct the title of your open task", _retitle_command),
    (("hold",), "put an active or queued task on hold", _hold_command),
    (("delete",), "permanently remove a task", _delete_command),
    (("add",), "add a task to another agent's board", _add_command),
    (("clone-to-all",), "clone a repository into agent workspaces", _clone_to_all_command),
    (("usage",), "show token usage and estimated cost", _usage_command),
)

_COMMANDS: tuple[str, ...] = tuple(name for names, _, _ in _COMMAND_TABLE for name in names)
_DESCRIPTIONS: dict[str, str] = {name: desc for names, desc, _ in _COMMAND_TABLE for name in names}
_DISPATCH: dict[str, "callable"] = {name: handler for names, _, handler in _COMMAND_TABLE for name in names}


def _root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="office",
        description="Message peers and manage agents in this office.",
    )
    subcommands = parser.add_subparsers(dest="command", metavar="COMMAND")
    for name in _COMMANDS:
        subcommands.add_parser(name, help=_DESCRIPTIONS[name], add_help=False)
    return parser


def _dispatch(args: list[str]) -> None:
    parser = _root_parser()
    if not args:
        parser.print_help()
        return
    if args[0] in ("-h", "--help"):
        parser.parse_args(args)
        return

    command, remainder = args[0], args[1:]
    if command not in _DISPATCH:
        parser.error(f"unknown command: {command}")
        return
    try:
        _DISPATCH[command](remainder)
    except OfficeError as exc:
        print(f"office: error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def main(argv: Sequence[str] | None = None) -> None:
    # ⚠ Scoped, not global. This command runs inside an agent's window, so its
    # stdout is a pane and bus telemetry printed there is a signpost the agent
    # does not need (core/logging.py's own H_MESH_LOG_QUIET note). Restore the
    # previous value afterwards: main() is called in-process by the test
    # suite, and leaking the flag would silence unrelated components'
    # logging for the rest of the run.
    previous_quiet = os.environ.get("H_MESH_LOG_QUIET")
    os.environ["H_MESH_LOG_QUIET"] = "1"
    try:
        _dispatch(list(sys.argv[1:] if argv is None else argv))
    finally:
        if previous_quiet is None:
            os.environ.pop("H_MESH_LOG_QUIET", None)
        else:
            os.environ["H_MESH_LOG_QUIET"] = previous_quiet


def clone_to_all_entrypoint(argv: Sequence[str] | None = None) -> None:
    """`clone-to-all <url>` -- the bare name on PATH, one implementation behind it.

    Renamed from the previous system's `clone_to_all_main`: this module's own
    `main()` is a different entry point (the whole dispatcher) and naming
    both "main" invited exactly the kind of accidental reimplementation that
    happened once before there -- a second, independent copy of this command
    existed for two days, silently dropped the cleanup of a half-written
    clone, and left directories every later run misread as already done.
    Delegate; do not reimplement.
    """
    main(["clone-to-all", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    main()
