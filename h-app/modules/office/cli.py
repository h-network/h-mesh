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
import http.client
import importlib
import ipaddress
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote, urlsplit
from datetime import datetime, timezone
from pathlib import Path

import redis

from core.channels import send
from core.envelope import EnvelopeError, parse
from core.keys import (
    incarnation_key, interoffice_participants_key, prefix, receive_undeliverable_key, receive_unresolved_key, retired_inbox_key, validate_segment,
)
from core.logging import log_record, record_task_event
from core.receipts import trace as trace_delivery
from core.registry import is_member, members, port_type
from lib.attachment_schema import ATTACHMENT_MAX_BYTES, MIME_TYPE_REGEX
from lib.board_interaction import (
    AUTHORITY_MAX_CHARS, NOTE_MAX_CHARS, NOTES_MAX, BoardError, authority_block, normalize_ticket,
    serialize_ticket, valid_notes,
)
from lib.chat_memory import TTL_SECONDS_MAX as _CHAT_MEMORY_TTL_SECONDS
from lib.chat_memory import ChatMemory
from lib.paths import ensure_workdir_ownership, get_workdir_root
from lib import doc_store
from lib.paths import get_workdir_root
from lib.reply_correlation import is_valid_reply_id

from .pricing import calculate_cost, load_pricing

# ⚠ A tenant with a Redis password exports REDIS_URL carrying it. Without one
# this is unchanged, and an agent window still has no REDIS_URL to find.
_REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
ATTACHMENT_MAX_CAPTION_BYTES = 65_536


class OfficeError(ValueError):
    """A user-facing command error."""

    exit_code = 1


class OfficeUnknownOutcome(OfficeError):
    """The request may or may not have taken effect (exit 3, never 1).

    Distinct from every provable failure: callers must not blindly retry.
    """

    exit_code = 3


def _context():
    source = os.environ.get("AGENT_NAME")
    if not source:
        raise OfficeError("AGENT_NAME environment variable not set")
    pod = os.environ.get("POD", "default")
    tenant = os.environ.get("TENANT", "default")
    return redis.Redis.from_url(_REDIS_URL), pod, tenant, source


class _UsageErrorParser(argparse.ArgumentParser):
    """An ArgumentParser whose own parse failures exit with a caller-chosen
    code instead of argparse's hardcoded 2.

    Most callers pass the default (2) and see no change. `hire` is the
    exception: its own outcomes already use exit 1 (a real, stream_id-proven
    rejection) and 2 (sent, unconfirmed within the wait) -- see
    _lifecycle_command. A parse failure (an invalid --cli choice, a missing
    required argument, ...) happens before send() is ever called, so nothing
    was sent at all, but reusing argparse's default 2 made that
    indistinguishable from the real "sent, no proof of failure" outcome to
    every caller, setup.sh included: it reported "requested -- no rejection
    seen" for a hire that never left this process. See hire's own
    --cli/--wait help text for the exit code contract this protects.
    """

    def __init__(self, *args, usage_error_exit_code: int = 2, **kwargs):
        self._usage_error_exit_code = usage_error_exit_code
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(self._usage_error_exit_code, f"{self.prog}: error: {message}\n")


def _operation_parser(
    command: str, description: str, *, usage_error_exit_code: int = 2
) -> argparse.ArgumentParser:
    return _UsageErrorParser(
        prog=f"office {command}", description=description,
        usage_error_exit_code=usage_error_exit_code,
    )


# ---------------------------------------------------------------------------
# send, send-file, broadcast, peers, profiles, status
# ---------------------------------------------------------------------------


def _message(
    r, *, pod: str, tenant: str, source: str, destination: str, text: str,
    in_reply_to: str | None = None, context: str | None = None,
) -> str:
    payload = {"text": text}
    if context is not None:
        # `context` rides in the payload, not as a separate send() argument
        # -- it's an ordinary Message field modules/claude_sdk/port.py reads
        # (see its own docstring), not wire-format/switch mechanics core.
        # channels needs to know about. Meaningless to a non-claude_sdk
        # destination, but harmless: every other port already ignores
        # payload keys it doesn't recognize.
        payload["context"] = context
    return send(
        r,
        pod=pod,
        tenant=tenant,
        source=source,
        destination=destination,
        payload=payload,
        kind="Message",
        module="office",
        in_reply_to=in_reply_to,
    )


# A double-quoted shell argument is expanded BEFORE office sees it: `name` and
# $(cmd) are executed and replaced by their output (usually nothing), $VAR by
# its value (usually nothing). What office receives is already shorter, and a
# shorter body is indistinguishable from a deliberate one -- the byte count
# printed on success then matches what was received, not what was meant.
# The one trace that is detectable is the hole left behind: "the `x` setting"
# arrives as "the  setting". Two or more spaces between words is therefore
# reported (stderr, never blocking). Heuristic and deliberately narrow: it
# misses a dropped word at a line edge or next to punctuation, and fires on
# aligned columns; the .!?-preceded "two spaces after a sentence" is exempt.
_SHELL_HOLE_RE = re.compile(r"(?<=[^\s.!?])[ ]{2,}(?=\S)")


def _warn_if_shell_mangled(text: str, what: str, command: str) -> None:
    """Warn on stderr when argv text shows the hole a shell expansion leaves.

    Only for text that arrived as a command-line argument; --stdin/--file text
    is never shell-parsed and is not checked.
    """
    for line in text.split("\n"):
        if _SHELL_HOLE_RE.search(line.strip()):
            print(
                f"office: warning: {what} has a double space; if a word is missing, the shell "
                "removed it (backticks, $(...) and $VARS inside a double-quoted argument are "
                f"expanded before office sees them). Resend with `office {command} ... --stdin` "
                "or --file for exact text.",
                file=sys.stderr,
            )
            return


def _resolve_message_text(args, command: str) -> str:
    """Resolve one message body from positional text, --stdin, or --file.

    Shared by send and broadcast so both reject the same conflicts with the
    same wording.
    """
    if (args.stdin or args.file is not None) and args.text is not None:
        raise OfficeError(
            f"office {command} accepts exactly one of positional text, --stdin, or --file"
        )
    if args.stdin:
        text = sys.stdin.read()
        if text == "":
            raise OfficeError(f"office {command} --stdin received no message text")
        return text
    if args.file is not None:
        try:
            return args.file.read_text(encoding="utf-8")
        except OSError as exc:
            raise OfficeError(f"cannot read message file {str(args.file)!r}: {exc}") from exc
    if args.text is not None:
        if not args.text.strip():
            raise OfficeError(
                f"office {command} message text is empty -- if it was built with backticks, "
                "$(...) or $VARS, the shell may have expanded it to nothing; use --stdin or --file"
            )
        _warn_if_shell_mangled(args.text, "the message text", command)
        return args.text
    raise OfficeError(f"office {command} requires message text, --stdin, or --file")


def _send_command(argv: list[str]) -> None:
    parser = _operation_parser("send", "Send a message to one agent.")
    parser.add_argument("-a", "--agent", metavar="AGENT", required=True, help="destination agent")
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
        "--context",
        metavar="CONTEXT",
        help=(
            "for a claude_sdk destination: the memory context this message belongs "
            "to (see modules/claude_sdk/port.py) -- same context on a later send "
            "recalls this one, a fresh context or none starts (or stays) memory-free. "
            "Ignored by every other destination port_type."
        ),
    )
    parser.add_argument(
        "--office",
        metavar="OFFICE",
        help=(
            "send to agent AGENT in another office from this office's address book "
            "(see 'office offices') instead of a local agent. Exit 0 = accepted by "
            "the remote office (queued, not delivered); 1 = refused, provably nothing "
            "sent; 3 = outcome unknown (timeout/reset/bad reply) -- do not blindly retry."
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
    text = _resolve_message_text(args, "send")
    if args.office is not None:
        if args.reply_to is not None or args.context is not None:
            raise OfficeError("--office cannot be combined with --reply-to or --context")
        _send_to_office(args.office, args.agent, text)
        return

    if args.reply_to is not None and not is_valid_reply_id(args.reply_to):
        raise OfficeError(
            f"--reply-to {args.reply_to!r} is not a 32-character lowercase hex stream_id"
        )
    if args.context is not None:
        try:
            validate_segment(args.context)
        except KeyError:
            raise OfficeError(
                f"--context {args.context!r} must be lowercase alphanumeric/hyphens, "
                "1-63 chars, starting with a letter or digit, not all-digits"
            )

    r, pod, tenant, source = _context()
    if not is_member(r, pod=pod, tenant=tenant, agent=args.agent):
        raise OfficeError(f"unknown destination agent {args.agent!r}")
    stream_id = _message(
        r, pod=pod, tenant=tenant, source=source, destination=args.agent, text=text,
        in_reply_to=args.reply_to, context=args.context,
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
    parser.add_argument("-a", "--agent", metavar="AGENT", required=True, help="destination agent")
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

def _doc_command(argv: list[str]) -> None:
    """`office doc put|get|ls|info`: store a document once, refer to it by hash."""
    parser = _operation_parser("doc", "Store a document once and refer to it by hash.")
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    put_p = actions.add_parser("put", help="store a file; prints its doc:<hash> reference")
    put_p.add_argument("path", type=Path, metavar="PATH", help="path to regular file")
    put_p.add_argument("--name", metavar="NAME", help="mutable name that points at this version")
    put_p.add_argument("--mime-type", metavar="TYPE", help="MIME type (guessed from extension if omitted)")

    get_p = actions.add_parser("get", help="print a document's bytes, or write them with -o")
    get_p.add_argument("ref", metavar="REF", help="doc:<hash>, a hash prefix (8+ hex), or a name")
    get_p.add_argument("-o", "--output", type=Path, metavar="PATH", help="write to PATH instead of stdout")

    actions.add_parser("ls", help="list stored documents")

    info_p = actions.add_parser("info", help="show a document's metadata without its body")
    info_p.add_argument("ref", metavar="REF", help="doc:<hash>, a hash prefix (8+ hex), or a name")

    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    try:
        _doc_run(args, r, pod, tenant, source)
    except doc_store.DocError as exc:
        raise OfficeError(str(exc)) from exc


def _doc_run(args, r, pod: str, tenant: str, source: str) -> None:
    if args.action == "put":
        path = args.path
        if not path.exists():
            raise OfficeError(f"document file does not exist: {str(path)!r}")
        if not path.is_file():
            raise OfficeError(f"document path {str(path)!r} is not a regular file")
        _validate_filename(path.name)
        if args.mime_type is not None:
            _validate_mime_type(args.mime_type)
            mime_type = args.mime_type
        else:
            guessed, _ = mimetypes.guess_type(path.name)
            mime_type = guessed if guessed and MIME_TYPE_REGEX.match(guessed) else "application/octet-stream"
        try:
            if path.stat().st_size > ATTACHMENT_MAX_BYTES:
                raise OfficeError(
                    f"document file size exceeds maximum allowed size of {ATTACHMENT_MAX_BYTES} bytes"
                )
            data = path.read_bytes()
        except OSError as exc:
            raise OfficeError(f"cannot read document file {str(path)!r}: {exc}") from exc
        doc_hash, created = doc_store.put(
            r, pod=pod, tenant=tenant, author=source, data=data, filename=path.name,
            mime_type=mime_type, name=args.name,
        )
        state = "stored" if created else "already stored"
        label = f" as {args.name!r}" if args.name else ""
        print(f"{state}: {doc_store.REF_SCHEME}{doc_store.short(doc_hash)} {len(data)} bytes{label}")
    elif args.action == "get":
        _, _, data = doc_store.get(r, pod=pod, tenant=tenant, ref=args.ref)
        if args.output is None:
            sys.stdout.flush()
            sys.stdout.buffer.write(data)
            return
        try:
            args.output.write_bytes(data)
        except OSError as exc:
            raise OfficeError(f"cannot write {str(args.output)!r}: {exc}") from exc
        print(f"wrote {len(data)} bytes to {args.output}")
    elif args.action == "info":
        doc_hash, meta, _ = doc_store.get(r, pod=pod, tenant=tenant, ref=args.ref)
        print(f"hash: {doc_hash}")
        for field in ("filename", "mime_type", "size", "author", "created"):
            print(f"{field}: {meta.get(field, 'unknown')}")
    else:
        for row in doc_store.listing(r, pod=pod, tenant=tenant):
            names = f"  [{', '.join(row['names'])}]" if row["names"] else ""
            print(
                f"{doc_store.REF_SCHEME}{doc_store.short(row['hash'])}  {row.get('size', '?')} bytes  "
                f"{row.get('filename', '?')}  {row.get('author', '?')}  {row.get('created', '?')}{names}"
            )


def _broadcast_command(argv: list[str]) -> None:
    parser = _operation_parser("broadcast", "Send a message to every peer agent.")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--stdin", action="store_true", help="read message text from stdin")
    sources.add_argument("--file", type=Path, metavar="PATH", help="read message text from a file")
    parser.add_argument(
        "text",
        nargs="?",
        help="one quoted message argument; use -- before text that begins with a dash",
    )
    args = parser.parse_args(argv)
    text = _resolve_message_text(args, "broadcast")

    r, pod, tenant, source = _context()
    recipients = sorted(
        agent
        for agent in members(r, pod=pod, tenant=tenant)
        if agent != source and port_type(r, pod=pod, tenant=tenant, agent=agent) == "tmux"
    )
    for destination in recipients:
        print(_message(r, pod=pod, tenant=tenant, source=source, destination=destination, text=text))


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


def _contexts_command(argv: list[str]) -> None:
    parser = _operation_parser("contexts", "List a claude_sdk agent's live memory contexts.")
    parser.add_argument("-a", "--agent", metavar="AGENT", required=True, help="claude_sdk destination agent")
    args = parser.parse_args(argv)

    if not args.agent:
        raise OfficeError("office contexts requires -a <agent>")

    r, pod, tenant, _source = _context()
    if not is_member(r, pod=pod, tenant=tenant, agent=args.agent):
        raise OfficeError(f"unknown agent {args.agent!r}")
    if port_type(r, pod=pod, tenant=tenant, agent=args.agent) != "claude_sdk":
        raise OfficeError(f"{args.agent!r} is not a claude_sdk agent -- it has no memory contexts")

    # Direct Redis read, same as office status/peers -v already do for their
    # own per-agent state -- not a round trip through the ListContexts
    # envelope kind: this file already owns a Redis client and this agent's
    # (pod, tenant, agent) identity, the exact shape lib/chat_memory.py's
    # own docstring says any such caller can use directly.
    memory = ChatMemory(r, pod, tenant, args.agent, ttl_seconds_max=_CHAT_MEMORY_TTL_SECONDS)
    contexts = memory.list_chat_ids()
    if not contexts:
        print(f"{args.agent}: no live memory contexts")
        return
    print(f"{args.agent}: {', '.join(contexts)}")


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
    decoded_blocked = (
        {_text(field): _text(value) for field, value in blocked.items()} if blocked else None
    )
    # Presence and delivery verification are different facts with different
    # owners. A blocked hash says one paste was not verified; it does not say
    # the agent is currently unable to work, and must not override the state
    # PresenceSampler derived from current activity.
    state = decoded_presence.get("state") or "unknown"

    doing_key = prefix(pod, tenant, agent=agent, resource="tasks.doing")
    raw_ticket = next(iter(r.lrange(doing_key, 0, 0)), None)
    if raw_ticket is None:
        task = "—"
    else:
        ticket = normalize_ticket(raw_ticket, state="doing")
        opened = _age(ticket.get("started_ts"), now=now)
        task = f'"{ticket["title"]}"' + (f" {opened}" if opened else "")

    if state == "unknown":
        activity = "no activity feed"
    else:
        last = _age(decoded_presence.get("last_activity"), now=now)
        activity = f"last activity {last} ago" if last else "no activity yet"
    if decoded_blocked is not None:
        unverified_age = _age(decoded_blocked.get("since"), now=now)
        activity += (
            f"; delivery unverified for {unverified_age}"
            if unverified_age
            else "; delivery unverified (age unknown)"
        )
    return f"  {agent:<12} {state:<10} {task:<35} {activity}"


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


def _run_command(argv: list[str]) -> None:
    parser = _operation_parser(
        "run", "Open a non-addressable process window; not an agent. "
        "Use: office run NAME [--cwd PATH] [-- COMMAND ...]. "
        "Default: bash -il. Exited commands stay visible and are never restarted.",
    )
    parser.add_argument("name", help="window label (lowercase office-name syntax)")
    parser.add_argument("--cwd", type=Path, help="working directory (default: current directory)")
    separator = argv.index("--") if "--" in argv else None
    args = parser.parse_args(argv if separator is None else argv[:separator])
    command = None if separator is None else argv[separator + 1:]
    if command == []:
        parser.error("provide a command after --, or omit -- for a shell")
    session = os.environ.get("TMUX_SESSION") or os.environ.get("TENANT")
    if not session:
        raise OfficeError("run requires TMUX_SESSION or TENANT for an existing office session")
    from modules.tmux.ops import create_run_window
    try:
        window = create_run_window(
            session, args.name, command, cwd=str(args.cwd) if args.cwd else None,
            socket=os.environ.get("TMUX_SOCKET"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise OfficeError(str(exc)) from exc
    print(f"opened non-addressable window {window} in {session}")


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
    parser = _operation_parser(
        command, descriptions[command],
        usage_error_exit_code=_HIRE_USAGE_ERROR_EXIT if command == "hire" else 2,
    )
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
        # ⚠ Bare hire (no --wait at all) still runs a short, SILENT
        # attributable-completion check (see _BARE_HIRE_CHECK_TIMEOUT_S)
        # before returning -- operator's explicit call, reversing an
        # earlier removal of --wait entirely: a fast sanity check that can
        # catch a real, proven rejection is worth having on every hire,
        # but a message that fires on every hire saying nothing new (the
        # far more common "unknown" outcome) is noise, not signal. So bare
        # hire speaks ONLY on a proven rejection (exit 1, same failure
        # message --wait itself would print) and stays exactly as quiet as
        # it always was -- prints the stream_id, exits 0 -- on "unknown".
        # An explicit --wait keeps its existing loud contract for BOTH
        # outcomes, since asking for it means wanting to know either way.
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
        # unknown. Safely reporting a real success needs a stream-id-keyed
        # attributable-completion signal (both success AND failure, and
        # covering identity-creation and idempotent-reconfiguration
        # separately) that nothing in this codebase writes today -- 1s/5s
        # make the cost of checking bearable, they do not make success
        # provable.
        parser.add_argument(
            "--wait", nargs="?", const=5.0, type=_wait_seconds, default=None, metavar="SECONDS",
            help="wait up to SECONDS (default 5; a bare hire with no --wait at all "
                 "still waits 1s, silently, for the same check) and report what can "
                 "be proven about this request, instead of returning as soon as it's "
                 "merely accepted. Two outcomes today, not three: exit 1 = failed (a "
                 "real, stream_id-matched rejection, seen in the destination's "
                 "dead-letter list); exit 2 = unknown -- no proof of failure within "
                 "SECONDS. Exit 0 is NOT currently reachable: bare registry "
                 "membership cannot be attributed to this specific request under "
                 "concurrency (a different request for the same agent name could be "
                 "the one that actually registered it, or already had), so this "
                 "never claims success -- only the absence of a proven failure. A "
                 "timeout is not a failure: a stranded request can still complete "
                 "later once switch recovery re-kicks it (see switch-agent's "
                 "drain/recovery fix). A bad invocation -- an invalid --cli choice, "
                 "a missing agent, an unrecognized flag -- never reaches send() at "
                 "all and exits 3, not 1 or 2: nothing was requested.",
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
    if command == "hire":
        explicit_wait = args.wait is not None
        timeout = args.wait if explicit_wait else _BARE_HIRE_CHECK_TIMEOUT_S
        outcome, detail = _await_hire_confirmation(
            r, pod=pod, tenant=tenant, stream_id=stream_id, timeout=timeout,
        )
        if outcome == "failed":
            print(f"failed: {args.agent} was not registered -- {detail}", file=sys.stderr)
            raise SystemExit(1)
        # outcome == "unknown" -- explicit --wait always speaks (that's what
        # asking for it means); bare hire stays silent and falls through to
        # the same plain stream_id print it always had, unchanged, whether
        # "unknown" means a genuine timeout OR the check itself couldn't
        # run (a Redis read failure) -- bare hire never distinguished those
        # in its silence to begin with, so neither needs special handling
        # here; only explicit --wait's message needs to say which one it
        # was, so it doesn't claim a real SECONDS-long wait ran when the
        # check never got to run at all.
        if explicit_wait:
            if detail is not None:
                print(
                    f"unknown: could not check {args.agent}'s hire status -- {detail} -- "
                    "the request was admitted, but whether it succeeded or failed could "
                    "not be checked. Check 'office status' or the registry directly if "
                    "you want to look for yourself.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"unknown: no proof of failure for {args.agent} within {timeout:.0f}s -- "
                    "this does NOT mean it failed, it may well have succeeded; there is "
                    "currently no way to prove success for this request, only to disprove "
                    "it. Check 'office status' or the registry directly if you want to "
                    "look for yourself.",
                    file=sys.stderr,
                )
            raise SystemExit(2)
    print(stream_id)


# ⚠ Not a magic number picked here -- the operator's own value, restoring
# --wait after removing it entirely once: a bare hire waits this long,
# silently, for the same proven-rejection check --wait itself runs. Keep
# in sync with _lifecycle_command's --wait help text if this ever changes.
_BARE_HIRE_CHECK_TIMEOUT_S = 1.0

# Distinct from hire's own exit 1 (failed) and 2 (unknown) -- a parse
# failure on hire's own arguments never reaches send(), so it must not be
# mistaken for either real outcome. setup.sh's hire-status dispatch already
# treats any exit code outside {0, 1, 2} as a hard, surfaced error (see its
# own comment above the roster loop); this just keeps hire's parser from
# colliding with the two exit codes that already mean something else.
_HIRE_USAGE_ERROR_EXIT = 3

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
    or left honestly unknown. The real fix would be a stream-id-
    attributable lifecycle completion signal, covering explicit
    idempotent-success and reconfiguration-success cases, not just a
    reordered or better-gated registry check -- nothing in this codebase
    writes that signal today.

    ⚠ Read-only against the dead-letter list (LRANGE, never pop) -- popping
    it here would consume evidence a human or another tool still needs to
    see, for a check this command has no ownership over.

    ⚠ Only the destination ("host") ever dead-letters a StartAgent it
    itself rejected -- the tmux-source-only feedback path
    (_notify_dead_letter_sender in core.channels) never reaches a caller
    like this one (source is usually "host" itself, not a tmux agent), so
    this reads the recipient's dead list directly instead of waiting for a
    reply message that would never arrive.

    ⚠ Best-effort against a read failure, not fail-closed -- reviewer
    FAILED an earlier version of this branch for the inverse of its own
    principle: send() had already ADMITTED the request and returned a
    real stream_id before this function is ever called, so a Redis error
    reading the dead-letter list must not destroy that proven result by
    propagating as an uncaught exception (empty stdout, empty stderr, the
    one thing the caller already knew gone). A failure to CHECK is not
    evidence of failure -- it collapses to the same "unknown" outcome a
    timeout does, carrying `detail` so callers that print an explicit
    message (an actual --wait, not a bare hire) can say honestly that the
    check itself didn't run, rather than implying a real SECONDS-long
    wait happened when it didn't.
    """
    dead_key = prefix(pod, tenant, agent="host", resource="dead")
    deadline = time.monotonic() + timeout
    while True:
        try:
            raw_entries = r.lrange(dead_key, 0, -1)
        except redis.RedisError as exc:
            return "unknown", f"could not check -- {exc}"
        for raw in raw_entries:
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


# ---------------------------------------------------------------------------
# board event notices (#18): ordinary Message envelopes, so they show in the
# mesh log like any other. `add` tells the assignee, `done` tells the creator.
# ---------------------------------------------------------------------------

# One assignment notice per target per window: a bulk `add` of N tickets must
# not paste N lines into one agent's pane. The suppressed tickets are still on
# the board, and the notice that does go out points at `list`.
_ADD_NOTICE_WINDOW_SECONDS = 30
_OFFICE_CMD = os.environ.get("OFFICE_TOOLS", "h-mesh-office")


def _one_line(value: str, limit: int = 100) -> str:
    flat = " ".join(str(value).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "\u2026"


def _send_board_notice(r, *, pod: str, tenant: str, source: str, destination: str, text: str) -> bool:
    """Send one notice; the board change it reports is already committed, so a
    failure here is reported on stderr and never fails the command."""
    try:
        _message(r, pod=pod, tenant=tenant, source=source, destination=destination, text=text)
        return True
    except Exception as exc:
        print(
            f"office: warning: the board change succeeded but the notice to {destination} "
            f"was not sent ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return False


def _announce_assignment(
    r, *, pod: str, tenant: str, source: str, target: str, ticket_id: str, title: str
) -> None:
    """Notice to one assignee, queued behind the AddTicket on the same
    source->destination path so the ticket is normally on the board before the
    notice is read. SET NX EX is the debounce: only the first add per target
    per window announces."""
    window_key = prefix(pod, tenant, agent=target, resource="notice.add")
    try:
        first = bool(r.set(window_key, source, nx=True, ex=_ADD_NOTICE_WINDOW_SECONDS))
    except Exception:
        first = True
    if first:
        _send_board_notice(
            r, pod=pod, tenant=tenant, source=source, destination=target,
            text=(
                f'[board] {source} assigned you ticket {ticket_id[:8]} '
                f'"{_one_line(title)}". Run `{_OFFICE_CMD} list` to see your queue, '
                f"`{_OFFICE_CMD} take` to start. Further tickets added within "
                f"{_ADD_NOTICE_WINDOW_SECONDS}s are not announced. Notice only, no reply needed."
            ),
        )


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


def _select(
    r,
    keys: dict[str, str],
    states: Sequence[str],
    reference: str | None,
    *,
    missing_message: str | None = None,
):
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
        raise OfficeError(missing_message or f"no task matches id {reference!r}")
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
    if ticket.get("notes"):
        line += f"  notes:{len(ticket['notes'])}"
    age = _ticket_age(ticket, state=state, now=now)
    if age:
        line += f"  age:{age}"
    if state == "hold" and ticket.get("hold_reason"):
        line += f"  reason:{' '.join(ticket['hold_reason'].split())}"
    if state == "done":
        if ticket.get("status") == "cancelled":
            line += "  cancelled"
        elif ticket.get("outcome"):
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
            if state == "hold":
                print(f"{indent}  (resume with: take <id> or unhold <id>)")
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


_DELIVERY_TERMINAL = frozenset({"delivered", "failed", "unresolved"})


def _delivery_command(argv: list[str]) -> None:
    parser = _operation_parser(
        "delivery",
        "Show where a sent message is: queued, delivering, delivered, failed, "
        "unresolved or unknown. Read-only.",
    )
    parser.add_argument("stream_id", metavar="STREAM_ID", help="the id printed by office send")
    parser.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="poll until the message is delivered, failed or unresolved, or SECONDS pass",
    )
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{32}", args.stream_id):
        raise OfficeError(f"{args.stream_id!r} is not a 32-character lowercase hex stream_id")
    if args.wait < 0 or not math.isfinite(args.wait):
        raise OfficeError("--wait must be a non-negative number of seconds")
    r, pod, tenant, source = _context()
    deadline = time.monotonic() + args.wait
    while True:
        result = trace_delivery(
            r, pod=pod, tenant=tenant, stream_id=args.stream_id, extra_agents=(source,)
        )
        if result["state"] in _DELIVERY_TERMINAL or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    print(json.dumps(result, separators=(",", ":")))


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
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full ticket as JSON instead of a one-line confirmation",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="do not send the completion notice to the ticket's creator",
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
    _finish_command("done", args.id, outcome=args.outcome, json_output=args.json,
                    notify=not args.no_notify)


def _cancel_command(argv: list[str]) -> None:
    parser = _operation_parser("cancel", "Cancel your open task.")
    parser.add_argument("id", nargs="?", help="ticket id or unique prefix")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the full ticket as JSON instead of a one-line confirmation",
    )
    args = parser.parse_args(argv)
    _finish_command("cancel", args.id, json_output=args.json)


def _finish_command(
    action: str, task_id: str | None, *, outcome: str | None = None, json_output: bool = False,
    notify: bool = True,
) -> None:
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    # `done` acts only on the taken (doing) ticket. `cancel <id>` may also drop
    # an untaken (todo) one, but bare `cancel` (no id) still abandons the single
    # active ticket -- key on whether an id was given, like `hold` does, so a
    # todo backlog doesn't make the no-id form ambiguous.
    states = ("doing",) if action == "done" or task_id is None else ("doing", "todo")
    state, raw, ticket = _select(r, keys, states, task_id)
    ticket["status"] = "done" if action == "done" else "cancelled"
    ticket["done_ts"] = _now()
    if outcome is not None:
        ticket["outcome"] = outcome
    _transition_selected(
        r,
        source_key=keys[state],
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
    if action == "done" and notify:
        creator = ticket.get("created_by")
        # Not to oneself (a self-added ticket), and only to an agent that is
        # still enrolled: a creator that retired or is not a participant has
        # nowhere to receive it, and that is not an error for `done`.
        if (
            isinstance(creator, str) and creator and creator != source
            and is_member(r, pod=pod, tenant=tenant, agent=creator)
        ):
            _send_board_notice(
                r, pod=pod, tenant=tenant, source=source, destination=creator,
                text=(
                    f'[board] {source} finished ticket {ticket["id"][:8]} '
                    f'"{_one_line(ticket["title"])}" ({outcome}). '
                    "Notice only, no reply needed."
                ),
            )
    if json_output:
        print(serialize_ticket(ticket))
    else:
        verb = "done" if action == "done" else "cancelled"
        detail = f" ({outcome})" if outcome else ""
        print(f'{verb} {ticket["id"]}: "{ticket["title"]}"{detail}')


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


def _show_everywhere(r, *, pod: str, tenant: str, reference: str) -> None:
    """Office-wide `show`: every enrolled board's matches for an id or prefix.

    Read-only. An unreadable entry on some board is skipped rather than
    quarantined -- this path never mutates another agent's board.
    """
    found = 0
    for owner in sorted(members(r, pod=pod, tenant=tenant)):
        try:
            keys = _task_keys(pod, tenant, owner)
        except KeyError:
            continue
        for state in ("todo", "doing", "hold", "done"):
            for raw in r.lrange(keys[state], 0, -1):
                try:
                    ticket = normalize_ticket(raw, state=state)
                except BoardError:
                    continue
                if ticket["id"].startswith(reference):
                    found += 1
                    print(json.dumps({"agent": owner, "state": state, "ticket": ticket}, separators=(",", ":")))
    if not found:
        raise OfficeError(f"no task matches id {reference!r} on any board")


def _show_command(argv: list[str]) -> None:
    parser = _operation_parser("show", "Read one ticket without changing its board state.")
    parser.add_argument("id", help="ticket id or unique prefix")
    parser.add_argument("-a", "--agent", metavar="AGENT",
                        help="board owner (default: you); 'all' searches every enrolled board and "
                             "prints one JSON line {agent, state, ticket} per match")
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    agent = args.agent or source
    if agent == "all":
        _show_everywhere(r, pod=pod, tenant=tenant, reference=args.id)
        return
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


def _reject_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise OfficeError("stored ticket has duplicate fields; refusing to rewrite it")
    return dict(pairs)


def _note_command(argv: list[str]) -> None:
    parser = _operation_parser(
        "note", "Append a progress note to one of your open tasks (kept on the ticket)."
    )
    parser.add_argument("-i", "--id", metavar="ID", help="ticket id or unique prefix (default: your doing task)")
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("--stdin", action="store_true", help="read the note from stdin")
    sources.add_argument("--file", type=Path, metavar="PATH", help="read the note from a file")
    parser.add_argument("text", nargs="?", help="one quoted note")
    args = parser.parse_args(argv)
    text = _resolve_message_text(args, "note").strip()
    if not text:
        raise OfficeError("note cannot be empty")
    if len(text) > NOTE_MAX_CHARS:
        raise OfficeError(
            f"note is {len(text)} characters; the limit is {NOTE_MAX_CHARS}. "
            "Keep the ticket to what was tried and found; send the rest as a message"
        )
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    states = ("doing",) if args.id is None else ("todo", "doing", "hold")
    state, raw, normalized = _select(
        r, keys, states, args.id,
        missing_message=(
            f"no open task matches id {args.id!r}; a finished ticket must be "
            "reopened first (office reopen ID)"
        ) if args.id else None,
    )
    # Edit the stored object itself, not normalize_ticket's projection, so a
    # field this CLI does not know about survives, as retitle guarantees.
    try:
        stored = json.loads(
            raw.decode() if isinstance(raw, bytes) else raw,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        # OfficeError is a ValueError: let the duplicate-field refusal pass through.
        raise OfficeError("stored ticket is not valid JSON; refusing to rewrite it") from exc
    if not isinstance(stored, dict):
        raise OfficeError("stored ticket is not a JSON object")
    notes = stored.get("notes", [])
    if not isinstance(notes, list):
        raise OfficeError("stored ticket has a malformed notes field; refusing to rewrite it")
    if len(notes) >= NOTES_MAX:
        raise OfficeError(
            f"ticket already has {len(notes)} notes (limit {NOTES_MAX}); "
            "nothing was added or dropped"
        )
    notes.append({"ts": _now(), "by": source, "text": text})
    stored["notes"] = notes
    replacement = serialize_ticket(stored)
    _rewrite_selected(r, key=keys[state], raw=raw, replacement=replacement)
    record_task_event("note", id=normalized["id"], title=normalized["title"], agent=source, actor=source)
    log_record(
        "office", "task_noted", destination=source, task_id=normalized["id"],
        title=normalized["title"],
    )
    print(f"noted {normalized['id']} ({len(notes)} on ticket)")


def _reopen_command(argv: list[str]) -> None:
    parser = _operation_parser(
        "reopen",
        "Reopen one of your finished tasks as doing (or queued with --todo).",
    )
    parser.add_argument("id", help="ticket id or unique prefix of a done or cancelled task")
    parser.add_argument(
        "--todo", action="store_true",
        help="queue it in todo instead of taking it now (needed if you already have a doing task)",
    )
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    _, raw, ticket = _select(
        r, keys, ("done",), args.id,
        missing_message=f"no finished task matches id {args.id!r}; reopen works on your own done or cancelled tasks",
    )
    previous = "cancelled" if ticket.get("status") == "cancelled" else (
        f"done ({ticket['outcome']})" if ticket.get("outcome") else "done"
    )
    now = _now()
    # The earlier verdict would otherwise vanish with the outcome field, so it is
    # kept on the ticket as a note: reopening never erases what was decided.
    ticket["notes"] = valid_notes(ticket.get("notes")) + [
        {"ts": now, "by": source, "text": f"reopened; was {previous}"}
    ]
    ticket["done_ts"] = None
    ticket["held_ts"] = None
    ticket.pop("outcome", None)
    ticket.pop("hold_reason", None)
    if args.todo:
        ticket["status"] = "todo"
        ticket["started_ts"] = None
    else:
        ticket["status"] = "doing"
        ticket["started_ts"] = now
        if r.llen(keys["doing"]):
            raise OfficeError(
                "you already have one open task; reopen with --todo to queue it instead"
            )
    try:
        _transition_selected(
            r,
            source_key=keys["done"],
            destination_key=keys["todo" if args.todo else "doing"],
            raw=raw,
            replacement=serialize_ticket(ticket),
            require_destination_empty=not args.todo,
        )
    except OfficeError as exc:
        if str(exc) == "you already have one open task":
            raise OfficeError(
                "you already have one open task; reopen with --todo to queue it instead"
            ) from exc
        raise
    record_task_event("reopen", id=ticket["id"], title=ticket["title"], agent=source, actor=source)
    _log_task("task_reopened", agent=source, ticket=ticket)
    print(serialize_ticket(ticket))


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
    parser = _operation_parser("delete", "Permanently remove a task from your own board.")
    parser.add_argument("id", help="ticket id or unique prefix")
    args = parser.parse_args(argv)
    r, pod, tenant, source = _context()
    keys = _task_keys(pod, tenant, source)
    state, raw, ticket = _select(
        r,
        keys,
        ("todo", "doing", "hold", "done"),
        args.id,
        missing_message=(
            f"delete searches only your own board; no task matches id {args.id!r}. "
            "It cannot withdraw a task assigned to another agent"
        ),
    )
    _remove(r, keys[state], raw)
    record_task_event("delete", id=ticket["id"], title=ticket["title"], agent=source, actor=source)
    _log_task("task_deleted", agent=source, ticket=ticket)
    print(serialize_ticket(ticket))


def _purge_command(argv: list[str]) -> None:
    # Imported here: only this command needs the tmux mechanics, and the other
    # commands run inside agent windows where a lighter import is the norm.
    from modules.tmux.ops import PurgeError, purge_session_history

    parser = _operation_parser(
        "purge",
        "Permanently delete a RETIRED agent's on-disk CLI session history (irreversible).",
    )
    parser.add_argument("agent", help="exact name of the retired agent")
    # ⚠ Required, not defaulted: retirement deletes the agent's launch and
    # profile keys, so once it is gone nothing records which CLI or account it
    # ran. Guessing would purge the wrong store or miss the right one.
    parser.add_argument("--cli", required=True, choices=("claude", "codex", "agy"),
                        help="which CLI's history to delete")
    parser.add_argument("--profile", metavar="ACCOUNT",
                        help="the account it ran under (default: the default account)")
    parser.add_argument("--confirm", required=True, metavar="AGENT",
                        help="the agent's exact name again, to confirm the deletion")
    args = parser.parse_args(argv)

    try:
        validate_segment(args.agent)
    except KeyError:
        raise OfficeError(f"invalid agent name {args.agent!r}") from None
    if args.confirm != args.agent:
        raise OfficeError(f"--confirm must repeat the agent name exactly ({args.agent!r}); nothing was removed")
    r, pod, tenant, _source = _context()
    # Fail closed: if the registry cannot be read, liveness is unknown.
    try:
        enrolled = is_member(r, pod=pod, tenant=tenant, agent=args.agent)
    except redis.RedisError as exc:
        raise OfficeError(f"cannot confirm {args.agent!r} is retired ({exc}); nothing was removed") from exc
    if enrolled:
        raise OfficeError(
            f"{args.agent!r} is still enrolled; let-go it first. A live session may rewrite its history"
        )
    try:
        count = purge_session_history(args.agent, args.cli, profile=args.profile)
    except (PurgeError, OSError) as exc:
        raise OfficeError(f"purge failed: {exc}") from exc
    unit = "history line" if args.cli == "agy" else "session"
    print(f"purged {count} {unit}{'' if count == 1 else 's'} for {args.agent} ({args.cli})")
def _board_state_holding(r, *, pod: str, tenant: str, agent: str, ticket_id: str) -> str | None:
    """The state (todo/doing/hold/done) in which ``agent``'s board holds exactly
    ``ticket_id``, else None. Unreadable entries are ignored."""
    keys = _task_keys(pod, tenant, agent)
    for state in ("todo", "doing", "hold", "done"):
        for raw in r.lrange(keys[state], 0, -1):
            try:
                if normalize_ticket(raw, state=state)["id"] == ticket_id:
                    return state
            except BoardError:
                continue
    return None


def _add_command(argv: list[str]) -> None:
    parser = _operation_parser("add", "Add a task to another agent's board.")
    parser.add_argument("-a", "--agent", required=True, metavar="AGENT[,AGENT...]",
                         help="board owner; several comma-separated owners get the SAME ticket id "
                              "on each board")
    parser.add_argument("--id", metavar="TICKET_ID", dest="ticket_id",
                         help="reuse an existing 32-hex ticket id instead of allocating one (to give "
                              "the same piece of work to another board later); refused if that "
                              "board already holds the id")
    parser.add_argument("-t", "--title", required=True, metavar="TITLE")
    description = parser.add_mutually_exclusive_group(required=True)
    description.add_argument("-d", "--description", metavar="DESCRIPTION")
    description.add_argument("--description-stdin", action="store_true",
                             help="read the description from stdin (never shell-parsed)")
    description.add_argument("--description-file", type=Path, metavar="PATH",
                             help="read the description from a file (never shell-parsed)")
    parser.add_argument("-p", "--priority", metavar="PRIORITY")
    parser.add_argument("-r", "--related", metavar="ID[,ID...]",
                         help="ticket ids this one references -- stored, never validated "
                              "(a related ticket may live on another agent's board)")
    parser.add_argument("--no-notify", action="store_true",
                        help="do not send the assignment notice to the target agent")
    parser.add_argument("--authority", metavar="TEXT",
                         help="the authoritative directive for this ticket (what you, as creator, "
                              "are instructing/approving), shown by take/show with your name and "
                              f"the time; max {AUTHORITY_MAX_CHARS} chars. Set once at add time")
    args = parser.parse_args(argv)

    targets = list(dict.fromkeys(value.strip() for value in args.agent.split(",")))
    if not all(targets):
        raise OfficeError("office add -a: empty agent name in the list")
    if args.ticket_id is not None and not is_valid_reply_id(args.ticket_id):
        raise OfficeError(f"--id {args.ticket_id!r} is not a 32-character lowercase hex ticket id")
    if args.description_stdin:
        args.description = sys.stdin.read()
        if not args.description.strip():
            raise OfficeError("office add --description-stdin received no description")
    elif args.description_file is not None:
        try:
            args.description = args.description_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise OfficeError(f"cannot read description file {str(args.description_file)!r}: {exc}") from exc
    else:
        for value, what in ((args.title, "the title"), (args.description, "the description")):
            if not value.strip():
                raise OfficeError(
                    f"office add {what} is empty -- the shell may have expanded it to nothing "
                    "(backticks, $(...), $VARS); use --description-stdin or --description-file"
                )
        _warn_if_shell_mangled(args.description, "the description", "add")
    _warn_if_shell_mangled(args.title, "the title", "add")

    if args.authority is not None and authority_block(args.authority, by="x", ts="x") is None:
        raise OfficeError(
            f"--authority must be non-empty text of at most {AUTHORITY_MAX_CHARS} characters"
        )
    r, pod, tenant, source = _context()
    for target in targets:
        if not is_member(r, pod=pod, tenant=tenant, agent=target):
            raise OfficeError(f"unknown destination agent {target!r}")
    # Allocate the established 32-lowercase-hex identity locally (or reuse the
    # caller's canonical one). This state is ALLOCATED; send returning below
    # advances the envelope to ADMITTED, while only downstream custody can
    # prove the board ticket was CREATED.
    ticket_id = args.ticket_id or os.urandom(16).hex()
    if args.ticket_id is not None:
        for target in targets:
            holder = _board_state_holding(r, pod=pod, tenant=tenant, agent=target, ticket_id=ticket_id)
            if holder:
                raise OfficeError(f"{target}'s board already holds ticket {ticket_id} ({holder})")
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
    if args.authority is not None:
        # Only the text travels; the receiving board stamps who and when
        # (lib/board_interaction.authority_block), so it cannot be forged here.
        payload["authority"] = args.authority.strip()
    sent: list[str] = []
    for target in targets:
        try:
            send(
                r,
                pod=pod,
                tenant=tenant,
                source=source,
                destination=target,
                payload=payload,
                kind="AddTicket",
                module="office",
            )
        except Exception:
            if sent:
                print(
                    f"office: add stopped at {target!r} after sending ticket {ticket_id} to "
                    f"{', '.join(sent)}; re-run with --id {ticket_id} for the rest",
                    file=sys.stderr,
                )
            raise
        sent.append(target)
        if not args.no_notify and target != source:
            _announce_assignment(r, pod=pod, tenant=tenant, source=source, target=target,
                                 ticket_id=ticket_id, title=args.title)
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
        ensure_workdir_ownership(target)
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
    header = f"{'agent':<10} {'cli':<8} {'model':<23} {'input':>7}{'cache_r':>10}{'cache_w':>9}{'output':>9}{'USD':>10}"
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
            f"{row['agent']:<10} {row['cli']:<8} {row['model']:<23} "
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
        print(f"{'':>79}{'------':>10}")
        print(f"{'':>79}{total_usd:>10.2f}")


# ---------------------------------------------------------------------------
# inter-office: address book, send --office, cooperation grants
#
# The sending half is this CLI alone: it POSTs straight to the other office's
# api door (no relay on the send path). Everything it needs is in the
# caller's own Redis (address book + token) and the HTTP contract:
#   POST <url>/agents/<agent>/envelopes
#   Authorization: Bearer coop.<token-id>.<secret>
#   {"text": ..., "as": "<participant>"}  ->  202 {stream_id, correlation_id}
# A 202 means "queued on the remote office's egress", never "delivered".
# ---------------------------------------------------------------------------

_OFFICE_SENT_TTL_SECONDS = 86400
_OFFICE_HTTP_TIMEOUT_SECONDS = 10.0
_COOP_TOKEN_RE = re.compile(r"^coop\.[0-9a-f]{16}\.[A-Za-z0-9_-]{43}$")
_COOP_TOKEN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
# The remote office's fixed participants; a cooperation can never name them
# (the receiving door refuses them as well -- failing here is only faster).
_FIXED_PARTICIPANTS = frozenset({"api", "host"})
# Local api participant the relay daemon replies from; enrolled by office-add.
_RELAY_PARTICIPANT = "relay"


def _key(name: str, pod: str, tenant: str, *args):
    """Prefer switch-agent's named helper in core.keys once it exists; until
    then build the contract's exact resource name with prefix()."""
    import core.keys as core_keys

    helper = getattr(core_keys, f"{name}_key", None)
    if helper is not None:
        return helper(pod, tenant, *args)
    resources = {
        "offices": "offices",
        "office_tokens": "offices.tokens",
        "coop_tokens": "coop-tokens",
    }
    if name == "office_sent":
        return prefix(pod, tenant, resource=f"xsent.s{args[0]}")
    return prefix(pod, tenant, resource=resources[name])


def _mark_interoffice(r, pod: str, tenant: str, participant: str, role: str) -> None:
    """Record a local api participant as speaking for another office.

    core.channels does not treat such a sender as a client owed a reply.
    """
    try:
        r.hset(interoffice_participants_key(pod, tenant), participant, role)
    except redis.RedisError as exc:
        raise OfficeError(f"cannot record {participant!r} as an inter-office participant: {exc}") from exc


def _require_lead(r, *, pod: str, tenant: str, source: str, command: str) -> None:
    """Fail closed unless the caller is the tenant's lead.

    ⚠ Advisory, not authentication: the caller's identity is the AGENT_NAME
    environment variable and anything with Redis access can write these keys
    directly. This stops mistakes and non-lead agents, not a hostile local
    process. See modules/office/README.md.
    """
    lead = _text(r.get(prefix(pod, tenant, resource="lead")))
    if not lead:
        raise OfficeError(f"office {command} is lead-only and this office has no lead")
    if lead != source:
        raise OfficeError(f"office {command} is lead-only (the lead is {lead!r}, you are {source!r})")


def _is_loopback_host(host: str) -> bool:
    host = host.strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_office_url(raw: str, *, allow_plaintext: bool) -> tuple[str, bool]:
    """Return (normalised url, is_plaintext_over_the_network)."""
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        parts.port  # noqa: B018 -- raises ValueError on a bad port
    except ValueError as exc:
        raise OfficeError(f"invalid office url {raw!r}: {exc}") from exc
    if parts.scheme not in ("http", "https") or not host:
        raise OfficeError("office url must be http(s)://host[:port][/prefix]")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise OfficeError("office url must not carry credentials, a query or a fragment")
    plaintext = parts.scheme == "http" and not _is_loopback_host(host)
    if plaintext and not (allow_plaintext or os.environ.get("H_MESH_ALLOW_PLAINTEXT") == "1"):
        raise OfficeError(
            "plaintext http to a non-loopback host sends the cooperation token in the clear; "
            "use https (the supported default), or pass --allow-plaintext for a lab test bed"
        )
    return raw.rstrip("/"), plaintext


def _segment_arg(value: str, what: str) -> str:
    try:
        return validate_segment(value)
    except KeyError:
        raise OfficeError(
            f"invalid {what} {value!r}: lowercase alphanumeric/hyphens, 1-63 chars, "
            "starting with a letter or digit, not all-digits, not a reserved word"
        ) from None


def _office_record(r, *, pod: str, tenant: str, name: str) -> dict | None:
    try:
        raw = r.hget(_key("offices", pod, tenant), name)
    except redis.RedisError as exc:
        raise OfficeError(f"cannot read the office address book: {exc}") from exc
    if raw is None:
        return None
    try:
        record = json.loads(_text(raw))
    except json.JSONDecodeError:
        record = None
    if not isinstance(record, dict) or not isinstance(record.get("url"), str) \
            or not isinstance(record.get("participant"), str):
        raise OfficeError(f"address book entry for office {name!r} is unreadable; re-add it")
    return record


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would replay the Authorization header
    to wherever the other end points us."""

    def redirect_request(self, *args, **kwargs):
        return None


def _server_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read(2048).decode("utf-8", "replace")
    except Exception:
        return ""


def _server_detail(body: str, token: str) -> str:
    detail = body
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "detail" in parsed:
            detail = parsed["detail"] if isinstance(parsed["detail"], str) else json.dumps(parsed["detail"])
    except json.JSONDecodeError:
        pass
    detail = " ".join(detail.split())[:200]
    return detail.replace(token, "***") if token else detail


def _server_reason(body: str) -> str | None:
    """The machine reason of a 401: top-level "reason" (the contract); a
    reason nested in "detail" is tolerated. Anything else is None."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    reason = parsed.get("reason")
    if reason is None and isinstance(parsed.get("detail"), dict):
        reason = parsed["detail"].get("reason")
    return reason if reason in _TOKEN_REASON_TEXT else None


_REGRANT_HINT = "; ask its lead for a fresh coop-grant, then re-run office-add"
_TOKEN_REASON_TEXT = {
    "revoked": "the cooperation token was revoked by the remote office",
    "unknown_token": "the remote office does not recognise this token "
                     "(wrong token, or it was issued by another office)",
    "participant_not_enrolled": "the participant this token was granted to is no longer "
                                "enrolled at the remote office",
}


# connect-phase failures that prove no byte of the request reached the server.
_NOT_SENT_ERRORS = (ConnectionRefusedError, socket.gaierror)


def _post_to_office(url: str, token: str, agent: str, body: dict) -> dict:
    """POST one envelope; return the parsed 202 body.

    Raises OfficeError when the failure proves nothing was accepted and
    OfficeUnknownOutcome when it may have been.
    """
    endpoint = f"{url}/agents/{quote(agent, safe='')}/envelopes"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=_OFFICE_HTTP_TIMEOUT_SECONDS) as response:
            status = response.status
            raw = response.read(65536)
    except urllib.error.HTTPError as exc:
        exc_body = _server_body(exc)
        detail = _server_detail(exc_body, token)
        if exc.code >= 500 or 300 <= exc.code < 400:
            raise OfficeUnknownOutcome(
                f"remote office answered HTTP {exc.code}"
                f"{': ' + detail if detail else ''} -- the message may have been accepted; "
                "do not blindly retry"
            ) from None
        reason = _server_reason(exc_body)
        hint = {
            401: " -- " + _TOKEN_REASON_TEXT.get(
                reason, "the remote office did not accept our cooperation token "
                        "(wrong, revoked or not for this office)") + _REGRANT_HINT,
            403: " -- the remote office's cooperation policy does not allow this token to "
                 f"send to {agent!r}; ask its lead to widen the grant",
            404: f" -- the remote office has no agent {agent!r} (check the name with its lead)",
        }.get(exc.code, "")
        raise OfficeError(
            f"remote office refused the message: HTTP {exc.code}"
            f"{': ' + detail if detail else ''}{hint}"
        ) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, _NOT_SENT_ERRORS):
            raise OfficeError(f"cannot reach the remote office at {url}: {exc.reason}") from None
        raise OfficeUnknownOutcome(
            f"request to the remote office failed mid-flight ({exc.reason}) -- the message "
            "may have been accepted; do not blindly retry"
        ) from None
    except (OSError, http.client.HTTPException) as exc:
        raise OfficeUnknownOutcome(
            f"connection to the remote office broke ({type(exc).__name__}) -- the message "
            "may have been accepted; do not blindly retry"
        ) from None
    try:
        reply = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        reply = None
    stream_id = reply.get("stream_id") if isinstance(reply, dict) else None
    if status != 202 or not is_valid_reply_id(stream_id):
        raise OfficeUnknownOutcome(
            f"remote office answered HTTP {status} without a valid stream_id -- the message "
            "may have been accepted; do not blindly retry"
        )
    return reply


_OFFICE_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class _Link:
    """Result of probing a remote office with a saved/proposed token.

    state is one of: ok, mismatch, token_problem, unsupported, unreachable,
    error. message is one plain line (never carries the token).
    """
    state: str
    message: str
    participant: str | None = None
    office: str | None = None


def _probe_office(url: str, token: str, expect_participant: str | None) -> _Link:
    """GET {url}/whoami with the token. Never raises, never follows redirects."""
    request = urllib.request.Request(
        f"{url}/whoami", method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=_OFFICE_PROBE_TIMEOUT_SECONDS) as response:
            status = response.status
            raw = response.read(65536)
    except urllib.error.HTTPError as exc:
        body = _server_body(exc)
        detail = _server_detail(body, token)
        if exc.code == 401:
            reason = _server_reason(body)
            text = _TOKEN_REASON_TEXT.get(
                reason, "the remote office did not accept the cooperation token "
                        "(wrong, revoked or not for this office)")
            return _Link("token_problem", text + _REGRANT_HINT)
        if exc.code in (404, 405):
            return _Link("unsupported", (
                f"the remote office at {url} has no /whoami (older build?), so the link "
                "cannot be verified; upgrade it, or use --no-verify to save unchecked"))
        if 300 <= exc.code < 400:
            return _Link("error", (
                f"the remote office answered HTTP {exc.code} (a redirect); redirects are never "
                "followed because they would replay the token -- use the final url"))
        return _Link("error", (
            f"the remote office answered HTTP {exc.code}{': ' + detail if detail else ''}"))
    except urllib.error.URLError as exc:
        return _Link("unreachable", f"cannot reach the remote office at {url}: {exc.reason}")
    except (OSError, http.client.HTTPException) as exc:
        return _Link("unreachable", (
            f"connection to the remote office at {url} broke ({type(exc).__name__})"))
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        info = None
    who = info.get("participant") if isinstance(info, dict) else None
    if status != 200 or not isinstance(who, str) or not who:
        return _Link("error", (
            f"{url} did not answer /whoami like an h-mesh api door "
            f"(HTTP {status}); is the url the api door?"))
    remote_office = info.get("office") if isinstance(info.get("office"), str) else None
    if info.get("api") != 1:
        return _Link("error", (
            f"the remote office speaks api version {info.get('api')!r}, this CLI understands 1; "
            "upgrade whichever side is older"), who, remote_office)
    if expect_participant is not None and who != expect_participant:
        return _Link("mismatch", (
            f"the remote office says this token belongs to participant {who!r}, "
            f"but the address book has {expect_participant!r}; "
            f"use --participant {who}"), who, remote_office)
    return _Link("ok", "ok", who, remote_office)


def _send_to_office(office: str, agent: str, text: str) -> None:
    office = _segment_arg(office, "office name")
    agent = _segment_arg(agent, "agent name")
    if agent in _FIXED_PARTICIPANTS:
        raise OfficeError(f"cannot send to {agent!r}: not an agent")
    r, pod, tenant, source = _context()
    record = _office_record(r, pod=pod, tenant=tenant, name=office)
    if record is None:
        raise OfficeError(f"unknown office {office!r} (see: office offices)")
    try:
        token = _text(r.hget(_key("office_tokens", pod, tenant), office))
    except redis.RedisError as exc:
        raise OfficeError(f"cannot read the office token: {exc}") from exc
    if not token:
        raise OfficeError(f"office {office!r} has no token; the lead must re-add it")

    # Captured BEFORE the POST and retained across it: the sender identity the
    # reply must go back to is the incarnation that sent, not whoever holds
    # this name when the reply arrives. Unreadable/absent -> None, which the
    # relay treats as uncorrelated (routes to the lead), never as a match.
    try:
        sender_incarnation = _text(r.get(incarnation_key(pod, tenant, source))) or None
    except Exception:
        sender_incarnation = None

    reply = _post_to_office(
        record["url"], token, agent, {"text": text, "as": record["participant"]},
    )
    stream_id = reply["stream_id"]
    correlation_id = reply.get("correlation_id")
    if not is_valid_reply_id(correlation_id):
        correlation_id = None
    # The message is already accepted: a failed bookkeeping write must not
    # make this look failed (a retry would duplicate it). It only costs reply
    # routing for this one message, which we say out loud.
    try:
        r.set(
            _key("office_sent", pod, tenant, stream_id),
            json.dumps({
                "sender": source, "sender_incarnation": sender_incarnation,
                "correlation_id": correlation_id,
                "office": office, "agent": agent, "ts": _now(),
            }),
            ex=_OFFICE_SENT_TTL_SECONDS,
        )
    except Exception as exc:
        print(
            f"office: warning: sent, but could not record the sender for reply routing "
            f"({type(exc).__name__}); a reply to {stream_id} cannot be routed back to you",
            file=sys.stderr,
        )
    print(f"sent to {office}/{agent}: {len(text.encode('utf-8'))} bytes ({stream_id})")


def _office_add_command(argv: list[str]) -> None:
    parser = _operation_parser(
        "office-add",
        "Add (or update) another office in the address book. Lead only. "
        "The cooperation token is read from stdin and never accepted as an argument.",
    )
    parser.add_argument("name", help="local name for the other office")
    parser.add_argument("url", help="base url of its api door, e.g. https://office-b.example:8080")
    parser.add_argument("--participant", metavar="NAME",
                        help="the participant the cooperation was granted to (the 'as' value); "
                             "taken from the remote office when omitted (required with --no-verify)")
    parser.add_argument("--token-stdin", action="store_true", required=True,
                        help="read the cooperation token (coop.<id>.<secret>) from stdin")
    parser.add_argument("--allow-plaintext", action="store_true",
                        help="permit http:// to a non-loopback host (lab test beds only; "
                             "the token then travels in the clear)")
    parser.add_argument("--no-verify", action="store_true",
                        help="save without asking the remote office (GET /whoami) first; "
                             "for offline use. A wrong participant or token then only "
                             "shows on the first send")
    args = parser.parse_args(argv)

    name = _segment_arg(args.name, "office name")
    if args.participant is None and args.no_verify:
        raise OfficeError("--no-verify needs --participant: there is nothing to read it from")
    participant = _segment_arg(args.participant, "participant") if args.participant else None
    if participant in _FIXED_PARTICIPANTS:
        raise OfficeError(f"participant {participant!r} is reserved")
    url, plaintext = _validated_office_url(args.url, allow_plaintext=args.allow_plaintext)
    token = sys.stdin.read().strip()
    if not _COOP_TOKEN_RE.fullmatch(token):
        raise OfficeError("stdin did not carry a cooperation token of the form coop.<16 hex>.<43 chars>")

    r, pod, tenant, source = _context()
    _require_lead(r, pod=pod, tenant=tenant, source=source, command="office-add")
    # The relay daemon delivers remote replies into this office as the local
    # api participant `relay`. Enrol it if absent (same StartAgent mechanism as
    # coop-grant); refuse before any write if the name is taken by a non-api agent.
    relay_enrolled = is_member(r, pod=pod, tenant=tenant, agent=_RELAY_PARTICIPANT)
    if relay_enrolled and port_type(r, pod=pod, tenant=tenant, agent=_RELAY_PARTICIPANT) != "api":
        raise OfficeError(f"{_RELAY_PARTICIPANT!r} exists but is not an api participant; it is reserved for the relay")
    verified_as = None
    if not args.no_verify:
        link = _probe_office(url, token, participant)
        if link.state == "mismatch" and link.participant in _FIXED_PARTICIPANTS:
            raise OfficeError(f"nothing saved: the remote office reports the reserved participant {link.participant!r}")
        if link.state != "ok":
            raise OfficeError(f"nothing saved: {link.message}")
        if participant is None:
            participant = _segment_arg(link.participant, "participant reported by the remote office")
            if participant in _FIXED_PARTICIPANTS:
                raise OfficeError(f"nothing saved: the remote office reports the reserved participant {participant!r}")
        verified_as = link.office or "?"
    record = {"url": url, "participant": participant, "added_by": source, "added_ts": _now()}
    try:
        existed = r.hexists(_key("offices", pod, tenant), name)
        # Token first: the book never names an office whose token is missing.
        r.hset(_key("office_tokens", pod, tenant), name, token)
        r.hset(_key("offices", pod, tenant), name, json.dumps(record, separators=(",", ":")))
    except redis.RedisError as exc:
        raise OfficeError(f"cannot write the office address book: {exc}") from exc
    # Mark the relay as an inter-office speaker before it can send anything, so
    # its replies never open an "unanswered message" entry for the lead.
    _mark_interoffice(r, pod, tenant, _RELAY_PARTICIPANT, "relay")
    if not relay_enrolled:
        send(
            r, pod=pod, tenant=tenant, source=source, destination="host",
            payload={"agent": _RELAY_PARTICIPANT, "port_type": "api"},
            kind="StartAgent", module="office",
        )
    log_record("office", "office_added", source=source, destination=name, evidence=url)
    if plaintext:
        print(f"office: warning: {url} is plaintext http; the token travels in the clear",
              file=sys.stderr)
    print(f"{'updated' if existed else 'added'} office {name} -> {url} (as {participant})"
          + (f"; verified with the remote office ({verified_as})" if verified_as else "; NOT verified"))


def _offices_command(argv: list[str]) -> None:
    parser = _operation_parser("offices", "List the other offices in the address book (no secrets).")
    parser.add_argument("--check", action="store_true",
                        help="also ask each remote office (GET /whoami) whether the saved token "
                             "and participant still work; exits 1 if any link is not ok")
    args = parser.parse_args(argv)
    r, pod, tenant, _ = _context()
    try:
        raw = r.hgetall(_key("offices", pod, tenant))
    except redis.RedisError as exc:
        raise OfficeError(f"cannot read the office address book: {exc}") from exc
    if not raw:
        print("no offices configured")
        return
    bad = 0
    for name, value in sorted((_text(k), v) for k, v in raw.items()):
        try:
            rec = json.loads(_text(value))
            line = f"{name}\t{rec['url']}\tas {rec['participant']}\tadded by {rec.get('added_by', '?')}"
        except (json.JSONDecodeError, KeyError, TypeError):
            print(f"{name}\t(unreadable entry)" + ("\tlink: unreadable entry; re-add it" if args.check else ""))
            bad += 1 if args.check else 0
            continue
        if args.check:
            try:
                token = _text(r.hget(_key("office_tokens", pod, tenant), name))
            except redis.RedisError as exc:
                raise OfficeError(f"cannot read the office token: {exc}") from exc
            if not token:
                link = _Link("token_problem", "no token saved; the lead must re-add it")
            else:
                link = _probe_office(rec["url"], token, rec["participant"])
            if link.state != "ok":
                bad += 1
            label = {"ok": "ok", "mismatch": "participant mismatch",
                     "token_problem": "token problem", "unsupported": "cannot verify",
                     "unreachable": "unreachable", "error": "error"}[link.state]
            line += f"\tlink: {label}" + ("" if link.state == "ok" else f" -- {link.message}")
        print(line)
    if bad:
        raise OfficeError(f"{bad} office link(s) not ok")


def _office_rm_command(argv: list[str]) -> None:
    parser = _operation_parser("office-rm", "Remove another office from the address book. Lead only.")
    parser.add_argument("name")
    args = parser.parse_args(argv)
    name = _segment_arg(args.name, "office name")
    r, pod, tenant, source = _context()
    _require_lead(r, pod=pod, tenant=tenant, source=source, command="office-rm")
    try:
        removed = r.hdel(_key("offices", pod, tenant), name)
        r.hdel(_key("office_tokens", pod, tenant), name)
    except redis.RedisError as exc:
        raise OfficeError(f"cannot update the office address book: {exc}") from exc
    if not removed:
        raise OfficeError(f"unknown office {name!r}")
    log_record("office", "office_removed", source=source, destination=name)
    print(f"removed office {name}")


def _coop_tokens():
    try:
        return importlib.import_module("lib.coop_tokens")
    except ImportError as exc:
        raise OfficeError(
            "cooperation tokens are not available in this build (lib/coop_tokens is missing)"
        ) from exc


def _coop_grant_command(argv: list[str]) -> None:
    parser = _operation_parser(
        "coop-grant",
        "Grant another office a cooperation token for PARTICIPANT in this office. Lead only. "
        "The token is printed once and cannot be shown again.",
    )
    parser.add_argument("participant", help="name of the api participant the other office will send as")
    args = parser.parse_args(argv)
    participant = _segment_arg(args.participant, "participant")
    if participant in _FIXED_PARTICIPANTS:
        raise OfficeError(f"participant {participant!r} is reserved")
    coop = _coop_tokens()
    r, pod, tenant, source = _context()
    _require_lead(r, pod=pod, tenant=tenant, source=source, command="coop-grant")
    if is_member(r, pod=pod, tenant=tenant, agent=participant) and \
            port_type(r, pod=pod, tenant=tenant, agent=participant) != "api":
        raise OfficeError(f"{participant!r} is an existing non-api agent; pick another participant name")
    # Mark first so the participant's first message cannot open an "unanswered"
    # entry; then enrol first, mint second: a failure in between leaves an idle
    # api participant, never a live token for a participant that does not exist.
    _mark_interoffice(r, pod, tenant, participant, "coop")
    send(
        r, pod=pod, tenant=tenant, source=source, destination="host",
        payload={"agent": participant, "port_type": "api"},
        kind="StartAgent", module="office",
    )
    token_id, token = coop.grant(r, pod, tenant, participant, kinds=("Message",))
    log_record("office", "coop_granted", source=source, destination=participant, evidence=token_id)
    print(f"participant: {participant}")
    print(f"token-id: {token_id}")
    print(f"token: {token}")
    print("This token is shown once. Give it to the other office's lead "
          "(office-add ... --token-stdin); revoke with: office coop-revoke " + token_id,
          file=sys.stderr)


def _coop_revoke_command(argv: list[str]) -> None:
    parser = _operation_parser("coop-revoke", "Revoke a cooperation token immediately. Lead only.")
    parser.add_argument("token_id", metavar="TOKEN-ID", help="16-hex id from 'office coops'")
    args = parser.parse_args(argv)
    if not _COOP_TOKEN_ID_RE.fullmatch(args.token_id):
        raise OfficeError("token id must be 16 lowercase hex characters (the id, not the token)")
    coop = _coop_tokens()
    r, pod, tenant, source = _context()
    _require_lead(r, pod=pod, tenant=tenant, source=source, command="coop-revoke")
    coop.revoke(r, pod, tenant, args.token_id)
    log_record("office", "coop_revoked", source=source, evidence=args.token_id)
    print(f"revoked {args.token_id}")


def _coops_command(argv: list[str]) -> None:
    parser = _operation_parser("coops", "List cooperation tokens granted by this office (no secrets).")
    parser.parse_args(argv)
    coop = _coop_tokens()
    r, pod, tenant, _ = _context()
    rows = coop.list_tokens(r, pod, tenant)
    if not rows:
        print("no cooperation tokens")
        return
    # Whitelist what is printed: whatever else a record carries stays unprinted.
    for row in rows:
        kinds = ",".join(str(k) for k in (row.get("kinds") or ()))
        state = "revoked" if row.get("revoked") else "active"
        print(f"{row.get('token_id', '?')}\t{row.get('participant', '?')}\t{kinds}\t{state}\t"
              f"{row.get('created_ts', '?')}")



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
    (("doc",), "store a document once and refer to it by hash", _doc_command),
    (("broadcast",), "send a message to every peer agent", _broadcast_command),
    (("peers",), "list peer agents", _peers_command),
    (("contexts",), "list a claude_sdk agent's live memory contexts", _contexts_command),
    (("status",), "show agent presence and open work", _status_command),
    (("hire",), "enrol a new agent", lambda argv: _lifecycle_command("hire", argv)),
    (("run",), "open a non-addressable shell or command window", _run_command),
    (("let-go",), "retire an agent", lambda argv: _lifecycle_command("letGo", argv)),
    (("pause",), "pause an agent's CLI", lambda argv: _lifecycle_command("pause", argv)),
    (("resume",), "resume an agent's CLI and inbox", lambda argv: _lifecycle_command("resume", argv)),
    (("list",), "show a task board", _list_command),
    (("delivery",), "show where a sent message is (queued / delivered / failed)", _delivery_command),
    (("unresolved",), "show unresolved delivery outcomes", _unresolved_command),
    (("undeliverable",), "show messages not opened before retirement", _undeliverable_command),
    (("retired-inbox",), "show api agents' inbox content conserved at retirement", _retired_inbox_command),
    (("take", "unhold"), "take (or unhold) a todo or held task into doing", _take_command),
    (("done",), "finish your open task and record its outcome", _done_command),
    (("cancel",), "cancel your open task", _cancel_command),
    (("return",), "return your open task to todo", _return_command),
    (("show",), "read one ticket without changing it", _show_command),
    (("retitle",), "correct the title of your open task", _retitle_command),
    (("reopen",), "reopen one of your done or cancelled tasks", _reopen_command),
    (("note",), "append a progress note to one of your open tasks", _note_command),
    (("hold",), "pause (hold) an active or queued task; resume with take/unhold", _hold_command),
    (("delete",), "permanently remove a task from your own board", _delete_command),
    (("add",), "add a task to another agent's board", _add_command),
    (("purge",), "delete a retired agent's on-disk CLI session history", _purge_command),
    (("clone-to-all",), "clone a repository into agent workspaces", _clone_to_all_command),
    (("usage",), "show token usage and estimated cost", _usage_command),
    (("office-add",), "add another office to the address book (lead only)", _office_add_command),
    (("offices",), "list other offices in the address book", _offices_command),
    (("office-rm",), "remove another office from the address book (lead only)", _office_rm_command),
    (("coop-grant",), "grant another office a cooperation token (lead only)", _coop_grant_command),
    (("coop-revoke",), "revoke a cooperation token (lead only)", _coop_revoke_command),
    (("coops",), "list granted cooperation tokens", _coops_command),
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
        raise SystemExit(exc.exit_code) from exc


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
        # Flush inside the try: if a reader closed the pipe with output still
        # buffered, the BrokenPipeError surfaces here (and is caught below)
        # rather than from the interpreter's shutdown flush after main()
        # returns, where this handler could no longer catch it.
        sys.stdout.flush()
    except BrokenPipeError:
        # A downstream reader closed the pipe early (e.g. `office list | head`).
        # Exit quietly instead of letting the BrokenPipeError surface as a
        # traceback. Only for the real CLI (argv is None) do we also redirect
        # fd 1 to os.devnull, so the interpreter's shutdown flush cannot
        # re-raise on the dead pipe; in-process callers (the test suite always
        # passes argv) must never have fd 1 clobbered.
        if argv is None:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        raise SystemExit(0)
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
