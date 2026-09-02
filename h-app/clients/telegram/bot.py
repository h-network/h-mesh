"""Telegram bot client for h-mesh.

Talks to an h-mesh tenant REST API over HTTP, allowing users to interact with
the 'architect' agent via Telegram.
"""

import argparse
import asyncio
import base64
import html
import json
import logging
import os
import pathlib
import queue
import re
import ssl
import sys
import edge_tts
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, MutableMapping
from websockets.sync.client import connect as ws_connect

LOG_LEVEL_ENV_VAR = "H_MESH_LOG_LEVEL"
# The five standard names, plus the two stdlib aliases, so a deploy that writes
# the obvious WARN is not silently demoted to INFO for a spelling.
LOG_LEVEL_NAMES = ("CRITICAL", "FATAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG")

logger = logging.getLogger("mesh_telegram")


def _resolve_log_level(raw: str | None) -> int:
    """The threshold `H_MESH_LOG_LEVEL` asks for, INFO when it says nothing usable.

    Level NAMES only, case- and whitespace-insensitive; a numeric string is not
    a name and falls back like any other unrecognised value.

    ⚠ Never raises. This decides verbosity at import, before the bot can report
    anything at all, so a typo in a fresh VM's env must cost log detail and not
    the whole daemon.
    """
    name = (raw or "").strip().upper()
    return getattr(logging, name) if name in LOG_LEVEL_NAMES else logging.INFO


def _configure_logging(raw: str | None) -> int:
    """`basicConfig` at the requested threshold, returning what was applied."""
    level = _resolve_log_level(raw)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if (raw or "").strip() and (raw or "").strip().upper() not in LOG_LEVEL_NAMES:
        # Loud on purpose, and emitted at the fallback level so it survives it:
        # a mistyped DEGUB that quietly resolved to INFO would rebuild the exact
        # blind spot this knob exists to remove — someone believing they are
        # running at DEBUG while every debug line is still dropped on the floor.
        logger.warning(
            f"{LOG_LEVEL_ENV_VAR}={raw!r} is not a level name "
            f"({', '.join(LOG_LEVEL_NAMES)}); logging at INFO instead"
        )
    return level


_configure_logging(os.environ.get(LOG_LEVEL_ENV_VAR))


class MeshClient:
    """Thin REST client for h-mesh API based on API.md."""

    def __init__(self, base_url: str, token: str, app_name: str = "telegram",
                 ssl_context: "ssl.SSLContext | None" = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.app_name = app_name
        # ⚠ This context reaches the h-mesh door and nothing else. The Telegram
        # Bot API is a public host with a real certificate — weakening
        # verification there would be a different decision entirely, so
        # TelegramClient does not take one.
        self.ssl_context = ssl_context

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, data: dict | None = None) -> tuple[int, dict]:
        """⚠ The logging here is method, path, byte count and status code —
        never the body and never a header. The body carries chat text and the
        headers carry the bearer token; a request line is meant to answer "did
        this call happen, and what did the door say", which needs neither."""
        url = f"{self.base_url}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(url, data=body, headers=self._headers(), method=method)
        logger.debug(f"api -> {method} {path} ({len(body) if body else 0} bytes)")
        try:
            # context is ignored for http:// urls, so this needs no branch
            with urllib.request.urlopen(req, timeout=10, context=self.ssl_context) as resp:
                resp_body = resp.read().decode("utf-8")
                parsed = json.loads(resp_body) if resp_body else {}
                logger.debug(f"api <- {method} {path} {resp.status}")
                return resp.status, parsed
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8")
            try:
                parsed = json.loads(err_body)
            except Exception:
                parsed = {"detail": err_body}
            logger.debug(f"api <- {method} {path} {err.code}")
            return err.code, parsed
        except Exception as exc:
            # Transport failure, not an answer: the caller sees a synthesized
            # 500 that is indistinguishable from the door returning one, so
            # this is the only place the difference is recorded at all.
            logger.warning(f"api !! {method} {path} failed before any status: {exc}")
            return 500, {"detail": str(exc)}

    def enrol(self) -> tuple[int, dict]:
        """Enrol application client with host using StartAgent and port_type: api."""
        return self.request(
            "POST",
            "/agents/host/envelopes",
            {"kind": "StartAgent", "payload": {"agent": self.app_name, "port_type": "api"}},
        )

    def send_message(self, destination: str, text: str) -> tuple[int, dict]:
        """Send a text message envelope to an agent."""
        return self.request(
            "POST",
            f"/agents/{destination}/envelopes",
            {"text": text, "as": self.app_name},
        )

    def send_command(self, destination: str, text: str) -> tuple[int, dict]:
        """Send a Command-kind envelope. Unlike send_message's Message-kind
        shorthand, modules.tmux.port's command_opener pastes payload.text raw with
        a trailing newline and no "[message from X]" wrapper — so a native
        CLI slash command (e.g. Claude Code's /clear) is interpreted by the
        underlying CLI instead of read as chat text saying "/clear"."""
        return self.request(
            "POST",
            f"/agents/{destination}/envelopes",
            {"kind": "Command", "payload": {"text": text}, "as": self.app_name},
        )

    def send_attachment(
        self, destination: str, filename: str, mime_type: str, content_base64: str, caption: str | None = None
    ) -> tuple[int, dict]:
        """Send an Attachment-kind envelope (docs/CONTRACTS.md) — file bytes
        on the bus, not a path shared out of band. The api re-validates and
        re-checks the decoded size regardless of what this client already
        checked (a direct bus caller can bypass both the api and here)."""
        payload = {"filename": filename, "mime_type": mime_type, "content_base64": content_base64}
        if caption:
            payload["caption"] = caption
        return self.request(
            "POST",
            f"/agents/{destination}/envelopes",
            {"kind": "Attachment", "payload": payload, "as": self.app_name},
        )

    def get_presence(self, agent: str) -> tuple[int, dict]:
        """Get queue depths and presence state for an agent."""
        return self.request("GET", f"/agents/{agent}")

    def get_board(self, agent: str) -> tuple[int, dict]:
        """Get task board for an agent."""
        return self.request("GET", f"/agents/{agent}/board")

    def get_agents(self) -> tuple[int, dict]:
        """List every enrolled agent in the tenant roster (names only)."""
        return self.request("GET", "/agents")

    def get_all_boards(self) -> tuple[int, dict]:
        """Get task boards for every enrolled agent in one round-trip."""
        return self.request("GET", "/board")

    def add_ticket(self, agent: str, title: str, description: str = "", priority: str = "") -> tuple[int, dict]:
        """Add a ticket to an agent's board without interrupting them."""
        payload: dict = {"title": title}
        if description:
            payload["description"] = description
        if priority:
            payload["priority"] = priority
        return self.request(
            "POST",
            f"/agents/{agent}/envelopes",
            {"kind": "AddTicket", "payload": payload, "as": self.app_name},
        )

    def control_agent(self, kind: str, agent: str) -> tuple[int, dict]:
        """Send a PauseAgent/ResumeAgent lifecycle envelope, addressed to host."""
        return self.request(
            "POST",
            "/agents/host/envelopes",
            {"kind": kind, "payload": {"agent": agent}, "as": self.app_name},
        )

    def retire_agent(self, agent: str) -> tuple[int, dict]:
        """StopAgent: removes roster membership and identity state. Queues
        and boards are kept for a later re-hire — destructive to identity,
        not to work already recorded."""
        return self.request(
            "POST",
            "/agents/host/envelopes",
            {"kind": "StopAgent", "payload": {"agent": agent}, "as": self.app_name},
        )

    def hire_agent(self, agent: str, cli: str = "claude", profile: str | None = None,
                    provider: str | None = None) -> tuple[int, dict]:
        """StartAgent with port_type "tmux": a new terminal agent with its own
        window and CLI. Unlike StopAgent (retire), this is not destructive —
        no identity or queues are ever removed by hiring.

        ⚠ `profile` is validated server-side against the tenant's account
        registry (`available_profiles`, `control/openers.py`), which lists
        the valid accounts in its error if the name is wrong. There is no
        REST endpoint that exposes that registry ahead of time — `office
        profiles` reads Redis directly — so this client cannot offer a picker
        and doesn't pretend to; a bad profile name is a clear 422, not a
        guess. `provider` points the agent at a named local model endpoint
        (`AGENT_PROVIDERS`) — format-checked only, no registry to validate
        against either."""
        payload: dict = {"agent": agent, "port_type": "tmux", "cli": cli}
        if profile:
            payload["profile"] = profile
        if provider:
            payload["provider"] = provider
        return self.request(
            "POST",
            "/agents/host/envelopes",
            {"kind": "StartAgent", "payload": payload, "as": self.app_name},
        )

    def get_messages(self, after: str | None = None, limit: int = 100) -> tuple[int, dict]:
        """Catch-up poll mailbox messages for this client."""
        path = f"/agents/{self.app_name}/messages?limit={limit}"
        if after:
            path += f"&after={urllib.parse.quote(after)}"
        return self.request("GET", path)

    def poll_messages_forever(self, after: str | None = None, interval: float = 1.0):
        """Yield each new mailbox message for this client as it arrives,
        forever — a blocking generator wrapping repeated `get_messages`
        calls, so ReplyPusher can consume it exactly like AlertPusher
        consumes `stream_alerts`. Polling rather than SSE: `GET
        /agents/{client}/messages/stream` exists, but this client is a
        plain synchronous urllib caller with no long-lived-connection
        machinery beyond what `stream_alerts` already built for one
        endpoint — a second one wasn't worth it for a mailbox this low in
        volume. Errors are logged and retried on the same interval rather
        than raised, so one bad poll never kills the pushing thread.
        """
        cursor = after
        while True:
            code, data = self.get_messages(after=cursor)
            if code == 200:
                for msg in data.get("messages", []):
                    cursor = msg.get("cursor", cursor)
                    yield msg
            else:
                logger.warning(f"poll_messages_forever: GET /agents/{self.app_name}/messages failed: status={code}, body={data}")
            time.sleep(interval)

    def get_activity(self, agent: str, after: str | None = None, limit: int = 100) -> tuple[int, dict]:
        """Catch-up poll activity feed events for an agent."""
        path = f"/agents/{agent}/activity?limit={limit}"
        if after:
            path += f"&after={urllib.parse.quote(after)}"
        return self.request("GET", path)

    def get_alerts(self, after: str | None = None, limit: int = 100) -> tuple[int, dict]:
        """Catch-up poll watchdog alerts (blocked / stalled / credential —
        API.md's Watchdog Alerts Feed). ⚠ There is no "give me the tail"
        query: without `after`, this reads from the OLDEST stored alert, same
        as every other stream endpoint. A caller wanting "recent" must fetch
        with a large `limit` and take the tail itself (see TelegramBot's
        handle_alerts_command)."""
        path = f"/alerts?limit={limit}"
        if after:
            path += f"&after={urllib.parse.quote(after)}"
        return self.request("GET", path)

    def stream_alerts(self, after: str | None = None):
        """Yield alert dicts from GET /alerts/stream as they arrive.

        Blocking generator — never returns on its own — meant to run in its
        own thread. Reconnects with capped exponential backoff on any
        connection failure or stream-side `error` event, resuming from the
        last cursor seen so a reconnect does not replay what was already
        delivered.

        ⚠ Uses a finite socket timeout despite API.md §4a's "SSE heartbeats
        are not guaranteed, do not infer death from silence" — that warning
        is about not treating silence as a *logical* error (do not, say, tell
        a user "alerts are broken"). For a background reconnect loop the
        trade-off flips: periodically reconnecting an idle-but-healthy stream
        is harmless (cursor-based resume, no duplicates, no gap), while a
        socket that died without a FIN and is never noticed hangs this thread
        forever. Bounded timeout + resume is strictly safer here.
        """
        cursor = after
        backoff = 1.0
        while True:
            path = "/alerts/stream"
            if cursor:
                path += f"?after={urllib.parse.quote(cursor)}"
            req = urllib.request.Request(f"{self.base_url}{path}", headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=90, context=self.ssl_context) as resp:
                    backoff = 1.0
                    for event_type, event_id, data in _parse_sse_events(resp):
                        if event_id:
                            cursor = event_id
                        if event_type == "error" or data is None:
                            continue
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        if parsed.get("cursor"):
                            cursor = parsed["cursor"]
                        yield parsed
            except Exception as exc:
                logger.warning(f"alerts stream disconnected, retrying in {backoff:.0f}s: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def stream_activity(self, agent: str, after: str | None = None, heartbeat: bool = False):
        """Yield activity dicts from GET /agents/{agent}/activity/stream as they arrive.

        ⚠ With `heartbeat=True`, also yields None whenever the stream says
        "still here and still idle" — a keepalive frame, or a reconnect after
        a read timeout. A consumer with a deadline needs those: blocked in
        `next()` on a silent stream it cannot check the clock, and the only
        thing an idle agent produces is keepalives. Default False so the
        alerts consumer, which has no deadline, is unaffected.
        """
        cursor = after
        backoff = 1.0
        while True:
            path = f"/agents/{agent}/activity/stream"
            if cursor:
                path += f"?after={urllib.parse.quote(cursor)}"
            req = urllib.request.Request(f"{self.base_url}{path}", headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=90, context=self.ssl_context) as resp:
                    backoff = 1.0
                    for event_type, event_id, data in _parse_sse_events(resp):
                        if event_id:
                            cursor = event_id
                        if event_type == "error" or data is None:
                            if heartbeat:
                                yield None
                            continue
                        try:
                            parsed = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        if parsed.get("cursor"):
                            cursor = parsed["cursor"]
                        yield parsed
            except Exception as exc:
                logger.warning(f"activity stream for {agent} disconnected, retrying in {backoff:.0f}s: {exc}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _parse_sse_events(line_iter):
    """Parse raw SSE lines into `(event_type, id, data)` tuples, one per
    blank-line-terminated frame. Pure and network-free so it is directly unit
    testable; `stream_alerts` is the only network-touching caller."""
    event_type = None
    event_id = None
    data_lines: list[str] = []
    for raw_line in line_iter:
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        line = line.rstrip("\r\n")
        if line == "":
            if data_lines:
                yield event_type, event_id, "\n".join(data_lines)
            event_type, data_lines = None, []
            continue
        if line.startswith(":"):
            # ⚠ Reported, not swallowed. A keepalive comment is the ONLY thing
            # an idle stream sends, and a consumer that never sees one has no
            # opportunity to check its own deadline — which is exactly how the
            # activity watcher leaked threads against a quiet agent. Callers
            # that don't care filter it out; the one that does gets a tick.
            yield "keepalive", event_id, None
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
        elif line.startswith("id:"):
            event_id = line[len("id:"):].strip()


# Same rule and reserved set clients/web/ui/lifecycle.js enforces client-side
# for hire — kept identical so a name this bot refuses is refused everywhere,
# not just here. The api would refuse it too, but telling the user before the
# round trip is worth the duplication of one regex.
_AGENT_NAME = re.compile(r"^(?![0-9]+$)[a-z0-9][a-z0-9-]{0,62}$")
_RESERVED_AGENT_NAMES = {"all", "pod", "tenant", "agent"}

_ALERT_ICONS = {"blocked": "⊘", "stalled": "⏳", "credential": "🔑"}

# Telegram's own Bot API ceiling for getFile — a "photo" upload is always
# recompressed by Telegram well under this, so it is cheap insurance rather
# than an expected case, checked against PhotoSize's own reported file_size
# before downloading anything at all.
TELEGRAM_MAX_FILE_BYTES = 20 * 1024 * 1024

# docs/CONTRACTS.md's Attachment section — the strictly decoded content is
# capped at 10 MiB, smaller than TELEGRAM_MAX_FILE_BYTES above. A photo
# between 10 and 20MB downloads fine from Telegram but must still be
# refused as an Attachment, so this is checked separately and is not the
# same limit reused twice.
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024

# filename: non-empty UTF-8 basename, at most 255 UTF-8 bytes, not "." or
# "..", and none of "/", "\", NUL, another ASCII control character, or
# U+007F.
_ATTACHMENT_FILENAME_FORBIDDEN = re.compile(r"[/\\\x00-\x1f\x7f]")

# mime_type: at most 255 ASCII bytes, no parameters/whitespace/controls/wildcards.
_ATTACHMENT_MIME_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)


def _valid_attachment_filename(name: str) -> bool:
    if not name or name in (".", "..") or len(name.encode("utf-8")) > 255:
        return False
    return not _ATTACHMENT_FILENAME_FORBIDDEN.search(name)


def _valid_attachment_mime_type(value: str) -> bool:
    return bool(value) and len(value) <= 255 and bool(_ATTACHMENT_MIME_TYPE_RE.match(value))


# caption: at most 65,536 UTF-8 bytes (docs/CONTRACTS.md).
ATTACHMENT_MAX_CAPTION_BYTES = 65536

# "The payload is a closed shape ... no other field is accepted"
# (docs/CONTRACTS.md) — ReplyPusher._push_attachment enforces this the same
# as the api door and the tmux opener do, since it runs on envelopes a
# direct bus caller could have written with no api validation at all.
ATTACHMENT_ALLOWED_PAYLOAD_KEYS = frozenset({"filename", "mime_type", "content_base64", "caption"})

# A leading "@name rest of message" — a one-off destination override for
# just that one message, unlike 🎯 Message agent's handle_message_agent_pick
# which changes the persistent chat_target_agent. Deliberately anchored to
# the START of the text only: an "@word" anywhere else in the message is
# ordinary content, not a second routing directive — Slack-style inline
# mentions notify, they don't redirect an entire message, and this one
# does redirect, so it needs to be unambiguous about which text it applies
# to. That also settles "what about multiple @mentions" without a separate
# rule: only the first token position is ever inspected, so a later "@foo"
# in the body is just text. The name is captured loosely and lowercased
# (chat clients auto-capitalize) — handle_mention_prompt is what actually
# validates it against `_AGENT_NAME`/the roster.
_MENTION_RE = re.compile(r"^@([A-Za-z0-9-]{1,63})(?:[ \t]+(.*))?$", re.DOTALL)


def _parse_mention(text: str) -> tuple[str, str] | None:
    match = _MENTION_RE.match(text)
    if not match:
        return None
    return match.group(1).lower(), (match.group(2) or "").strip()


# `/run <agent> <command>` — a policy-reviewed exception to Command being
# "deliberately not exposed" (README §2a, web/SPEC.md §6). Unrestricted by
# default (empty allowlist), not bounded to a fixed set: this bot is
# already locked to one TELEGRAM_CHAT_ID (§1), and an agent already runs
# with permissions skipped, so whoever holds that chat already has the
# equivalent of a live terminal to it — an allowlist here would be
# restricting the same operator from themselves, not adding a boundary
# against anyone else. `/run` still requires typing an agent name and a
# command by hand (see handle_run_command for the one structural
# requirement that remains: single-line only). An operator who *does* want
# to bound this — a shared chat, a CLI whose commands need vetting, whatever
# the reason — sets --run-allowed-commands/RUN_ALLOWED_COMMANDS to a
# non-empty list; a non-empty allowlist is still enforced as an exact,
# whole-string match, same as before this default changed. That knob is
# global rather than per-CLI: the api exposes no field for which CLI an
# agent runs (same limitation PANE_WATCH_CHROME_OVERRIDES exists for).
DEFAULT_RUN_ALLOWED_COMMANDS: tuple[str, ...] = ()


def _parse_command_allowlist(spec: str) -> frozenset[str]:
    """Parse "/clear,/compact" — comma-separated, each entry stripped of
    surrounding whitespace, blank entries dropped."""
    return frozenset(item.strip() for item in spec.split(",") if item.strip())


def render_alert(alert: dict) -> str:
    """One-line rendering of a GET /alerts entry, shared by the on-demand
    Alerts menu and the live AlertPusher so the two never drift."""
    kind = alert.get("kind", "unknown")
    icon = _ALERT_ICONS.get(kind, "🔔")
    agent = alert.get("agent", "?")

    def _minutes(seconds) -> str:
        return f"{seconds // 60}m" if isinstance(seconds, int) else "unknown"

    if kind == "blocked":
        return f"{icon} blocked — {agent} — unconsumed {_minutes(alert.get('unconsumed_s'))}"
    if kind == "stalled":
        ticket = alert.get("ticket", "")
        return f"{icon} stalled — {agent} — \"{ticket}\" — doing {_minutes(alert.get('doing_age_s'))}"
    if kind == "credential":
        return f"{icon} credential — {alert.get('account', '?')}/{alert.get('cli', '?')} — {alert.get('status', '?')}"
    # Forward-compatible fallback for a kind this client does not know yet.
    details = {k: v for k, v in alert.items() if k not in ("v", "ts", "cursor", "kind")}
    return f"{icon} {kind} — {json.dumps(details)}"


# Matches the deployed convention (container/entrypoint.sh's own
# --cursor-file "~/.h-mesh/telegram.cursor.json") rather than a
# bare relative filename. A bare "cursor.json" default lands wherever CWD
# happens to be — including the repo root itself for an ad hoc local run
# with no --cursor-file, where it sits as an untracked file forever after.
DEFAULT_CURSOR_FILE = os.environ.get(
    "H_MESH_CURSOR_FILE",
    os.environ.get(
        "CURSOR_FILE",
        str(pathlib.Path(os.environ.get("H_MESH_STATE_DIR", str(pathlib.Path.home() / ".h-mesh"))) / "telegram.cursor.json")
    ),
)


class CursorStore:
    """Persists cursor to disk so bot restarts do not replay mailbox."""

    def __init__(self, filepath: str = DEFAULT_CURSOR_FILE):
        self.filepath = pathlib.Path(filepath)

    def load(self) -> str | None:
        if self.filepath.exists():
            try:
                data = json.loads(self.filepath.read_text(encoding="utf-8"))
                return data.get("cursor")
            except Exception as exc:
                logger.warning(f"Failed to load cursor from {self.filepath}: {exc}")
        return None

    def save(self, cursor: str | None) -> None:
        if not cursor:
            return
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            self.filepath.write_text(json.dumps({"cursor": cursor, "updated_at": time.time()}), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"Failed to save cursor to {self.filepath}: {exc}")


class AlertPusher:
    """Consumes GET /alerts/stream and pushes each new alert to a fixed
    Telegram chat as it happens — the point (per the ticket) is not having to
    be watching the pane or the menu to find out.

    ⚠ Only the three kinds `GET /alerts` documents ever arrive here: blocked,
    stalled, credential. `doing_duration` and `todo_duration` — the two
    lead-only alerts watchdog added — are pasted directly into the *lead's*
    tmux pane as an ordinary Message envelope (`modules.watchdog.service`
    `_notify_lead`) and never touch the alerts stream at all (confirmed by
    reading `_check_doing_duration`/`_check_todo_duration` against `_alert`).
    They are invisible to this client and to `GET /alerts` alike — there is
    currently no API surface that exposes them to anything but the lead's own
    pane.
    """

    def __init__(self, mesh: "MeshClient | None" = None, telegram=None, chat_id=None, cursor_store: CursorStore = None):
        self.mesh = mesh
        self.telegram = telegram
        self.chat_id = chat_id
        self.cursor_store = cursor_store

    def _seed_cursor(self) -> str | None:
        """On a fresh cursor store, start at the current tail rather than
        replay the whole retained history (up to 1000 alerts) as if every one
        were new — the same reasoning TelegramBot.enrol applies to mailboxes."""
        code, data = self.mesh.get_alerts(limit=1000)
        if code == 200 and data.get("next_cursor"):
            return data["next_cursor"]
        return None

    def run(self, stream_fn=None) -> None:
        """Blocking; run this in its own thread. `stream_fn` defaults to
        `self.mesh.stream_alerts` and is overridable so tests can inject a
        finite, network-free generator."""
        stream_fn = stream_fn or self.mesh.stream_alerts
        cursor = self.cursor_store.load()
        if cursor is None:
            cursor = self._seed_cursor()
            if cursor:
                self.cursor_store.save(cursor)
        for alert in stream_fn(after=cursor):
            cursor = alert.get("cursor", cursor)
            if cursor:
                self.cursor_store.save(cursor)
            if self.telegram:
                self.telegram.send_message(self.chat_id, render_alert(alert))


def render_reply(message: dict, fallback_source: str) -> str:
    """One-line rendering of a mailbox message for ReplyPusher."""
    source = message.get("l2", {}).get("source") or fallback_source
    payload = message.get("payload")
    text = payload.get("text") if isinstance(payload, dict) else None
    return f"{source}: {text}" if text else f"{source} sent a message"


DEFAULT_TTS_VOICE = "en-GB-RyanNeural"


def synthesize_speech(
    text: str,
    voice: str = DEFAULT_TTS_VOICE,
    output_path: str | pathlib.Path | None = None,
) -> str:
    """Render text to an MP3 file using edge-tts."""
    cleaned_text = text.strip()
    if not cleaned_text:
        raise ValueError("empty text for TTS synthesis")
    selected_voice = voice or DEFAULT_TTS_VOICE

    if output_path is None:
        fd, temp_path = tempfile.mkstemp(suffix=".mp3", prefix="mesh_tts_")
        os.close(fd)
        target_path = temp_path
    else:
        target_path = str(output_path)

    try:
        communicate = edge_tts.Communicate(cleaned_text, selected_voice)
        asyncio.run(communicate.save(target_path))
        return target_path
    except Exception:
        pathlib.Path(target_path).unlink(missing_ok=True)
        raise


class ReplyPusher:
    """Consumes this bot's own mailbox (GET /agents/{app_name}/messages) and
    pushes each new reply into a fixed Telegram chat as it arrives.

    Same shape as AlertPusher (seed cursor from the tail, run in its own
    thread, persist cursor as it goes) — polling instead of SSE since that's
    what `MeshClient.poll_messages_forever` wraps. This is what actually
    delivers a reply now: `handle_user_prompt` only posts and returns,
    matching the real fire-and-forget delivery model (`POST
    /agents/{agent}/envelopes` always returns 202 immediately; nothing in
    switch/port/api waits on anything). The old design had
    `handle_user_prompt` itself poll-and-wait inline, which blocked the
    entire polling loop for every chat while one reply was pending — measured
    live on the acceptance VM. This owns that job instead, independently.
    """

    def __init__(
        self,
        mesh: "MeshClient | None" = None,
        telegram=None,
        chat_id=None,
        cursor_store: CursorStore = None,
        tts_voice: str | None = None,
        voice_enabled: bool = False,
        voice_enabled_fn=None,
        activity_finalizer_fn=None,
    ):
        self.mesh = mesh
        self.telegram = telegram
        self.chat_id = chat_id
        self.cursor_store = cursor_store
        self.tts_voice = tts_voice or os.getenv("TTS_VOICE", DEFAULT_TTS_VOICE)
        self.voice_enabled = voice_enabled
        self.voice_enabled_fn = voice_enabled_fn
        self.activity_finalizer_fn = activity_finalizer_fn

    def _seed_cursor(self) -> str | None:
        """On a fresh cursor store, start at the current tail — a message
        that arrived before this process started is not an answer to
        anything sent through it; replaying it looks like lag at best and a
        stale, mismatched reply at worst."""
        cursor = None
        try:
            while True:
                code, data = self.mesh.get_messages(after=cursor, limit=1000)
                if code != 200:
                    break
                items = data.get("messages", [])
                if not items:
                    break
                cursor = data.get("next_cursor")
                if len(items) < 1000:
                    break
            return cursor
        except Exception as exc:
            logger.debug(f"Failed to seed mailbox cursor: {exc}")
        return None

    def run(self, stream_fn=None) -> None:
        """Blocking; run this in its own thread. `stream_fn` defaults to
        `self.mesh.poll_messages_forever` and is overridable so tests can
        inject a finite, network-free generator."""
        stream_fn = stream_fn or self.mesh.poll_messages_forever
        cursor = self.cursor_store.load()
        if cursor is None:
            cursor = self._seed_cursor()
            if cursor:
                self.cursor_store.save(cursor)
        for message in stream_fn(after=cursor):
            cursor = message.get("cursor", cursor)
            if cursor:
                self.cursor_store.save(cursor)
            if self.telegram:
                source = message.get("l2", {}).get("source")
                if self.activity_finalizer_fn and source:
                    self.activity_finalizer_fn(self.chat_id, source)

                if message.get("kind") == "Attachment":
                    # ⚠ Not a text reply — the api stores this kind's mailbox
                    # entry with content_base64 unchanged (docs/CONTRACTS.md),
                    # and render_reply only ever reads payload.text, so
                    # falling through to it here would render a useless
                    # "<agent> sent a message" instead of delivering the file.
                    self._push_attachment(message, source)
                    continue

                reply_text = render_reply(message, self.mesh.app_name)
                self.telegram.send_message(self.chat_id, reply_text)
                is_voice = (
                    self.voice_enabled_fn(self.chat_id)
                    if self.voice_enabled_fn
                    else self.voice_enabled
                )
                if is_voice:
                    msg_voice = (
                        message.get("payload", {}).get("voice")
                        if isinstance(message.get("payload"), dict)
                        else None
                    ) or self.tts_voice or DEFAULT_TTS_VOICE
                    # "record_voice", not the generic "typing" every other
                    # reply here uses -- edge_tts synthesis is a real network
                    # call, not instant, and this is what it actually is.
                    self.telegram.send_chat_action(self.chat_id, "record_voice")
                    voice_file = synthesize_speech(reply_text, msg_voice)
                    try:
                        self.telegram.send_voice(self.chat_id, voice_file)
                    finally:
                        pathlib.Path(voice_file).unlink(missing_ok=True)

    def _push_attachment(self, message: dict, source: str | None) -> None:
        """The other direction of the Attachment feature (docs/CONTRACTS.md):
        an agent's `office send-file` lands here as a mailbox entry with
        `kind: "Attachment"`, decoded and forwarded via `sendDocument`.

        ⚠ Re-validates the full contract independently, not just "is there
        something to send" — `docs/CONTRACTS.md` promises an api-type client
        "validates and decodes the same payload contract" the api door and
        the tmux opener do, the same defense-in-depth reasoning the opener's
        own re-check is built on: this envelope already passed the api
        door's validation in the normal flow, but a direct bus caller
        bypasses that door entirely and would otherwise reach this code
        with no real validation at all. Closed schema, filename basename
        rules, mime_type grammar, caption length and the decoded-size cap
        are all checked — reuses `_valid_attachment_filename`/
        `_valid_attachment_mime_type`/`ATTACHMENT_MAX_BYTES` from the send
        side (`handle_photo_message`) rather than a second copy of the same
        rules. A rejection is reported back to the chat, same as any other
        failure here — never silently dropped.
        """
        label = source or self.mesh.app_name

        def reject(reason: str) -> None:
            self.telegram.send_message(self.chat_id, f"{label} sent an attachment, but it was rejected: {reason}.")

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        extra_keys = sorted(set(payload.keys()) - ATTACHMENT_ALLOWED_PAYLOAD_KEYS)
        if extra_keys:
            reject(f"unexpected field(s) {', '.join(extra_keys)}")
            return

        filename = payload.get("filename")
        if not isinstance(filename, str) or not _valid_attachment_filename(filename):
            reject("invalid or missing filename")
            return

        mime_type = payload.get("mime_type")
        if not isinstance(mime_type, str) or not _valid_attachment_mime_type(mime_type):
            reject("invalid or missing mime_type")
            return

        content_b64 = payload.get("content_base64")
        if not isinstance(content_b64, str) or not content_b64:
            reject("missing content_base64")
            return

        caption_field = payload.get("caption")
        if caption_field is not None and (
            not isinstance(caption_field, str) or len(caption_field.encode("utf-8")) > ATTACHMENT_MAX_CAPTION_BYTES
        ):
            reject("invalid caption")
            return

        try:
            data = base64.b64decode(content_b64, validate=True)
        except (ValueError, TypeError) as exc:
            reject(f"content_base64 failed strict decoding ({exc})")
            return
        if len(data) > ATTACHMENT_MAX_BYTES:
            reject("decoded content exceeds the 10MB attachment limit")
            return

        caption = f"from {label}" + (f": {caption_field}" if caption_field else "")
        self.telegram.send_chat_action(self.chat_id, "upload_document")
        result = self.telegram.send_document(self.chat_id, filename, data, mime_type=mime_type, caption=caption)
        if not result.get("ok"):
            self.telegram.send_message(
                self.chat_id,
                f"Failed to deliver {label}'s attachment ({filename}): {result.get('description', 'unknown error')}",
            )


class TelegramClient:
    """Wrapper for Telegram Bot HTTP API."""

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def request(self, method: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/{method}"
        body = json.dumps(params).encode("utf-8") if params is not None else None
        headers = {"Content-Type": "application/json"}
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8")
            logger.error(f"Telegram API error {method}: {err.code} {err_body}")
            try:
                return json.loads(err_body)
            except Exception:
                return {"ok": False, "description": err_body}
        except Exception as exc:
            logger.error(f"Telegram request failed: {exc}")
            return {"ok": False, "description": str(exc)}

    def download_file(self, file_path: str) -> bytes | None:
        """GET the raw bytes of a file already resolved via getFile's
        `file_path` — a different base URL than every other call here
        (`api.telegram.org/file/bot<token>/...`, not `.../bot<token>/<method>`),
        Telegram's own split between the JSON API and file downloads."""
        url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as exc:
            logger.error(f"Telegram file download failed: {exc}")
            return None

    def request_multipart(
        self,
        method: str,
        fields: dict | None = None,
        files: dict | None = None,
    ) -> dict:
        url = f"{self.base_url}/{method}"
        boundary = f"----MeshTelegramBoundary{uuid.uuid4().hex}"
        body = bytearray()

        if fields:
            for name, val in fields.items():
                if val is None:
                    continue
                body.extend(f"--{boundary}\r\n".encode("utf-8"))
                body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
                body.extend(str(val).encode("utf-8"))
                body.extend(b"\r\n")

        if files:
            for name, (filename, data, content_type) in files.items():
                body.extend(f"--{boundary}\r\n".encode("utf-8"))
                body.extend(
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8")
                )
                body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
                body.extend(data)
                body.extend(b"\r\n")

        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        req = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            err_body = err.read().decode("utf-8")
            logger.error(f"Telegram API error {method}: {err.code} {err_body}")
            try:
                return json.loads(err_body)
            except Exception:
                return {"ok": False, "description": err_body}
        except Exception as exc:
            logger.error(f"Telegram multipart request failed: {exc}")
            return {"ok": False, "description": str(exc)}

    def send_voice(
        self,
        chat_id: int | str,
        voice: str | bytes | pathlib.Path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        """Send voice audio via Telegram sendVoice endpoint using multipart upload."""
        if isinstance(voice, (str, pathlib.Path)):
            voice_path = pathlib.Path(voice)
            filename = voice_path.name or "voice.mp3"
            with open(voice_path, "rb") as f:
                voice_data = f.read()
        else:
            filename = "voice.mp3"
            voice_data = bytes(voice)

        fields: dict = {"chat_id": chat_id}
        if caption:
            fields["caption"] = caption[:1024]
        if reply_to_message_id:
            fields["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            fields["reply_markup"] = json.dumps(reply_markup)

        files = {"voice": (filename, voice_data, "audio/mpeg")}
        return self.request_multipart("sendVoice", fields=fields, files=files)

    def send_document(
        self,
        chat_id: int | str,
        filename: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        caption: str | None = None,
    ) -> dict:
        """Send file bytes via Telegram's sendDocument endpoint — the agent
        -> Telegram direction of the Attachment feature (docs/CONTRACTS.md),
        same multipart upload shape send_voice already uses."""
        fields: dict = {"chat_id": chat_id}
        if caption:
            # Telegram's own caption limit (1024 chars) applies here
            # regardless of the bus's much larger one (65,536 UTF-8 bytes,
            # docs/CONTRACTS.md) — same truncation send_voice already does.
            fields["caption"] = caption[:1024]
        files = {"document": (filename, data, mime_type)}
        return self.request_multipart("sendDocument", fields=fields, files=files)

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> dict:
        data = {"chat_id": chat_id, "text": text}
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        if parse_mode is not None:
            data["parse_mode"] = parse_mode
        if disable_web_page_preview is not None:
            data["disable_web_page_preview"] = disable_web_page_preview
        return self.request("sendMessage", data)

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> dict:
        data = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        if parse_mode is not None:
            data["parse_mode"] = parse_mode
        if disable_web_page_preview is not None:
            data["disable_web_page_preview"] = disable_web_page_preview
        return self.request("editMessageText", data)

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> dict:
        return self.request("sendChatAction", {"chat_id": chat_id, "action": action})

    def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> dict:
        """Stop the inline button's loading spinner. Telegram expects one of
        these per callback_query within its own short timeout, regardless of
        whether the tap led to a visible reply. `show_alert` pops `text` as a
        real modal dialog instead of the small toast — for a tap that can't
        be honored at all (e.g. the agent it names is no longer enrolled),
        so the failure is impossible to miss the way a toast that overlaps
        the next screen edit could be."""
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        if show_alert:
            data["show_alert"] = True
        return self.request("answerCallbackQuery", data)

    def edit_message_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: dict | None = None) -> dict:
        """Change only a message's inline keyboard, leaving its text
        untouched — lighter than `edit_message_text` when nothing about the
        message's content actually changed, only what can still be tapped on
        it (e.g. clearing a just-tapped button before its action resolves,
        so a slow response can't be double-tapped)."""
        data = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        return self.request("editMessageReplyMarkup", data)

    def set_message_reaction(self, chat_id: int | str, message_id: int, emoji: str | None) -> dict:
        """Attach (or, with `emoji=None`, clear) a single emoji reaction on
        `message_id` — a lighter, persistent "seen and acted on" signal than
        `send_chat_action`'s few-second, easy-to-miss typing indicator,
        especially for a turn that can run far longer than typing ever
        would."""
        reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
        return self.request("setMessageReaction", {"chat_id": chat_id, "message_id": message_id, "reaction": reaction})

    def set_chat_menu_button(self, chat_id: int | str | None = None, menu_button: dict | None = None) -> dict:
        """Set the persistent button glued to the compose bar — distinct from
        both the sticky `ReplyKeyboardMarkup` menu and the `/` command list.
        `chat_id=None` sets the global default for every private chat;
        passing one scopes it to that chat only."""
        data: dict = {}
        if chat_id is not None:
            data["chat_id"] = chat_id
        if menu_button is not None:
            data["menu_button"] = menu_button
        return self.request("setChatMenuButton", data)

    def set_my_commands(self, commands: list[dict]) -> dict:
        """Register the bot's `/` command list with Telegram itself, so it
        shows up in the client's own command picker instead of requiring the
        user to know and type a command blind."""
        return self.request("setMyCommands", {"commands": commands})

    def get_updates(self, offset: int | None = None, timeout: int = 20) -> list[dict]:
        """⚠ getUpdates is per-BOT, not per-chat.

        Two processes polling one token compete for the same queue and each
        takes roughly half the updates — so running this against a token another
        bot is already using makes that bot drop messages, silently, for as long
        as this runs. Keep the window short, or use a token of your own.
        """
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        res = self.request("getUpdates", params)
        if res.get("ok"):
            return res.get("result", [])
        # ⚠ 409 means another process is polling this token. Telegram allows
        # exactly one getUpdates per bot, and the loser receives nothing —
        # forever, while looking perfectly healthy. Swallowing it cost an
        # afternoon: the bot was up, the log was quiet, and no message ever
        # arrived.
        if res.get("error_code") == 409:
            raise RuntimeError(
                "another instance is polling this bot token — stop it first "
                "(Telegram allows one getUpdates per bot)"
            )
        return []


class ChatTransactionError(RuntimeError):
    """Per-chat state was changed outside that chat's transaction, or a
    transaction was opened twice on one thread. Both are design errors, and
    both are raised immediately rather than left to become a race or a hang."""


class ChatLock:
    """One chat's transaction lock: a plain, NON-reentrant lock that knows
    which thread holds it.

    ⚠ Plain rather than `RLock`, deliberately. There is no nested acquisition
    in this client, and re-entrancy is not free future-proofing: it silently
    accepts a handler calling another handler that re-locks the same chat,
    which is a design error worth seeing. A bare `Lock` would expose it as a
    deadlock — a hang with no message — so this exposes it as
    `ChatTransactionError` naming the chat instead. Same error surfaced, an
    hour of debugging cheaper.
    """

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self._lock = threading.Lock()
        self._owner: int | None = None

    def held_by_current_thread(self) -> bool:
        return self._owner == threading.get_ident()

    def __enter__(self) -> "ChatLock":
        if self.held_by_current_thread():
            raise ChatTransactionError(
                f"chat {self.chat_id}: a transaction is already open on this thread. "
                "Pass the state you already read down to the callee instead of "
                "re-entering — nesting here means two callers each believe they own "
                "the chat."
            )
        self._lock.acquire()
        self._owner = threading.get_ident()
        return self

    def __exit__(self, *exc_info) -> None:
        self._owner = None
        self._lock.release()


class ChatWorker:
    """One chat, one thread, one queue: that chat's updates are handled in the
    order Telegram delivered them.

    ⚠ Mutual exclusion is NOT ordering, and this is the difference. A lock
    stops two updates changing the state at once; it says nothing about which
    goes first, because lock acquisition is not FIFO. Measured: answers
    arriving "sme-9" then "-" could be applied "-" first — rejected against
    stage=name — leaving the flow one step behind, with nothing crashed and a
    log that reads normally. `getUpdates` returns updates in order and this
    keeps them that way per chat.

    ⚠ Per chat, and still one thread each, so the reason updates were taken
    off the polling loop in the first place still holds: a chat waiting on a
    slow call delays only itself, never the poller and never another chat.

    A handler that raises is logged and the worker continues. Before this,
    each update owned a bare thread, so an exception killed that thread
    silently and the operator saw nothing at all. `BaseException` is
    deliberately not caught: KeyboardInterrupt and SystemExit are the process
    ending, and a worker that swallowed them would keep a dying bot alive.

    ⚠ LIFECYCLE. Workers are created on a chat's first AUTHORISED update
    (`submit_update` rejects before allocating, so unauthenticated traffic
    cannot spawn threads) and are never stopped. They are daemon threads, so
    the process exits without joining them.

    ⚠ There IS something to flush, and it is lost: an earlier version of this
    docstring said otherwise and was wrong. `run_polling` acknowledges an
    update to Telegram when it queues it, so anything still queued at process
    death is gone while Telegram considers it delivered — and so is the update
    currently BEING handled, which has already left the queue. `qsize()` reads
    zero for it. `BACKLOG_WARN` is a backlog signal only: it never fires for a
    single in-flight update, so "no warning" does not mean "nothing at risk".
    That boundary is argued and pinned in `run_polling`'s own docstring; what
    matters here is that the queue is not a durable buffer and must not be
    mistaken for one.

    ⚠ BACKPRESSURE. The queue is unbounded ON PURPOSE. The alternatives are
    dropping an operator's message (silent data loss, the failure this whole
    branch exists to stop) or blocking the polling loop (which freezes every
    chat, the failure that put updates on their own threads in the first
    place). Growth is bounded by how fast one operator can type; a chat stuck
    behind a slow call announces itself at BACKLOG_WARN queued updates rather
    than growing quietly.
    """

    BACKLOG_WARN = 20

    def __init__(self, chat_id: str, handler):
        self.chat_id = chat_id
        self._handler = handler
        self._queue: "queue.Queue" = queue.Queue()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"chat-worker-{chat_id}"
        )
        self._thread.start()

    def submit(self, update: dict) -> None:
        self._queue.put(update)
        depth = self._queue.qsize()
        if depth >= self.BACKLOG_WARN:
            # Visible rather than silent: a chat this far behind is either
            # being flooded or stuck on a slow call, and both look like "the
            # bot ignored me" from the chat.
            logger.warning(f"chat {self.chat_id}: {depth} updates queued behind the one in progress")

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until this chat's queue is drained. For tests and one-shot
        paths; the polling loop never waits."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    def _run(self) -> None:
        while True:
            update = self._queue.get()
            try:
                self._handler(update)
            except Exception as exc:
                logger.error(
                    f"chat {self.chat_id}: update {update.get('update_id')} handler raised: {exc}",
                    exc_info=True,
                )
            finally:
                self._queue.task_done()


class FrozenChatState(Mapping):
    """A read-only view of one chat's stored state.

    ⚠ Per-chat values are handed out frozen because the guard on `ChatDict`
    can only see writes THROUGH it: `state = pending.get(cid)` followed by
    `state["stage"] = ...` mutates live shared state with no transaction and
    no error. Freezing turns that into a refusal at the point of the write,
    which is the only place it can be caught. Advance a flow by writing a NEW
    state back inside the transaction — `pending[cid] = {**state, "stage": x}`.
    """

    __slots__ = ("_data",)

    def __init__(self, data):
        # ⚠ Deep, not shallow. A nested dict left mutable would be the same
        # hole one level down: `state["flow_args"]["stage"] = ...` bypasses the
        # guard exactly as `state["stage"] = ...` used to. Non-dict values
        # (renders, Events, Threads) are passed through untouched — they are
        # objects with their own thread-safety story, not state this class can
        # or should freeze.
        object.__setattr__(
            self, "_data", {k: _frozen(v) for k, v in dict(data).items()}
        )

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        return f"FrozenChatState({self._data!r})"

    def __setitem__(self, key, value):
        raise ChatTransactionError(
            "per-chat state is read-only once stored: mutating it in place would "
            "change what another thread is looking at, outside any transaction. "
            "Write a new state back instead — `pending[chat_id] = {**state, ...}` "
            "inside `with self.chat_txn(chat_id):`."
        )

    def __delitem__(self, key):
        self.__setitem__(key, None)


def _frozen(value):
    """Freeze dict values on the way in; leave everything else alone (renders,
    watch slots holding threads and events, plain flags)."""
    return FrozenChatState(value) if isinstance(value, (dict, FrozenChatState)) else value


class ChatDict(MutableMapping):
    """Per-chat state, keyed by chat_id with int and str keys normalized.

    ⚠ Every MUTATION must happen inside that chat's transaction
    (`TelegramBot.chat_txn`), and this refuses the write if it isn't.
    Deliberately a `MutableMapping` over a private dict rather than a `dict`
    subclass: `update`, `clear`, `popitem`, `setdefault`, `pop` and `|=` are
    all implemented by the ABC in terms of `__setitem__`/`__delitem__`, so
    there is ONE choke point and no mutation method that quietly bypasses it.
    A `dict` subclass had four such holes.

    ⚠ What this does NOT catch, stated because the previous version of this
    docstring over-claimed: a STALE READ. Reading state outside a transaction
    and writing it back inside one is accepted here and is still wrong — the
    container cannot see how old the value in your hand is. Ordering and
    read-write atomicity come from the per-chat worker and from holding the
    transaction across the whole read-then-write, not from this class.

    Reads are unguarded on purpose: a lone read is always safe, and values are
    handed out frozen so a read cannot become an untracked write.
    """

    def __init__(self, *args, guard=None, **kwargs):
        self._data: dict = {}
        self._guard = guard
        if args or kwargs:
            self._data.update({str(k): _frozen(v) for k, v in dict(*args, **kwargs).items()})

    def _k(self, key):
        return str(key)

    def _require_transaction(self, key) -> None:
        if self._guard is None:
            return
        if not self._guard(key):
            raise ChatTransactionError(
                f"per-chat state for {key} changed outside its transaction. "
                "Wrap the read-and-write in `with self.chat_txn(chat_id):` — "
                "another update for this chat can run on another thread and "
                "interleave between the two halves."
            )

    def __getitem__(self, key):
        return self._data[self._k(key)]

    def __setitem__(self, key, value):
        self._require_transaction(self._k(key))
        self._data[self._k(key)] = _frozen(value)

    def __delitem__(self, key):
        self._require_transaction(self._k(key))
        del self._data[self._k(key)]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return self._k(key) in self._data

    def get(self, key, default=None):
        return self._data.get(self._k(key), default)

    def __ior__(self, other):
        # MutableMapping does not define |=; without this, `state |= {...}`
        # raises TypeError rather than routing through the guard. Explicit is
        # better than a spelling that happens to be unavailable today.
        self.update(other)
        return self

    def __repr__(self):
        return f"ChatDict({self._data!r})"


def _parse_int_overrides(spec: str) -> dict[str, int]:
    """Parse "name=int,name2=int" — same exceptions-only shape as
    entrypoint.sh's AGENT_CLIS/AGENT_PROFILES, here for per-agent pane-watch
    chrome-height overrides (see PANE_WATCH_CHROME_OVERRIDES below)."""
    result: dict[str, int] = {}
    for pair in spec.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        try:
            result[name] = int(value.strip())
        except ValueError:
            continue
    return result


def _agent_picker_keyboard(
    agents: list[str], callback_prefix: str, *, back_callback: str = "menu", columns: int = 3
) -> dict:
    """The inline keyboard every agent-picker (add ticket, lifecycle, message
    target, watch) shows: one button per agent, `columns` to a row rather
    than one-per-row — ten-plus enrolled agents made a single column a long
    scroll (measured: an operator's own report). `◀ Back` always gets its
    own row at the end, never folded into the grid, so it stays a single
    predictable tap regardless of how many agents fill the rows above it.
    """
    cells = [{"text": agent, "callback_data": f"{callback_prefix}:{agent}"} for agent in agents]
    rows = [cells[i : i + columns] for i in range(0, len(cells), columns)]
    rows.append([{"text": "◀ Back", "callback_data": back_callback}])
    return {"inline_keyboard": rows}


_AGENT_CALLBACK_PREFIXES = ("wp:", "at:", "lc:", "lp:", "lr:", "lret:", "ta:")

# `selective: True` scopes the forced reply to whichever user tapped or was
# addressed, not everyone in the chat -- irrelevant for a bot locked to one
# chat_id today, but the correct flag regardless of who else could be in it.
FORCE_REPLY = {"force_reply": True, "selective": True}


def _callback_agent(data: str) -> str | None:
    """The agent name a callback references, for the prefixes that carry
    one — `None` for everything else (menu codes, `ap:<priority>`, the bare
    `lc`/`ws:<agent>` stop-watching tap, which needs no roster check)."""
    for prefix in _AGENT_CALLBACK_PREFIXES:
        if data.startswith(prefix):
            return data[len(prefix):]
    return None


def _derive_session_url(api_url: str, session_url: str = "") -> str:
    """Derive the session websocket URL (e.g. ws://localhost:8081/session)
    from --session-url or by replacing API URL port with 8081."""
    if session_url:
        u = urllib.parse.urlsplit(session_url)
        scheme = "wss" if u.scheme in ("https", "wss") else "ws"
        netloc = u.netloc or f"{u.hostname or '127.0.0.1'}:{u.port or 8081}"
        path = u.path if (u.path and u.path != "/") else "/session"
        return f"{scheme}://{netloc}{path}"
    u = urllib.parse.urlsplit(api_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    host = u.hostname or "127.0.0.1"
    port = 8081
    return f"{scheme}://{host}:{port}/session"


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    """Drop cursor-movement/clear and SGR colour codes from a session-door
    snapshot, leaving the plain text `capture-pane` rendered.

    Session snapshots (`LLD-session.md` §3) are `\\x1b[2J\\x1b[H` + the
    `capture-pane -e` lines (which carry colour SGR codes) + a cursor-position
    escape. None of that survives into a Telegram message — a terminal's
    color codes are noise there, not signal — and stripping is safe because
    `capture-pane`'s own line breaks (joined with `\\n` by `control.py`) are
    untouched by this regex.
    """
    return _ANSI_ESCAPE_RE.sub("", text)


# ⚠ These target a second kind of chrome the fixed `chrome_lines` crop can't
# reach: a CLI's own thinking-spinner and update-nag banner, which sit just
# ABOVE the structural input box/hint rows `chrome_lines` already crops, and
# are present or absent depending on CLI *state* (mid-turn, update shipped),
# not CLI identity — a bug ticket caught this leaking on both claude
# ("✻ Churned for 20s", "✔ Update installed · Restart to update") and codex
# ("Boogieing… (17s · ↓ 639 tokens)") panes. Filtered by content rather than
# a wider fixed crop because how many of these rows exist varies run to run;
# a fixed offset would either still leak them some of the time or crop real
# content away the rest of the time.
_UPDATE_BANNER_RE = re.compile(r"update (?:installed|available)|restart to update", re.IGNORECASE)
_TOKEN_COUNT_TAIL_RE = re.compile(r"\btokens?\)\s*$", re.IGNORECASE)
_DONE_TIMESTAMP_RE = re.compile(r"\bdone\s+\d{1,2}:\d{2}\s*[AP]M\b", re.IGNORECASE)
# claude's "<verb> for Ns[ Ns]" spinner line — anchored at the start and
# length-capped so a genuine reply that merely *mentions* a duration
# mid-sentence ("I'll wait for 5s before retrying, then...") isn't caught;
# a spinner line is always short and has nothing before "<verb> for Ns".
_ELAPSED_DURATION_RE = re.compile(r"^\s*(?:\S+\s+){1,2}for\s+\d+[ms](?:\s+\d+s)?\b", re.IGNORECASE)
_TRANSIENT_STATUS_MAX_LEN = 60


def _is_transient_chrome_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if _UPDATE_BANNER_RE.search(stripped) or _TOKEN_COUNT_TAIL_RE.search(stripped):
        return True
    if len(stripped) > _TRANSIENT_STATUS_MAX_LEN:
        return False
    return bool(_DONE_TIMESTAMP_RE.search(stripped) or _ELAPSED_DURATION_RE.match(stripped))


def _pane_tail_window(lines: list[str], *, chrome_lines: int, tail_span: int) -> list[str]:
    """The slice of `lines` a human actually wants to see: the last
    `tail_span` rows of the pane, minus the bottom `chrome_lines` (input box,
    shortcut hint, separators — present on claude/codex/agy alike, see
    `clients/telegram/README.md` §2c), then trimmed at either edge of blank
    rows and of the transient spinner/update-banner chrome
    `_is_transient_chrome_line` recognises, so a short reply doesn't render
    as mostly empty space or a "thinking…" line mistaken for content.
    """
    if chrome_lines < 0 or tail_span <= chrome_lines:
        raise ValueError("tail_span must be greater than chrome_lines")
    end = len(lines) - chrome_lines
    start = max(0, len(lines) - tail_span)
    window = list(lines[start:end]) if end > start else []
    while window and _is_transient_chrome_line(window[0]):
        window.pop(0)
    while window and _is_transient_chrome_line(window[-1]):
        window.pop()
    return window


class PaneWatchRender:
    """One refreshing Telegram message showing a live slice of an agent's
    tmux pane — the `/watch` counterpart to `ActivityRender`, holding pane
    text instead of a structured event list. Same send-once-then-edit,
    diff-skip, rate-limited flush shape, deliberately kept separate rather
    than folded into `ActivityRender`: the two render entirely different
    content and only accidentally share a flush loop.
    """

    MAX_LEN = 3800

    def __init__(self, chat_id: int | str, agent: str) -> None:
        self.chat_id = str(chat_id)
        self.agent = agent
        self.message_id: int | None = None
        self.completed: bool = False
        self.last_flush_ts: float = 0.0
        self.last_rendered_text: str | None = None
        self.lock = threading.Lock()

    def render(self, pane_lines: list[str], *, footer: str | None = None) -> str:
        header = f"👁 <b>Watching</b> (<code>{html.escape(self.agent)}</code>)"
        body = html.escape("\n".join(pane_lines)) if pane_lines else "<i>(no content in this window yet)</i>"
        lines = [header, "", f"<pre>{body}</pre>"]
        if footer:
            lines.append(footer)
        text = "\n".join(lines)
        if len(text) > self.MAX_LEN:
            text = text[: self.MAX_LEN - 20] + "\n…[truncated]"
        return text

    def flush(
        self,
        telegram_client,
        pane_lines: list[str],
        *,
        footer: str | None = None,
        reply_markup: dict | None = None,
        clear_markup: bool = False,
        force: bool = False,
    ) -> None:
        if not telegram_client:
            return
        with self.lock:
            now = time.time()
            if not force and self.message_id is not None and not self.completed and (now - self.last_flush_ts < 1.5):
                return
        text = self.render(pane_lines, footer=footer)
        with self.lock:
            if self.message_id is not None and not force and text == self.last_rendered_text:
                return
            self.last_flush_ts = now

        markup = {"inline_keyboard": []} if clear_markup else reply_markup
        try:
            if self.message_id is None:
                resp = telegram_client.send_message(
                    self.chat_id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup,
                )
                msg_id = resp.get("result", {}).get("message_id") if isinstance(resp, dict) else None
                with self.lock:
                    self.message_id = msg_id
                    self.last_rendered_text = text
            else:
                telegram_client.edit_message_text(
                    self.chat_id, self.message_id, text, parse_mode="HTML",
                    disable_web_page_preview=True, reply_markup=markup,
                )
                with self.lock:
                    self.last_rendered_text = text
        except Exception as exc:
            logger.debug(f"PaneWatchRender flush failed (chat={self.chat_id}, msg={self.message_id}): {exc}")


class ActivityRender:
    """Coalesces real-time agent execution events (input, tool, output)
    into a single live-updating Telegram message using editMessageText.
    """

    MAX_LEN = 3800

    def __init__(self, chat_id: int | str, agent: str) -> None:
        self.chat_id = str(chat_id)
        self.agent = agent
        self.events: list[dict] = []
        self.message_id: int | None = None
        self.completed: bool = False
        self.last_flush_ts: float = 0.0
        self.last_rendered_text: str | None = None
        self.lock = threading.Lock()

    def add_event(self, event: dict) -> None:
        with self.lock:
            self.events.append(event)

    def finalize(self) -> None:
        with self.lock:
            self.completed = True

    def render(self) -> str:
        with self.lock:
            header = f"🛠 <b>Activity</b> (<code>{html.escape(self.agent)}</code>)"
            if self.completed:
                header += f" · completed ({len(self.events)} steps)"
            lines = [header]
            total = len(self.events)
            display_events = self.events
            omitted = 0
            if total > 20:
                omitted = total - 20
                display_events = self.events[-20:]

            if omitted > 0:
                lines.append(f"<i>… {omitted} earlier steps omitted …</i>")

            start_idx = omitted + 1
            for i, ev in enumerate(display_events, start=start_idx):
                kind = ev.get("kind", "")
                is_latest = (i == total and not self.completed)
                glyph = "⏳" if is_latest else "✓"
                if kind == "input":
                    desc = "💬 <i>input received</i>"
                elif kind == "output":
                    desc = "✍️ <i>output produced</i>"
                    glyph = "✓"
                elif kind == "tool":
                    tool_name = ev.get("tool", "tool")
                    desc = f"<code>{html.escape(tool_name)}</code>"
                else:
                    desc = html.escape(str(kind or "event"))
                lines.append(f"{i}. {glyph} {desc}")

            text = "\n".join(lines)
            if len(text) > self.MAX_LEN:
                text = text[: self.MAX_LEN - 20] + "\n…[truncated]"
            return text

    def flush(self, telegram_client, force: bool = False) -> None:
        """Send initial message or edit existing message. Debounced/throttled."""
        if not telegram_client:
            return
        with self.lock:
            if not self.events and self.message_id is None:
                return
            now = time.time()
            if not force and self.message_id is not None and not self.completed and (now - self.last_flush_ts < 0.8):
                return

        text = self.render()
        with self.lock:
            if self.message_id is not None and text == self.last_rendered_text:
                return
            self.last_flush_ts = now

        try:
            if self.message_id is None:
                resp = telegram_client.send_message(
                    self.chat_id,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                msg_id = resp.get("result", {}).get("message_id") if isinstance(resp, dict) else None
                with self.lock:
                    self.message_id = msg_id
                    self.last_rendered_text = text
            else:
                telegram_client.edit_message_text(
                    self.chat_id,
                    self.message_id,
                    text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                with self.lock:
                    self.last_rendered_text = text
        except Exception as exc:
            logger.debug(f"ActivityRender flush failed (chat={self.chat_id}, msg={self.message_id}): {exc}")


class TelegramBot:
    """Coalesces activity tool calls into a single Telegram progress message."""

    def __init__(
        self,
        mesh_client: MeshClient | None = None,
        telegram_client: TelegramClient | None = None,
        cursor_store: CursorStore | None = None,
        target_agent: str = "architect",
        allowed_chat_id: int | str | None = None,
        default_tts_voice: str | None = None,
        voice_feature_enabled: bool | None = None,
        no_activity_push: bool = False,
        session_url: str | None = None,
        pane_watch_chrome_default: int = 4,
        pane_watch_chrome_overrides: dict[str, int] | None = None,
        pane_watch_tail_span: int = 12,
        pane_watch_refresh_s: float = 2.0,
        pane_watch_max_duration_s: float = 600.0,
        mini_app_url: str | None = None,
        run_allowed_commands: "frozenset[str] | None" = None,
    ):
        self.mesh = mesh_client
        self.telegram = telegram_client
        self.cursor_store = cursor_store
        self.target_agent = target_agent
        self.session_url = session_url or os.getenv("H_MESH_SESSION_URL", "")
        # A public HTTPS URL for clients/web/mini.html — Telegram's own
        # requirement for a web_app button, not this codebase's. Unset means
        # no Mini App has been published for this tenant, so the button is
        # simply absent (_sticky_keyboard) rather than opening a broken URL.
        self.mini_app_url = mini_app_url or os.getenv("MINI_APP_URL", "") or None
        # ⚠ Every real inbound update goes through _dispatch_update, which
        # checks this before touching anything. Without it, any Telegram user
        # who finds the bot could hire/retire/pause/resume/broadcast, not
        # just chat — the menu redesign made incoming text powerful enough
        # that "whoever messages first" is no longer an acceptable identity
        # check. See _chat_allowed for what happens when this is None.
        self.allowed_chat_id = str(allowed_chat_id) if allowed_chat_id is not None else None
        # ⚠ One transaction per chat, guarding every read-then-write on the
        # per-chat state below. run_polling gives each update its own thread
        # (it must — handling inline froze the whole bot), so two updates for
        # one chat run concurrently and every "look at the state, then change
        # it" sequence is a race. Declared first because every ChatDict below
        # is constructed with a guard that consults it. See chat_txn.
        self._chat_locks: dict[str, ChatLock] = {}
        self._chat_workers: dict[str, ChatWorker] = {}
        self._chat_locks_guard = threading.Lock()
        # Per-chat multi-step flows (AddTicket's title/description prompts,
        # Hire's name prompt). A chat with no entry here is not mid-flow, so a
        # plain text message from it is a prompt for its target agent, not an
        # answer to a menu.
        self.pending: dict = ChatDict(guard=self._holds_chat_txn)
        # Which agent a chat's plain-text prompts go to, if the operator has
        # picked one via 🎯 Message agent — falls back to target_agent
        # (--agent) when a chat has never picked one.
        self.chat_target_agent: dict = ChatDict(guard=self._holds_chat_txn)
        # Tenant-level feature flag for spoken TTS voice replies
        self.voice_feature_enabled = (
            (os.getenv("TELEGRAM_VOICE") == "1")
            if voice_feature_enabled is None
            else bool(voice_feature_enabled)
        )
        # Per-chat toggle for spoken TTS voice replies
        self.chat_voice_enabled: dict = ChatDict(guard=self._holds_chat_txn)
        self.default_tts_voice = default_tts_voice or os.getenv("TTS_VOICE", DEFAULT_TTS_VOICE)
        self.no_activity_push = (
            (os.getenv("NO_ACTIVITY_PUSH") == "1")
            if no_activity_push is False and os.getenv("NO_ACTIVITY_PUSH") is not None
            else bool(no_activity_push)
        )
        self.activity_renders: dict = ChatDict(guard=self._holds_chat_txn)
        # `/watch` — one live-tail per chat. `_pane_watches` holds the
        # running thread and its stop switch; a second /watch in the same
        # chat replaces whatever it finds there rather than stacking (§2c).
        self.pane_watch_chrome_default = pane_watch_chrome_default
        self.pane_watch_chrome_overrides = dict(pane_watch_chrome_overrides or {})
        self.pane_watch_tail_span = pane_watch_tail_span
        self.pane_watch_refresh_s = pane_watch_refresh_s
        self.pane_watch_max_duration_s = pane_watch_max_duration_s
        self.pane_watches: dict = ChatDict(guard=self._holds_chat_txn)
        # `/run <agent> <command>` — see DEFAULT_RUN_ALLOWED_COMMANDS for why
        # this is global rather than per-CLI.
        self.run_allowed_commands = (
            frozenset(run_allowed_commands)
            if run_allowed_commands is not None
            else frozenset(DEFAULT_RUN_ALLOWED_COMMANDS)
        )

    def chat_txn(self, chat_id: int | str) -> ChatLock:
        """The transaction for one chat's state. Hold it across any sequence
        that reads per-chat state and then writes it — and note that writing
        without it raises (`ChatDict._require_transaction`), so forgetting is
        an immediate error rather than a race.

        ⚠ Per CHAT, not one global lock: a global one would make a chat
        mid-hire stall every other chat's traffic, which is a worse behaviour
        than the race it closes. Chats never interact, so they never need to
        wait on each other.

        ⚠ Created under its own guard, because "get the lock, and make one if
        there isn't one" is itself the check-then-mutate this class of bug is
        about — two threads for a chat's first two updates would otherwise
        each build a lock and each hold a different one.

        ⚠ What "hold it across the network call" actually costs, since the
        trade-off is only assessable with real numbers: a flow step makes one
        mesh call (10s timeout) and typically one or two Telegram calls (30s
        each; 60s for a file download, 90s for TTS and session streams). So a
        chat's next update can wait tens of seconds in the worst case, not the
        10s an earlier version of this docstring claimed. That is the price of
        the stage you read still being the stage when you act on it. The lock
        is released on exceptions too — it is a context manager — so a failing
        step frees the chat rather than wedging it.
        """
        cid = str(chat_id)
        with self._chat_locks_guard:
            lock = self._chat_locks.get(cid)
            if lock is None:
                lock = self._chat_locks[cid] = ChatLock(cid)
            return lock

    def _holds_chat_txn(self, key: str) -> bool:
        """Guard for the ChatDict instances below. `key` is a chat id, or for
        `activity_renders` a "chat:agent" composite whose chat half is what
        the transaction is keyed on."""
        cid = str(key).split(":", 1)[0]
        with self._chat_locks_guard:
            lock = self._chat_locks.get(cid)
        return bool(lock and lock.held_by_current_thread())

    def is_voice_enabled(self, chat_id: int | str) -> bool:
        return self.voice_feature_enabled and self.chat_voice_enabled.get(str(chat_id), False)

    def _voice_label(self, chat_id: int | str) -> str:
        return "🔊 Voice: ON" if self.is_voice_enabled(str(chat_id)) else "🔇 Voice: OFF"

    def _target_for(self, chat_id: int | str) -> str:
        return self.chat_target_agent.get(str(chat_id), self.target_agent)

    def _chat_allowed(self, chat_id: int | str) -> bool:
        """⚠ No configured chat_id means refuse everything, not allow
        everything. The historical reason --chat-id/TELEGRAM_CHAT_ID exists
        (a bot can't start a conversation, so a known chat is supplied to
        drive one directly) was never a security boundary — but incoming
        messages now are one, and defaulting an unset allowlist to "open"
        would silently hand office control to whoever finds the bot first.
        Same call the codebase already makes elsewhere for exposure
        (SPEC-bundled-clients-and-exposure.md: "when nothing is published,
        publish nothing" — an explicit yes, not an absent no). In practice
        this only bites manual/ad-hoc invocations without --chat-id:
        setup.sh's normal flow requires both TELEGRAM_BOT_TOKEN and
        TELEGRAM_CHAT_ID before it enables the bot at all, so a real
        deployment always has one.
        """
        if self.allowed_chat_id is None:
            return False
        return str(chat_id) == self.allowed_chat_id

    def enrol(self, *, timeout_s: float = 60.0) -> bool:
        """Enrol with retry.

        ⚠ container/entrypoint.sh forks the api door and this bundled client
        within the same instant — `start`/`start_client` fork-and-move-on, with
        no wait for api's HTTP server to actually be listening yet. A single
        early attempt can lose that race: measured live on the acceptance VM,
        `enrol()` got `Connection refused`, logged it, and moved on — the bot
        then ran forever unenrolled, and every subsequent send failed with
        "invalid 'as' client: must be an enrolled client with port_type
        'api'", indistinguishable from a real misconfiguration. Retrying with
        backoff for up to `timeout_s` covers the race; re-enrolling an
        already-enrolled name is safe and idempotent (API.md), so retrying
        never does anything destructive.
        """
        deadline = time.time() + timeout_s
        backoff = 1.0
        while True:
            code, body = self.mesh.enrol()
            if code == 202:
                logger.info(f"Enrolled application '{self.mesh.app_name}': status={code}, body={body}")
                break
            if time.time() >= deadline:
                logger.error(
                    f"Failed to enrol '{self.mesh.app_name}' after {timeout_s:.0f}s "
                    f"(last status={code}, body={body}); sends will fail with "
                    f"\"invalid 'as' client\" until this succeeds."
                )
                return False
            logger.warning(f"Enrol attempt failed (status={code}, body={body}); retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 1.5, 10.0)

        # Register /menu, /status with Telegram itself so they show up in the
        # client's own "/" command list instead of requiring the user to know
        # and type them blind. Best-effort: a failure here does not affect
        # anything the bot actually does, only how discoverable it is.
        if self.telegram:
            res = self.telegram.set_my_commands([
                {"command": "menu", "description": "Open the office menu (overview, tickets, agents, alerts)"},
                {"command": "status", "description": f"Quick status check for {self.target_agent}"},
                {"command": "watch", "description": "Live-tail an agent's tmux pane (/watch <agent>)"},
                {"command": "unwatch", "description": "Stop this chat's active /watch"},
                {"command": "run", "description": "Run an allowed CLI slash command, no wrapper (/run <agent> <command>)"},
                {"command": "voice", "description": "Toggle spoken voice replies (TTS)"},
            ])
            if not res.get("ok", True):
                logger.warning(f"setMyCommands failed: {res}")

            # A one-tap launcher glued to the compose bar itself — distinct
            # from both the sticky keyboard and the "/" command list above.
            # Only when there's somewhere for it to go (MINI_APP_URL, same
            # "configured or omitted entirely" rule the sticky keyboard's
            # own 📊 Dashboard button follows) and someone specific to scope
            # it to (an unset allowed_chat_id would set this globally for
            # every private chat this token ever talks to, not just the one
            # this tenant is locked to).
            if self.mini_app_url and self.allowed_chat_id:
                menu_res = self.telegram.set_chat_menu_button(
                    chat_id=self.allowed_chat_id,
                    menu_button={"type": "web_app", "text": "Dashboard", "web_app": {"url": self.mini_app_url}},
                )
                if not menu_res.get("ok", True):
                    logger.warning(f"setChatMenuButton failed: {menu_res}")

        return True

    def handle_status_command(self, chat_id: int | str) -> str:
        agent = self._target_for(chat_id)
        code, presence_data = self.mesh.get_presence(agent)
        code_b, board_data = self.mesh.get_board(agent)

        if code != 200:
            text = f"❌ Unable to fetch status for {agent}: {presence_data.get('detail', 'error')}"
        else:
            pres = presence_data.get("presence", {})
            state = pres.get("state", "unknown")
            since = pres.get("since", "unknown")

            doing_tasks = board_data.get("doing", []) if code_b == 200 else []
            doing_str = "none"
            if doing_tasks:
                first = doing_tasks[0]
                doing_str = first.get("title", str(first)) if isinstance(first, dict) else str(first)

            text = (
                f"🤖 Agent Status: {agent}\n"
                f"State: {state} (since {since})\n"
                f"Doing: {doing_str}\n"
                f"Ingress depth: {presence_data.get('depths', {}).get('ingress', 0)}"
            )

        if self.telegram:
            self.telegram.send_message(chat_id, text)
        return text

    # ── sticky menu ──────────────────────────────────────────────────────────
    # The top-level menu is a persistent ReplyKeyboardMarkup — pinned at the
    # bottom of the chat across messages, rather than an inline keyboard
    # attached to one message that scrolls away. Its buttons are ordinary
    # text: tapping one sends its label back as a plain message (no
    # callback_query), so handle_text_message matches the label against
    # STICKY_LABELS before treating text as a prompt for target_agent.
    # Sub-flows one level down (agent pickers, pause/resume) stay inline —
    # contextual, one-shot choices tied to a specific message, which is what
    # inline keyboards are for; the sticky keyboard is for top-level nav that
    # should always be one tap away.
    #
    # ⚠ One button is dynamic: "🎯 Message: <agent>" shows the CURRENT target
    # for this chat and changes as that changes, so the keyboard is rebuilt
    # per-chat (_sticky_keyboard(chat_id)) rather than being one static
    # constant. It is matched by prefix, not exact text (see
    # handle_text_message), since its suffix varies — unlike the Sprint-Z
    # design this was modelled after, staleness here is harmless: tapping an
    # old render of this button always opens a fresh agent picker rather than
    # directly re-invoking a stored (function, args) pair, so there is
    # nothing for a stale label to get wrong.
    STICKY_TARGET_PREFIX = "🎯 Message: "
    STICKY_LABELS = {
        "📋 Overview": "ov",
        "🎫 Add ticket": "at",
        "⏯ Lifecycle": "lc",
        "👁 Watch": "wa",
        "🔔 Alerts": "al",
        "➕ Hire": "hi",
        "📢 Broadcast": "bc",
        "🙈 Hide menu": "hm",
    }

    def _sticky_keyboard(self, chat_id: int | str) -> dict:
        target_label = f"{self.STICKY_TARGET_PREFIX}{self._target_for(chat_id)}"
        last_row = ["📢 Broadcast"]
        if self.voice_feature_enabled:
            last_row.append(self._voice_label(chat_id))
        layout = [
            ["📋 Overview", "🎫 Add ticket"],
            ["⏯ Lifecycle", "👁 Watch"],
            ["🔔 Alerts", "➕ Hire"],
            [target_label],
            last_row,
            ["🙈 Hide menu"],
        ]
        keyboard = [[{"text": label} for label in row] for row in layout]
        if self.mini_app_url:
            # ⚠ web_app is a different button shape than every other sticky
            # button: tapping it opens the Mini App WebView directly,
            # client-side, and never arrives as a text message the way a tap
            # on any STICKY_LABELS button does (handle_text_message never
            # sees this one). Its own row, not folded into an existing one,
            # so it reads as a different kind of action, not just another
            # menu item. Only appears when configured (clients/web's
            # /mini.html + TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID auth path) --
            # no URL to open means no button, same "started only when
            # configured" rule the bot itself follows in entrypoint.sh.
            keyboard.append([{"text": "📊 Dashboard", "web_app": {"url": self.mini_app_url}}])
        return {
            "keyboard": keyboard,
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def handle_voice_toggle(self, chat_id: int | str) -> str:
        cid = str(chat_id)
        if not self.voice_feature_enabled:
            text = "Voice replies are not enabled for this tenant (set TELEGRAM_VOICE=1 in tenants/<tenant>/.env)."
            if self.telegram:
                self.telegram.send_message(cid, text)
            return text
        # Two taps racing produced two "enabled" replies and left it enabled,
        # where two sequential toggles end disabled. Read and flip together.
        with self.chat_txn(cid):
            new_state = not self.chat_voice_enabled.get(cid, False)
            self.chat_voice_enabled[cid] = new_state
        if new_state:
            voice_info = f" (voice: {self.default_tts_voice})" if self.default_tts_voice else ""
            text = f"🔊 Voice replies enabled for this chat{voice_info}."
        else:
            text = "🔇 Voice replies disabled for this chat."
        if self.telegram:
            self.telegram.send_message(cid, text, reply_markup=self._sticky_keyboard(cid))
        return text

    def _send_or_edit_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        message_id: int | None = None,
        reply_markup: dict | None = None,
        clear_markup: bool = False,
        force_reply: bool = False,
    ) -> int | None:
        """Edit `message_id` in place when the caller has one (an inline
        sub-flow screen, or a typed-reply continuation of one, being
        updated) — otherwise send a fresh message and return its id so a
        flow can anchor later steps to it. Same send-once/edit-after shape
        as `PaneWatchRender.flush`, applied per-tap instead of per-stream
        update. `reply_markup` here is always an inline keyboard or None —
        editMessageText cannot attach a ReplyKeyboardMarkup, only replace
        an existing inline one (`clear_markup` sends an empty one to drop
        stale buttons). `force_reply` only ever takes effect on a fresh
        send for the same reason: `ForceReply` is its own distinct
        reply_markup type, and editMessageText cannot attach one either —
        an edited prompt just goes without it rather than silently
        swallowing the flag."""
        if not self.telegram:
            return message_id
        if message_id is not None:
            markup = {"inline_keyboard": []} if clear_markup else reply_markup
            resp = self.telegram.edit_message_text(chat_id, message_id, text, reply_markup=markup)
            ok = isinstance(resp, dict) and resp.get("ok", False)
            if not ok:
                description = str(resp.get("description", "")) if isinstance(resp, dict) else ""
                if "not modified" in description:
                    return message_id
                # A real failure here — not "nothing changed" — used to be
                # silently swallowed at DEBUG with no fallback: the anchor
                # (retired VM's leftover message, one past Telegram's edit
                # window, deleted by the user, whatever) stayed permanently
                # broken and every tap on it produced literally nothing, no
                # updated screen, no new message, no error shown. Failing
                # closed like that is worse than a duplicate message, so
                # fall back to a fresh send and re-anchor to it instead.
                logger.warning(
                    f"editMessageText failed (chat={chat_id}, msg={message_id}): {description or resp!r}; "
                    "falling back to a fresh send instead of dropping the tap"
                )
                resp = self.telegram.send_message(chat_id, text, reply_markup=markup)
                return resp.get("result", {}).get("message_id") if isinstance(resp, dict) else None
            return message_id
        markup = FORCE_REPLY if force_reply else ({"inline_keyboard": []} if clear_markup else reply_markup)
        resp = self.telegram.send_message(chat_id, text, reply_markup=markup)
        return resp.get("result", {}).get("message_id") if isinstance(resp, dict) else None

    def _dispatch_menu_action(self, chat_id: int | str, code: str, message_id: int | None = None) -> str:
        """Shared by the sticky keyboard (text label tap, no message to
        edit) and any inline button still using these same short codes
        (e.g. a sub-flow's "◀ Back" — see handle_callback_query). `message_id`
        is only ever non-None for "lc", the one code currently reachable
        both ways; passed through uniformly regardless so a future inline
        entry point to any of these doesn't need this dispatcher touched."""
        if code == "menu":
            return self.handle_menu_command(chat_id)
        if code == "ov":
            return self.handle_overview_command(chat_id)
        if code == "at":
            return self.handle_addticket_start(chat_id, message_id)
        if code == "lc":
            return self.handle_lifecycle_start(chat_id, message_id)
        if code == "al":
            return self.handle_alerts_command(chat_id)
        if code == "hi":
            return self.handle_hire_start(chat_id, message_id)
        if code == "ta":
            return self.handle_message_agent_start(chat_id, message_id)
        if code == "wa":
            return self.handle_watch_start(chat_id, message_id)
        if code == "bc":
            return self.handle_broadcast_start(chat_id)
        if code == "vt":
            return self.handle_voice_toggle(chat_id)
        if code == "hm":
            return self.handle_hide_menu_command(chat_id)
        return ""

    def _tmux_agents(self) -> list[str]:
        """Enrolled agents with a terminal window — the ones a person can add a
        ticket to or pause/resume. Excludes api clients like this bot itself
        (LLD-office.md / clients/web's own port_type == "tmux" filter)."""
        code, data = self.mesh.get_agents()
        if code != 200:
            return []
        result = []
        for name in data.get("agents", []):
            pcode, pdata = self.mesh.get_presence(name)
            if pcode == 200 and pdata.get("port_type") == "tmux":
                result.append(name)
        return result

    def handle_menu_command(self, chat_id: int | str) -> str:
        text = "h-mesh menu — pinned below, always one tap away:"
        if self.telegram:
            self.telegram.send_message(chat_id, text, reply_markup=self._sticky_keyboard(chat_id))
        return text

    def handle_hide_menu_command(self, chat_id: int | str) -> str:
        """A persistent `ReplyKeyboardMarkup` (`is_persistent: true`, what
        `/menu` sends) cannot actually be dismissed from the phone —
        Telegram's own "collapse" gesture is a temporary panel toggle, and
        the keyboard comes back on the next refresh. The only real removal
        is the bot explicitly sending `reply_markup: {"remove_keyboard":
        true}` (`ReplyKeyboardRemove`) — nothing did that before this.
        `/menu` (unchanged) brings it straight back."""
        text = "Menu hidden. Send /menu to bring it back."
        if self.telegram:
            self.telegram.send_message(chat_id, text, reply_markup={"remove_keyboard": True})
        return text

    def handle_overview_command(self, chat_id: int | str) -> str:
        # Three calls, not one (API.md §4a): agent list, presence per agent,
        # boards in bulk. GET /agents/{agent} never carries the open ticket.
        agents = self._tmux_agents()
        board_code, board_data = self.mesh.get_all_boards()
        boards_by_agent = {}
        if board_code == 200:
            for entry in board_data.get("agents", []):
                boards_by_agent[entry.get("agent")] = entry

        icons = {"working": "●", "idle": "○", "blocked": "⊘", "unknown": "?"}
        lines = ["📋 Office overview"]
        if not agents:
            lines.append("No tmux agents enrolled.")
        for agent in agents:
            pcode, pdata = self.mesh.get_presence(agent)
            state = pdata.get("presence", {}).get("state", "unknown") if pcode == 200 else "unknown"
            doing = boards_by_agent.get(agent, {}).get("doing", [])
            ticket = "no open ticket"
            if doing:
                first = doing[0]
                ticket = first.get("title", str(first)) if isinstance(first, dict) else str(first)
            lines.append(f"{icons.get(state, '?')} {agent} — {state} — {ticket}")
        text = "\n".join(lines)
        if self.telegram:
            self.telegram.send_message(chat_id, text)
        return text

    def handle_addticket_start(self, chat_id: int | str, message_id: int | None = None) -> str:
        agents = self._tmux_agents()
        if not agents:
            text = "No agents enrolled to add a ticket to."
            self._send_or_edit_message(chat_id, text, message_id=message_id, clear_markup=True)
            return text
        text = "Add a ticket — pick an agent:"
        self._send_or_edit_message(chat_id, text, message_id=message_id, reply_markup=_agent_picker_keyboard(agents, "at"))
        return text

    def handle_addticket_pick_agent(self, chat_id: int | str, agent: str, message_id: int | None = None) -> str:
        cid = str(chat_id)
        text = f"Ticket title for {agent}? (/cancel to abort)"
        anchor_id = self._send_or_edit_message(cid, text, message_id=message_id, clear_markup=True)
        with self.chat_txn(cid):
            self.pending[cid] = {"flow": "addticket", "agent": agent, "stage": "title", "message_id": anchor_id}
        return text

    def handle_addticket_priority(self, chat_id: int | str, priority: str) -> str:
        cid = str(chat_id)
        # ⚠ Claim the flow inside the transaction. Two taps on the same
        # priority screen (a double tap, or the operator and a stale screen)
        # both read this state; without the claim one adds the ticket and the
        # other dies on `del` with KeyError, which in production is a dispatch
        # thread disappearing with nothing shown in the chat. Only the CLAIM
        # is held: add_ticket below can block for 10s and the flow is already
        # gone by then, so nothing is left for a second tap to race over.
        with self.chat_txn(cid):
            state = self.pending.get(cid)
            if not state or state.get("flow") != "addticket" or state.get("stage") != "priority":
                return ""
            agent, title = state["agent"], state["title"]
            description = state.get("description", "")
            anchor_id = state.get("message_id")
            del self.pending[cid]
        if self.telegram:
            self.telegram.send_chat_action(cid)
        code, resp = self.mesh.add_ticket(agent, title, description, priority)
        if code == 202:
            text = f"✅ Ticket added to {agent}: {title} [{priority}]"
        else:
            text = f"❌ Failed to add ticket: {resp.get('detail', 'error')}"
        self._send_or_edit_message(cid, text, message_id=anchor_id, clear_markup=True)
        return text

    def handle_lifecycle_start(self, chat_id: int | str, message_id: int | None = None) -> str:
        agents = self._tmux_agents()
        if not agents:
            text = "No agents enrolled."
            self._send_or_edit_message(chat_id, text, message_id=message_id, clear_markup=True)
            return text
        text = "Lifecycle — pick an agent:"
        self._send_or_edit_message(chat_id, text, message_id=message_id, reply_markup=_agent_picker_keyboard(agents, "lc"))
        return text

    def handle_lifecycle_pick_agent(self, chat_id: int | str, agent: str, message_id: int | None = None) -> str:
        buttons = [
            [
                {"text": "⏸ Pause", "callback_data": f"lp:{agent}"},
                {"text": "▶ Resume", "callback_data": f"lr:{agent}"},
            ],
            [{"text": "🗑 Retire", "callback_data": f"lret:{agent}"}],
            [{"text": "◀ Back", "callback_data": "lc"}],
        ]
        text = f"{agent} — pause, resume, or retire?"
        self._send_or_edit_message(chat_id, text, message_id=message_id, reply_markup={"inline_keyboard": buttons})
        return text

    def handle_lifecycle_control(self, chat_id: int | str, kind: str, agent: str, message_id: int | None = None) -> str:
        if message_id is not None and self.telegram:
            # Clear the tapped button the instant it's tapped, before the
            # (network) control call resolves -- editMessageReplyMarkup,
            # not editMessageText, since nothing about the message's text is
            # known yet and shouldn't change until the result is in. Mainly
            # guards against a double-tap on a slow response; a stale-tap
            # alert (see _callback_agent) already caught the case where the
            # agent is gone entirely, so this is only ever a live one.
            self.telegram.edit_message_reply_markup(chat_id, message_id, reply_markup={"inline_keyboard": []})
        if self.telegram:
            self.telegram.send_chat_action(chat_id)
        code, resp = self.mesh.control_agent(kind, agent)
        verb = "paused" if kind == "PauseAgent" else "resumed"
        if code == 202:
            text = f"✅ {agent} {verb}."
            # Undo is just the other lifecycle action, addressed at this
            # same agent -- Pause and Resume are both safe, idempotent-ish
            # calls, so there's no expiry to track or invalidate here.
            undo_data = f"lr:{agent}" if kind == "PauseAgent" else f"lp:{agent}"
            reply_markup = {
                "inline_keyboard": [[
                    {"text": "↩ Undo", "callback_data": undo_data},
                    {"text": "📋 Copy name", "copy_text": {"text": agent}},
                ]]
            }
            self._send_or_edit_message(chat_id, text, message_id=message_id, reply_markup=reply_markup)
        else:
            text = f"❌ Failed to {verb[:-1]} {agent}: {resp.get('detail', 'error')}"
            self._send_or_edit_message(chat_id, text, message_id=message_id, clear_markup=True)
        return text

    def handle_retire_start(self, chat_id: int | str, agent: str, message_id: int | None = None) -> str:
        # ⚠ Same confirm-by-typing-the-name pattern clients/web/ui/lifecycle.js
        # uses for retire, not a yes/no tap — StopAgent removes roster
        # membership and identity state (queues and boards are kept), and a
        # single misplaced tap is too cheap a way to do that.
        cid = str(chat_id)
        text = f"Type '{agent}' exactly to confirm retiring them (queues and boards are kept; /cancel to abort)."
        anchor_id = self._send_or_edit_message(cid, text, message_id=message_id, clear_markup=True)
        with self.chat_txn(cid):
            self.pending[cid] = {"flow": "retire", "agent": agent, "message_id": anchor_id}
        return text

    def handle_message_agent_start(self, chat_id: int | str, message_id: int | None = None) -> str:
        agents = self._tmux_agents()
        if not agents:
            text = "No agents enrolled to message."
            self._send_or_edit_message(chat_id, text, message_id=message_id, clear_markup=True)
            return text
        text = f"Currently messaging {self._target_for(chat_id)} — pick a different agent:"
        self._send_or_edit_message(chat_id, text, message_id=message_id, reply_markup=_agent_picker_keyboard(agents, "ta"))
        return text

    def handle_message_agent_pick(self, chat_id: int | str, agent: str, message_id: int | None = None) -> str:
        cid = str(chat_id)
        with self.chat_txn(cid):
            self.chat_target_agent[cid] = agent
        if message_id is not None:
            # Acknowledge the tap on the picker itself so its buttons don't
            # linger, stale and still-tappable, once a target is picked.
            # editMessageText can't carry the ReplyKeyboardMarkup refresh
            # below (edit only ever accepts an inline keyboard, never a
            # custom reply keyboard), so that part still has to be a fresh
            # message no matter what.
            self._send_or_edit_message(cid, f"🎯 Selected {agent}.", message_id=message_id, clear_markup=True)
        text = f"🎯 Now messaging {agent}. Send any message to reach them."
        if self.telegram:
            # Re-send the sticky keyboard too -- its "🎯 Message: ..." button
            # is stale the instant the target changes, and nothing else
            # would refresh it short of the user sending /menu again.
            self.telegram.send_message(cid, text, reply_markup=self._sticky_keyboard(cid))
        return text

    def _validate_mention_target(self, name: str) -> str | None:
        """`None` if `name` is a valid, existing tmux agent an `@mention` may
        route to; otherwise an error string ready to send back to the chat.
        Checked against the same name shape `hire` enforces
        (`_AGENT_NAME`/`_RESERVED_AGENT_NAMES`) and against the live roster
        (`_tmux_agents()`, so a real but non-tmux client — `telegram` itself,
        `host` — is refused the same as an unknown name, never silently
        misrouted or dead-lettered onto a mailbox/queue that can't paste it
        anywhere). Shared by the text `@mention` path (`handle_mention_prompt`)
        and the photo path (`handle_photo_message`) — one place either kind
        of `@mention` gets validated.
        """
        if not _AGENT_NAME.match(name) or name in _RESERVED_AGENT_NAMES or name not in self._tmux_agents():
            return f"@{name} isn't a known agent to message. Use /menu to see who's enrolled."
        return None

    def handle_mention_prompt(
        self, chat_id: int | str, name: str, rest: str, *, message_id: int | None = None
    ) -> str:
        """A leading "@name ..." — one-off destination override for this
        message only; `chat_target_agent` is untouched, so the next plain
        message still goes to whatever it was already set to."""
        cid = str(chat_id)
        if not rest:
            text = f"@{name} — nothing to send. Usage: @{name} your message"
            if self.telegram:
                self.telegram.send_message(cid, text)
            return text
        error = self._validate_mention_target(name)
        if error:
            if self.telegram:
                self.telegram.send_message(cid, error)
            return error
        return self.handle_user_prompt(cid, rest, agent_override=name, message_id=message_id)

    def handle_run_command(self, chat_id: int | str, rest: str, message_id: int | None = None) -> str:
        """`/run <agent> <command>` — raw, unwrapped pane injection: a
        Command-kind envelope instead of the Message-kind shorthand every
        other text path uses, so a native CLI slash command (e.g. Claude
        Code's `/clear`) is interpreted by the underlying CLI instead of
        read as chat text saying "/clear". One-off, same as `@mention` —
        `chat_target_agent` is never touched by this.

        ⚠ Unrestricted by default (`self.run_allowed_commands` empty unless
        `--run-allowed-commands`/`RUN_ALLOWED_COMMANDS` configures one) — see
        `DEFAULT_RUN_ALLOWED_COMMANDS` for why an allowlist isn't the default
        boundary here: this bot is already locked to one chat_id, and the
        agent it runs on already has permissions skipped, so there is no
        second party for a default allowlist to actually protect. When an
        allowlist *is* configured, it's an exact, whole-string match — not a
        prefix a caller can tack arguments onto.

        ⚠ Single-line only, checked unconditionally, allowlist configured or
        not. `command_opener` pastes the text with one trailing newline
        appended — a command's own text carrying an embedded newline would
        submit it as one line and then paste a second, completely
        independent line of raw input right after it, on delivery.

        ⚠ No separate operator/chat_id restriction here beyond the existing
        single `allowed_chat_id` gate every command already goes through
        (`_chat_allowed`) — there is no concept of distinct "operators"
        yet for one chat_id to be restricted relative to another. Revisit
        once that lands; not building speculative restriction infra for a
        model that doesn't exist yet.
        """
        cid = str(chat_id)

        def _reject(text: str) -> str:
            if self.telegram:
                self.telegram.send_message(cid, text)
            return text

        parts = rest.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            return _reject("Usage: /run <agent> <command>")
        name, command_text = parts[0].lower(), parts[1]
        error = self._validate_mention_target(name)
        if error:
            return _reject(error)
        if "\n" in command_text or "\r" in command_text:
            return _reject("/run commands must be a single line.")
        # An empty allowlist (the default) means unrestricted -- see
        # DEFAULT_RUN_ALLOWED_COMMANDS. Only a configured, non-empty one is
        # actually enforced.
        if self.run_allowed_commands and command_text not in self.run_allowed_commands:
            allowed = ", ".join(sorted(self.run_allowed_commands))
            return _reject(f"'{command_text}' isn't an allowed /run command. Allowed: {allowed}")
        return self.handle_user_prompt(cid, command_text, agent_override=name, raw=True, message_id=message_id)

    def handle_photo_message(self, chat_id: int | str, photo_sizes: list[dict], caption: str) -> str:
        """A photo update never carries `text` — `_dispatch_update`'s
        text-only early return would otherwise silently drop it, which is
        the bug this closes. `photo_sizes[-1]` is Telegram's own convention
        for "smallest to largest" — the best *compressed* version this bot
        ever sees; a "photo" upload is always recompressed by Telegram
        itself, full original quality is only available via a "document"
        upload instead (`handle_document_message`). Shared mechanics —
        routing, size checks, download, sending the `Attachment` envelope —
        live in `_send_incoming_file_as_attachment`.
        """
        if not photo_sizes:
            return ""
        largest = photo_sizes[-1]
        return self._send_incoming_file_as_attachment(
            chat_id, caption, largest.get("file_id"), largest.get("file_size"),
            mime_type="image/jpeg", filename=None, fallback_extension=".jpg", label="photo",
        )

    def handle_document_message(self, chat_id: int | str, document: dict, caption: str) -> str:
        """A "document" upload (send as file, not as photo) is Telegram's
        uncompressed path — `message.document` is a single object with its
        own `file_id`/`file_name`/`mime_type`, not an array of `PhotoSize`
        like `message.photo`, and not always JPEG. Shares every mechanic
        with `handle_photo_message` beyond that (routing, both size
        ceilings, download, sending the envelope) via
        `_send_incoming_file_as_attachment` rather than a second copy.
        """
        if not document:
            return ""
        return self._send_incoming_file_as_attachment(
            chat_id, caption, document.get("file_id"), document.get("file_size"),
            mime_type=document.get("mime_type") or "application/octet-stream",
            filename=document.get("file_name"), fallback_extension="", label="file",
        )

    def _send_incoming_file_as_attachment(
        self,
        chat_id: int | str,
        caption: str,
        file_id: str | None,
        reported_size: int | None,
        mime_type: str,
        filename: str | None,
        fallback_extension: str,
        label: str,
    ) -> str:
        """Shared by `handle_photo_message` and `handle_document_message`.
        Routes exactly like a text message: the caption is the message
        body, and an `@mention` prefix on it overrides the destination
        one-off, same as typed text (`_parse_mention`/
        `_validate_mention_target` — no second routing implementation).
        `filename` is Telegram's own name when it already has one (a
        "document" always does); `None` for a "photo", which only ever
        gives a `file_path` to derive one from once downloaded — either way
        the result is run through the same basename validation with the
        same generated-name fallback, never trusted outright. `mime_type`
        is validated the same way, falling back to
        `application/octet-stream` rather than rejecting a file whose
        content is fine but whose reported type isn't spec-shaped.

        Sends a real `Attachment` envelope (`docs/CONTRACTS.md`) — file
        bytes on the bus, `content_base64`, not a path shared out of band.
        The tmux opener owns writing it to
        `<recipient's workdir>/attachments/<stream_id>/` (see
        `lib.paths.get_agent_workdir`) and everything about that
        directory's lifecycle (confirmed with tmux directly); this
        method never touches a filesystem at all.
        """
        cid = str(chat_id)
        caption = (caption or "").strip()
        agent_override: str | None = None
        body = caption
        mention = _parse_mention(caption)
        if mention is not None:
            name, rest = mention
            error = self._validate_mention_target(name)
            if error:
                if self.telegram:
                    self.telegram.send_message(cid, error)
                return error
            agent_override = name
            body = rest

        agent = agent_override or self._target_for(cid)

        if not file_id or not self.telegram:
            return ""

        code, presence_data = self.mesh.get_presence(agent)
        state = presence_data.get("presence", {}).get("state") if code == 200 else "unknown"
        if state == "blocked":
            reply = f"{agent} is not accepting messages right now"
            self.telegram.send_message(cid, reply)
            return reply

        # ⚠ Two different ceilings, checked separately, not the same limit
        # twice: TELEGRAM_MAX_FILE_BYTES (20MB) is what Telegram will let a
        # bot download at all; ATTACHMENT_MAX_BYTES (10MB, docs/CONTRACTS.md)
        # is what the bus will accept as decoded Attachment content. A file
        # between the two downloads fine and must still be refused here.
        if reported_size and reported_size > TELEGRAM_MAX_FILE_BYTES:
            reply = f"That {label} is too large to fetch (Telegram's own 20MB bot download limit)."
            self.telegram.send_message(cid, reply)
            return reply

        self.telegram.send_chat_action(cid)

        info = self.telegram.request("getFile", {"file_id": file_id})
        file_path = info.get("result", {}).get("file_path") if info.get("ok") else None
        if not file_path:
            reply = f"Couldn't fetch that {label} from Telegram: {info.get('description', 'unknown error')}"
            self.telegram.send_message(cid, reply)
            return reply

        data = self.telegram.download_file(file_path)
        if not data:
            reply = f"Couldn't download that {label} from Telegram."
            self.telegram.send_message(cid, reply)
            return reply
        if len(data) > ATTACHMENT_MAX_BYTES:
            reply = f"That {label} is too large to send as an attachment (10MB limit)."
            self.telegram.send_message(cid, reply)
            return reply

        resolved_filename = filename or pathlib.Path(file_path).name
        if not _valid_attachment_filename(resolved_filename):
            resolved_filename = f"telegram-{label}-{int(time.time())}-{uuid.uuid4().hex[:8]}{fallback_extension}"
        resolved_mime_type = mime_type if _valid_attachment_mime_type(mime_type) else "application/octet-stream"

        code, resp = self.mesh.send_attachment(
            agent, resolved_filename, resolved_mime_type, base64.b64encode(data).decode("ascii"), caption=body or None,
        )
        if code != 202:
            reply = f"Failed to send that {label} to {agent}: {resp.get('detail', 'error')}"
            self.telegram.send_message(cid, reply)
            return reply

        reply = f"✅ {label.capitalize()} sent to {agent}."
        self.telegram.send_message(cid, reply)
        return reply

    # ── /watch — live-tail an agent's tmux pane ─────────────────────────────
    # One watch per chat (§2c, clients/telegram/README.md): picking a new
    # agent, or a new /watch of the same one, replaces whatever is already
    # running there rather than stacking watchers.

    def handle_watch_start(self, chat_id: int | str, message_id: int | None = None) -> str:
        agents = self._tmux_agents()
        if not agents:
            text = "No agents enrolled to watch."
            self._send_or_edit_message(chat_id, text, message_id=message_id, clear_markup=True)
            return text
        text = "Watch — pick an agent's live terminal:"
        self._send_or_edit_message(chat_id, text, message_id=message_id, reply_markup=_agent_picker_keyboard(agents, "wp"))
        return text

    def handle_watch_pick(self, chat_id: int | str, agent: str, message_id: int | None = None) -> str:
        cid = str(chat_id)
        if agent not in self._tmux_agents():
            text = f"Unknown agent: {agent}"
            self._send_or_edit_message(cid, text, message_id=message_id, clear_markup=True)
            return text
        text = f"👁 Watching {agent}…"
        # Acknowledge the tap on the picker itself, in place — the live tail
        # that follows is its own new message (PaneWatchRender), a separate,
        # high-frequency channel this one shouldn't be merged into.
        self._send_or_edit_message(cid, text, message_id=message_id, clear_markup=True)
        render = PaneWatchRender(cid, agent)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run_pane_watch,
            args=(cid, agent, render, stop_event),
            daemon=True,
            name=f"pane-watch-{agent}",
        )
        # ⚠ STOP AND INSTALL ARE ONE POLICY, not two steps. Stopping outside
        # the transaction let two picks each see no current watch, each
        # install, and each start a thread: both watchers live, one of them
        # untracked, its stop_event unreachable, and no way to stop it through
        # the bot at all. Replacing is therefore a single guarded step — read
        # the incumbent, signal it, put this one in its place — and the
        # incumbent's own thread cleans up after itself via the
        # compare-and-pop in _run_pane_watch, which is why signalling under
        # the transaction is enough and joining here is not needed.
        with self.chat_txn(cid):
            incumbent = self.pane_watches.get(cid)
            if incumbent:
                incumbent["stop_event"].set()
            self.pane_watches[cid] = {
                "agent": agent, "stop_event": stop_event, "render": render, "thread": thread,
            }
        thread.start()
        return text

    def _stop_pane_watch(self, chat_id: int | str) -> str | None:
        """Signal any watch running in this chat to end. Does not join — the
        watcher thread sends its own final message (§ below) and clears
        itself out of `pane_watches` on the way out.

        ⚠ Read and signal inside the transaction, so this cannot signal a
        watch that a concurrent `handle_watch_pick` has already replaced.
        `handle_watch_pick` does NOT call this — replacing is one guarded
        step there, and calling in would be a nested transaction, which
        `ChatLock` refuses by design."""
        cid = str(chat_id)
        with self.chat_txn(cid):
            state = self.pane_watches.get(cid)
            if not state:
                return None
            state["stop_event"].set()
            return state["agent"]

    def handle_watch_stop(self, chat_id: int | str, agent: str) -> str:
        """The inline "⏹ Stop watching" button on the live-tail message
        itself. `answer_callback_query` (called generically for every
        callback) is the only acknowledgement — the watched message updating
        to its final state a moment later is the real feedback."""
        state = self.pane_watches.get(str(chat_id))
        if not state or state["agent"] != agent:
            return ""
        state["stop_event"].set()
        return f"stopping watch on {agent}"

    def handle_watch_stop_command(self, chat_id: int | str) -> str:
        cid = str(chat_id)
        agent = self._stop_pane_watch(cid)
        text = f"⏹ Stopping watch on {agent}…" if agent else "No active watch in this chat."
        if self.telegram:
            self.telegram.send_message(cid, text)
        return text

    def handle_hire_start(self, chat_id: int | str, message_id: int | None = None) -> str:
        cid = str(chat_id)
        text = "New agent's name? (lowercase letters, digits, hyphens; not all digits; /cancel to abort)"
        # ⚠ `message_id` is accepted and deliberately NOT edited. Every menu
        # entry point passes the tapped message's id uniformly
        # (_dispatch_menu_action), and passing it through here quietly cost
        # the ForceReply: `_send_or_edit_message` takes the edit branch
        # whenever it has an id, and force_reply only ever applies to a fresh
        # send (see its docstring — editMessageText cannot attach a
        # ForceReply at all). The comment this replaces claimed Hire was
        # "a fresh send in every real invocation"; the inline button
        # (handle_callback_query -> "hi") is the path the operator actually
        # uses and it always has an id, so the prompt that most needs the
        # compose box opened on it was the one prompt never getting it.
        # A fresh send every time; the menu message stays where it was.
        anchor_id = self._send_or_edit_message(cid, text, force_reply=True)
        with self.chat_txn(cid):
            self.pending[cid] = {"flow": "hire", "stage": "name", "message_id": anchor_id}
        return text

    def handle_broadcast_start(self, chat_id: int | str) -> str:
        cid = str(chat_id)
        with self.chat_txn(cid):
            self.pending[cid] = {"flow": "broadcast"}
        text = "Broadcast to every agent — type the message, or /cancel to abort."
        if self.telegram:
            self.telegram.send_message(cid, text)
        return text

    def handle_alerts_command(self, chat_id: int | str, limit: int = 10) -> str:
        # GET /alerts has no "give me the tail" query (see MeshClient.get_alerts);
        # fetch up to the stream's own retention cap and slice the tail here.
        code, data = self.mesh.get_alerts(limit=1000)
        if code != 200:
            text = f"❌ Unable to fetch alerts: {data.get('detail', 'error')}"
        else:
            alerts = data.get("alerts", [])[-limit:]
            if not alerts:
                text = "🔔 No alerts."
            else:
                text = "\n".join(["🔔 Recent alerts"] + [render_alert(a) for a in alerts])
        if self.telegram:
            self.telegram.send_message(chat_id, text)
        return text

    def handle_pending_text(self, chat_id: int | str, text: str) -> str | None:
        """Consume `text` as an answer to a pending flow's prompt. Returns None
        (leaving `text` untouched by the caller) when the chat has no flow open.

        ⚠ This wrapper exists to make the flow observable and nothing else —
        `_advance_pending_flow` below is the flow. A multi-step flow that
        "just stops" is undiagnosable without knowing which stage consumed
        which answer and where the anchor went, and that is exactly the state
        this logs: stage before, stage after, anchor before, anchor after.
        The answer's LENGTH, never its text: the stage names say what the
        operator was asked, which is what a log needs to know.

        ⚠ THE WHOLE STEP RUNS UNDER THIS CHAT'S TRANSACTION, network calls
        included, and deliberately so: the next answer must not be accepted
        before the prompt it answers is on screen. Claim-and-release would be
        safe only with an explicit in-progress state that queues or rejects
        input arriving mid-step.

        ⚠ What that costs the chat, with real numbers rather than the 10s an
        earlier version of this docstring claimed: one mesh call (10s timeout)
        plus typically one or two Telegram calls (30s each; 60s for a file
        download, 90s for TTS and the session stream), so tens of seconds in
        the worst case. Other chats are unaffected — the transaction is per
        chat, and so is the worker that feeds it. The transaction is released
        on exceptions, so a failing step frees the chat rather than wedging
        it."""
        cid = str(chat_id)
        with self.chat_txn(cid):
            before = self.pending.get(cid)
            if not before:
                logger.debug(f"pending: chat={cid} has no open flow ({len(text.strip())} chars fall through)")
                return None
            flow = before.get("flow")
            stage_before, anchor_before = before.get("stage"), before.get("message_id")
            logger.info(
                f"flow {flow}: chat={cid} stage={stage_before} anchor={anchor_before} "
                f"consuming an answer of {len(text.strip())} chars"
            )
            reply = self._advance_pending_flow(cid, text)
            after = self.pending.get(cid)
            if after is None:
                logger.info(f"flow {flow}: chat={cid} closed after stage={stage_before}")
            else:
                logger.info(
                    f"flow {flow}: chat={cid} stage {stage_before} -> {after.get('stage')}, "
                    f"anchor {anchor_before} -> {after.get('message_id')}"
                )
            return reply

    def _send_typed_answer_reply(
        self, chat_id: int | str, text: str, *, reply_markup: dict | None = None
    ) -> int | None:
        """A flow's response to something the operator TYPED: always a fresh
        send, never an edit, and the new message id becomes the flow's anchor.

        ⚠ An edit never notifies. The moment the operator types, their own
        message is the newest thing in the chat, so a prompt edited in place
        lands ABOVE it unannounced — indistinguishable, from the chat, from
        the flow having died. That is what "the Hire button does nothing"
        looked like. Edit-in-place remains correct for a screen answered by a
        BUTTON, where the operator is looking at the message they just
        tapped: this narrows that decision to that case rather than
        reversing it."""
        return self._send_or_edit_message(chat_id, text, reply_markup=reply_markup)

    def _advance(self, cid: str, state, **changes) -> None:
        """Write a flow's next state back. Stored state is frozen
        (`FrozenChatState`), so a step cannot advance by mutating what it read
        — it builds the successor and stores it, inside the transaction the
        caller already holds."""
        self.pending[cid] = {**dict(state), **changes}

    def _advance_pending_flow(self, cid: str, text: str) -> str | None:
        """One step of an open flow. Called only by `handle_pending_text`,
        which owns the logging around it; `cid` is already normalised and the
        chat is known to have a flow open.

        ⚠ Everything this function answers was TYPED — that is what having a
        pending flow means — so every reply below goes out through
        `_send_typed_answer_reply` and none of them edit. `message_id` in the
        pending state is still the flow's anchor, but it now means "the last
        message this flow posted" (what a button-answered screen edits, e.g.
        addticket's priority buttons) rather than "the one message the whole
        flow edits in place"."""
        state = self.pending.get(cid)
        if not state:
            return None
        anchor_id = state.get("message_id")
        if text.strip() == "/cancel":
            del self.pending[cid]
            reply = "Cancelled."
            # Fresh send so the operator actually sees it, plus a best-effort
            # clear of whatever the last screen was: addticket's priority
            # screen carries live buttons, and a cancelled flow must not stay
            # tappable. Cosmetic, so a failure here changes nothing.
            self._send_typed_answer_reply(cid, reply)
            if anchor_id is not None and self.telegram:
                self.telegram.edit_message_reply_markup(cid, anchor_id, {"inline_keyboard": []})
            return reply

        if state["flow"] == "addticket":
            if state["stage"] == "title":
                reply = "Description? (send - to skip, /cancel to abort)"
                self._advance(cid, state, title=text.strip(), stage="description",
                              message_id=self._send_typed_answer_reply(cid, reply))
                return reply
            if state["stage"] == "description":
                buttons = [[
                    {"text": "🔵 Low", "callback_data": "ap:low"},
                    {"text": "⚪ Normal", "callback_data": "ap:normal"},
                    {"text": "🔴 High", "callback_data": "ap:high"},
                ]]
                reply = "Priority?"
                self._advance(
                    cid, state,
                    description="" if text.strip() == "-" else text.strip(),
                    stage="priority",
                    message_id=self._send_typed_answer_reply(
                        cid, reply, reply_markup={"inline_keyboard": buttons}
                    ),
                )
                return reply
            # stage == "priority": this step is answered by tapping a button
            # (handle_addticket_priority), not typed text — stray text here
            # just gets pointed back at the buttons rather than silently lost.
            reply = "Tap a priority button above, or /cancel."
            self._advance(cid, state, message_id=self._send_typed_answer_reply(cid, reply))
            return reply

        if state["flow"] == "hire":
            if state["stage"] == "name":
                name = text.strip()
                if not _AGENT_NAME.match(name) or name in _RESERVED_AGENT_NAMES:
                    reply = "That name won't work — lowercase letters, digits and hyphens, not all digits, not a reserved word. Try again, or /cancel."
                    self._advance(cid, state, message_id=self._send_typed_answer_reply(cid, reply))
                    return reply  # stay in "name" stage; do not consume the pending flow
                # ⚠ No picker: office profiles reads Redis directly and has no
                # REST equivalent, so this client cannot list valid accounts
                # ahead of time (see MeshClient.hire_agent). A bad name still
                # gets a clear error, listing the valid ones, from the api.
                reply = f"Profile for {name}? (account/profile name, or - for the default; /cancel to abort)"
                self._advance(cid, state, name=name, stage="profile",
                              message_id=self._send_typed_answer_reply(cid, reply))
                return reply

            if state["stage"] == "profile":
                reply = f"Provider for {state['name']}? (named local model endpoint, or - for the default; /cancel to abort)"
                self._advance(cid, state,
                              profile=None if text.strip() == "-" else text.strip(),
                              stage="provider",
                              message_id=self._send_typed_answer_reply(cid, reply))
                return reply

            # stage == "provider"
            provider = None if text.strip() == "-" else text.strip()
            name, profile = state["name"], state["profile"]
            # ⚠ Dropped BEFORE the call, deliberately. hire_agent blocks for up
            # to 10s, and a second answer landing in that window (each update
            # runs on its own thread — see run_polling) would otherwise be read
            # as another provider answer and hire the same agent twice. The
            # cost is that anything failing past this line leaves the operator
            # with no flow, so the except below puts it back rather than
            # dropping them into silence.
            del self.pending[cid]
            if self.telegram:
                self.telegram.send_chat_action(cid)
            logger.info(
                f"hire submit: chat={cid} agent={name} "
                f"profile={'set' if profile else 'default'} provider={'set' if provider else 'default'}"
            )
            try:
                code, resp = self.mesh.hire_agent(name, profile=profile, provider=provider)
            except Exception as exc:
                # MeshClient.request turns transport failures into (500, ...)
                # itself, so reaching here means something broke outside that
                # contract entirely. Before, this killed the dispatch thread
                # with the flow already deleted: no agent, no message, no way
                # back except starting over — the exact shape of "it does
                # nothing" this ticket is about.
                logger.error(f"hire submit raised before any status: chat={cid} agent={name}: {exc}")
                reply = (
                    f"❌ Hire for {name} wasn't submitted ({type(exc).__name__}). "
                    "Send the provider again to retry, or /cancel."
                )
                # Anchor to the message that just told them, not to the one
                # they last saw before it — the retry is answered by typing,
                # so this reply is a fresh send like every other one here.
                self.pending[cid] = dict(state, stage="provider",
                                         message_id=self._send_typed_answer_reply(cid, reply))
                return reply
            logger.info(f"hire submit: chat={cid} agent={name} status={code}")
            if code == 202:
                extras = ", ".join(f"{k} {v}" for k, v in (("profile", profile), ("provider", provider)) if v)
                reply = (
                    f"⏳ Hire request admitted for {name}"
                    + (f" ({extras})" if extras else "")
                    + " · agent creation is not yet confirmed."
                )
                # A fresh name is the one thing here worth handing back
                # verbatim -- to @mention it, or type it into another chat --
                # without retyping it by hand.
                markup = {"inline_keyboard": [[{"text": "📋 Copy name", "copy_text": {"text": name}}]]}
                self._send_typed_answer_reply(cid, reply, reply_markup=markup)
            else:
                reply = f"❌ Failed to hire {name}: {resp.get('detail', 'error')}"
                self._send_typed_answer_reply(cid, reply)
            return reply

        if state["flow"] == "retire":
            agent = state["agent"]
            if text.strip() != agent:
                reply = f"That doesn't match '{agent}' — type it exactly to confirm, or /cancel."
                self._advance(cid, state, message_id=self._send_typed_answer_reply(cid, reply))
                return reply  # stay open for retry, same as the web console's disabled-until-match button
            del self.pending[cid]
            if self.telegram:
                self.telegram.send_chat_action(cid)
            code, resp = self.mesh.retire_agent(agent)
            if code == 202:
                reply = f"✅ {agent} retired · queues and boards retained for a later re-hire."
            else:
                reply = f"❌ Failed to retire {agent}: {resp.get('detail', 'error')}"
            self._send_typed_answer_reply(cid, reply)
            return reply

        if state["flow"] == "broadcast":
            message = text.strip()
            del self.pending[cid]
            if self.telegram:
                self.telegram.send_chat_action(cid)
            code, resp = self.mesh.send_message("all", message)
            if code == 202:
                reply = "📢 Broadcast sent."
            else:
                reply = f"❌ Broadcast failed: {resp.get('detail', 'error')}"
            if self.telegram:
                self.telegram.send_message(cid, reply)
            return reply

        return None

    def handle_callback_query(
        self, chat_id: int | str, callback_id: str, data: str, message_id: int | None = None
    ) -> str:
        """`message_id` — the id of the message the tapped button lives on,
        from `callback_query.message.message_id` — is how every sub-flow
        handler below knows to edit that screen in place instead of posting
        a new one; see `_send_or_edit_message`."""
        agent = _callback_agent(data)
        if agent is not None and agent not in self._tmux_agents():
            # Edit-in-place means a screen can outlive the agent it names —
            # retired between the picker showing and this tap landing. A
            # real popup here, not a small toast that could be masked by
            # the very edit this tap would otherwise trigger, and no edit
            # to what's very likely an already-stale screen.
            if self.telegram:
                self.telegram.answer_callback_query(
                    callback_id, text=f"⚠️ {agent} is no longer enrolled.", show_alert=True
                )
            return ""
        if self.telegram:
            self.telegram.answer_callback_query(callback_id)
        if data in ("menu", "ov", "at", "lc", "al", "hi", "ta", "vt", "wa"):
            return self._dispatch_menu_action(chat_id, data, message_id)
        if data.startswith("wp:"):
            return self.handle_watch_pick(chat_id, data[len("wp:"):], message_id)
        if data.startswith("ws:"):
            return self.handle_watch_stop(chat_id, data[len("ws:"):])
        if data.startswith("at:"):
            return self.handle_addticket_pick_agent(chat_id, data[len("at:"):], message_id)
        if data.startswith("lc:"):
            return self.handle_lifecycle_pick_agent(chat_id, data[len("lc:"):], message_id)
        if data.startswith("lp:"):
            return self.handle_lifecycle_control(chat_id, "PauseAgent", data[len("lp:"):], message_id)
        if data.startswith("lr:"):
            return self.handle_lifecycle_control(chat_id, "ResumeAgent", data[len("lr:"):], message_id)
        if data.startswith("lret:"):
            return self.handle_retire_start(chat_id, data[len("lret:"):], message_id)
        if data.startswith("ta:"):
            return self.handle_message_agent_pick(chat_id, data[len("ta:"):], message_id)
        if data.startswith("ap:"):
            return self.handle_addticket_priority(chat_id, data[len("ap:"):])
        return ""

    def handle_text_message(self, chat_id: int | str, text: str, message_id: int | None = None) -> str:
        """Entry point for a plain (non-callback) chat message: a pending
        flow's answer, a sticky-keyboard tap, a known command, or a prompt
        for this chat's target agent. `message_id` — the incoming message's
        own id — only matters past this point for the last two branches
        (`@mention` and the plain-prompt fallback), to react on it; every
        other branch here is a menu action or command, not a turn to a CLI
        agent that could run long enough for the reaction to matter."""
        # ⚠ Which BRANCH, not which text. "Nothing happened" is always one of
        # these branches having been taken instead of the expected one, and
        # the branch name says that without putting a chat's contents in a
        # log file. The sticky code ("hi", "at") is this bot's own menu
        # vocabulary, so it is logged as-is.
        pending_reply = self.handle_pending_text(chat_id, text)
        if pending_reply is not None:
            return pending_reply
        if text in self.STICKY_LABELS:
            code = self.STICKY_LABELS[text]
            logger.debug(f"text: chat={chat_id} -> sticky menu action {code!r}")
            return self._dispatch_menu_action(chat_id, code)
        if text.startswith(self.STICKY_TARGET_PREFIX):
            logger.debug(f"text: chat={chat_id} -> target-agent picker")
            return self.handle_message_agent_start(chat_id)
        if text.startswith("🔊 Voice") or text.startswith("🔇 Voice") or text in ("/voice", "/tts"):
            return self.handle_voice_toggle(chat_id)
        if text == "/menu":
            return self.handle_menu_command(chat_id)
        if text == "/status":
            return self.handle_status_command(chat_id)
        if text == "/watch":
            return self.handle_watch_start(chat_id)
        if text.startswith("/watch "):
            return self.handle_watch_pick(chat_id, text[len("/watch "):].strip())
        if text == "/unwatch":
            return self.handle_watch_stop_command(chat_id)
        if text == "/run" or text.startswith("/run "):
            return self.handle_run_command(chat_id, text[len("/run"):].strip(), message_id)
        if text.startswith("@"):
            mention = _parse_mention(text)
            if mention is not None:
                logger.debug(f"text: chat={chat_id} -> @mention prompt")
                return self.handle_mention_prompt(chat_id, *mention, message_id=message_id)
        logger.debug(f"text: chat={chat_id} -> prompt for {self.target_agent}")
        return self.handle_user_prompt(chat_id, text, message_id=message_id)

    def _get_activity_tail(self, agent: str) -> str | None:
        """Fetch the current latest activity cursor for an agent before prompting."""
        try:
            cursor = None
            while True:
                code, data = self.mesh.get_activity(agent, after=cursor, limit=1000)
                if code != 200:
                    break
                items = data.get("activity", [])
                if not items:
                    break
                cursor = data.get("next_cursor")
                if len(items) < 1000:
                    break
            return cursor
        except Exception as exc:
            logger.debug(f"Failed to fetch activity tail for {agent}: {exc}")
        return None

    def _watch_activity(
        self,
        chat_id: int | str,
        agent: str,
        after_cursor: str | None,
        render: ActivityRender,
        timeout_s: float = 300.0,
        stream_fn=None,
    ) -> None:
        """Background thread consuming activity events for a prompted turn.

        ⚠ THE DEADLINE HAS TO HOLD WHEN NOTHING IS HAPPENING, which is the
        only case it exists for. This loop checks `timeout_s` per iteration,
        so with an idle agent — no events, ever — it never got an iteration
        and the thread lived until the connection died, which with SSE
        keepalives is never. Measured on the acceptance instance: four live
        watcher threads and four held connections for one agent. A guard whose
        trigger condition is a subset of the condition it guards against is
        not a guard, and it is the second of that shape found in one day (the
        first was require_isolated_tmux, unfireable because the container
        always sets TMUX_TMPDIR).

        The fix is `heartbeat=True`: the stream now yields None on keepalives
        and reconnects, so an idle stream still gives this loop a turn to look
        at the clock and at its stop switch.

        ⚠ Replaced, not stacked: a newer prompt for the same chat and agent
        sets this watcher's `stop_event` (see handle_user_prompt), so the old
        one ends at its next tick instead of running out its own deadline
        alongside the new one.
        """
        stream_gen = stream_fn or (lambda: self.mesh.stream_activity(agent, after=after_cursor, heartbeat=True))
        start_time = time.time()
        stop_event = getattr(render, "stop_event", None)
        try:
            for event in stream_gen():
                if render.completed or (time.time() - start_time > timeout_s):
                    logger.debug(
                        f"activity watcher for {agent} ending: "
                        f"{'render completed' if render.completed else f'{timeout_s:.0f}s deadline'}"
                    )
                    break
                if stop_event is not None and stop_event.is_set():
                    logger.debug(f"activity watcher for {agent} ending: replaced by a newer turn")
                    break
                if not isinstance(event, dict):
                    continue  # heartbeat: the checks above are the point of it
                if event.get("agent") and event.get("agent") != agent:
                    continue
                render.add_event(event)
                render.flush(self.telegram)
        except Exception as exc:
            logger.debug(f"Activity watcher exception for {agent}: {exc}")
        finally:
            if render.events and render.message_id:
                render.finalize()
                render.flush(self.telegram, force=True)

    _SNAPSHOT_PREFIX = "\x1b[2J\x1b[H"

    def _run_pane_watch(
        self,
        chat_id: int | str,
        agent: str,
        render: "PaneWatchRender",
        stop_event: threading.Event,
        ws_connect_fn=None,
    ) -> None:
        """Background thread behind `/watch`: connects the session door once,
        and on a fixed cadence asks control.py for one fresh `capture-pane`
        (`{"refresh": true}`, `LLD-session.md` §3) rather than reconstructing
        a screen from the live `%output` diff stream — a client-side terminal
        emulator is more machinery than a periodic snapshot needs. A snapshot
        frame is recognised by the `\\x1b[2J\\x1b[H` clear-and-home prefix
        every capture-pane snapshot starts with (LLD-session §3); anything
        else received in between (an incremental live diff from the same
        persistent subscription) is drained and discarded — we only ever
        render a full fresh frame, never a partial one.
        """
        cid = str(chat_id)
        reply_markup = {"inline_keyboard": [[{"text": "⏹ Stop watching", "callback_data": f"ws:{agent}"}]]}
        chrome = self.pane_watch_chrome_overrides.get(agent, self.pane_watch_chrome_default)
        tail_span = max(self.pane_watch_tail_span, chrome + 1)
        start_time = time.time()
        saw_working = False
        stop_reason = "stopped by request"
        last_window: list[str] = []

        def window_from(data: str) -> list[str]:
            return _pane_tail_window(_strip_ansi(data).split("\n"), chrome_lines=chrome, tail_span=tail_span)

        try:
            base_url = getattr(self.mesh, "base_url", "http://127.0.0.1:8080")
            token = getattr(self.mesh, "token", "")
            ssl_ctx = getattr(self.mesh, "ssl_context", None)
            ws_url = _derive_session_url(base_url, self.session_url or "")

            def _default_connect():
                headers = {"Authorization": f"Bearer {token}"}
                return ws_connect(ws_url, additional_headers=headers, ssl=ssl_ctx)

            connect_fn = ws_connect_fn or _default_connect

            with connect_fn() as ws:
                while True:
                    if stop_event.is_set():
                        stop_reason = "stopped by request"
                        break
                    if time.time() - start_time > self.pane_watch_max_duration_s:
                        stop_reason = f"stopped after {int(self.pane_watch_max_duration_s)}s (time limit)"
                        break

                    ws.send(json.dumps({"subscribe": [agent], "mode": "read-only", "refresh": True}))

                    snapshot_text = None
                    drain_deadline = time.time() + 5.0
                    while time.time() < drain_deadline:
                        try:
                            msg = ws.recv(timeout=max(0.0, drain_deadline - time.time()))
                        except TimeoutError:
                            break
                        if msg is None:
                            break
                        try:
                            payload = json.loads(msg)
                        except (json.JSONDecodeError, TypeError):
                            continue
                        data = payload.get("data")
                        if payload.get("agent") != agent or not isinstance(data, str):
                            continue
                        if data.startswith(self._SNAPSHOT_PREFIX):
                            snapshot_text = data
                            break

                    if snapshot_text is not None:
                        last_window = window_from(snapshot_text)

                    pcode, pdata = self.mesh.get_presence(agent)
                    state = pdata.get("presence", {}).get("state") if pcode == 200 else None
                    if state == "working":
                        saw_working = True
                    elif state == "idle" and saw_working:
                        stop_reason = f"stopped: {agent} went idle"
                        render.flush(self.telegram, last_window, reply_markup=reply_markup)
                        break

                    render.flush(self.telegram, last_window, reply_markup=reply_markup)

                    if stop_event.wait(self.pane_watch_refresh_s):
                        stop_reason = "stopped by request"
                        break
        except Exception as exc:
            logger.debug(f"Pane watch exception for {agent}: {exc}")
            stop_reason = "stopped: session connection lost"
        finally:
            render.completed = True
            render.flush(
                self.telegram, last_window,
                footer=f"<i>⏹ {html.escape(stop_reason)}</i>",
                clear_markup=True, force=True,
            )
            # Only clear this chat's slot if a newer /watch hasn't already
            # replaced it — see handle_watch_pick. ⚠ The identity test and the
            # pop are one compare-and-swap and must not be separable: without
            # the lock, a /watch replacing the slot between them makes this
            # thread delete the NEW watch's entry, leaving a live watcher
            # nothing can stop.
            with self.chat_txn(cid):
                if self.pane_watches.get(cid, {}).get("stop_event") is stop_event:
                    self.pane_watches.pop(cid, None)

    def finalize_activity(self, chat_id: int | str, agent: str, render: "ActivityRender | None" = None) -> None:
        """Finalize and flush the live activity message for (chat_id, agent).

        ⚠ COMPARE-AND-POP when the caller owns a specific render. Popping by
        key alone let a failing prompt finalize a LATER prompt's render: A
        installs render A and blocks in its send, B swaps in render B and
        succeeds, A's send then fails and A cleans up "the render for this
        chat and agent" — which is now B's. B stayed live with its render
        completed and nothing tracking it. A caller that installed a render
        passes it here and only its own is taken.

        ⚠ A LIMIT WITH A FIX IN FLIGHT — not accepted behaviour, and not
        settled. `ReplyPusher` calls this with no render handle, because a
        reply carries no link back to the prompt that caused it: the api door
        mints a fresh `correlation_id` per envelope and an agent's reply is
        its own envelope, so nothing in a mailbox message says which turn it
        answers. With two overlapping prompts to one agent, the first reply
        therefore ends whichever render is installed — possibly the second,
        still-running turn's. The display stops early; no state is corrupted.

        api-agent first declined a wire change and that decline has been
        OVERTAKEN: architect put an opt-in exact-correlation option to them,
        they specified it, and it is now being built across the tmux port,
        `office send` and the openshell port — additive, opt-in, and
        dependent on the replying agent passing the id back. So this docstring
        does not get to say "by decision".

        ⚠ When it lands, the fallback still matters and this code path stays.
        Correlation is opt-in, so uncorrelated replies keep arriving — from
        agents that don't pass it, from anything replying by another route —
        and the by-key behaviour below is what serves them. The pinning test
        becomes the fallback's test rather than something to delete.

        Note also that this is an ordering dependency between ReplyPusher's
        thread and the polling worker, which the chat transaction excludes but
        does not order; correlation is what would let us break the dependency
        rather than schedule around it. Dropping the reply-triggered finalize
        would remove it too, at the cost of every turn's display lingering
        until the watcher notices (up to its 300s timeout), which architect
        ruled against: a certain cost on every turn against a rare one. Pinned
        by
        test_an_overlapping_reply_finalizes_the_wrong_turns_render.
        """
        cid = str(chat_id)
        key = f"{cid}:{agent}"
        with self.chat_txn(cid):
            current = self.activity_renders.get(key)
            if current is None or (render is not None and current is not render):
                return
            self.activity_renders.pop(key, None)
        current.finalize()
        current.flush(self.telegram, force=True)

    def handle_user_prompt(
        self,
        chat_id: int | str,
        text: str,
        *,
        agent_override: str | None = None,
        raw: bool = False,
        message_id: int | None = None,
    ) -> str:
        """Post `text` to this chat's target agent (§ 🎯 Message agent,
        default target_agent/--agent) and return immediately. `agent_override`
        is handle_mention_prompt's one-off "@name ..." destination — used for
        this call only, never written to `chat_target_agent`. `raw` is
        handle_run_command's "/run <agent> <text>" — sends a Command-kind
        envelope instead of a Message-kind one (see MeshClient.send_command),
        otherwise identical: same presence/blocked gate, same activity
        watcher, same one-off (never persistent) destination. `message_id` —
        the incoming prompt's own id — gets a 👀 reaction the moment the
        envelope is actually dispatched: a persistent marker on the message
        itself, unlike `send_chat_action`'s few-second typing indicator, for
        a turn whose real reply (via ReplyPusher, separately) can arrive
        much later than any typing indicator would ever suggest.

        ⚠ The "✅ Sent to X"/"✅ Ran on X" text confirmation only disappears
        when the reaction actually landed — confirmed by checking
        `setMessageReaction`'s own response, not assumed from having tried.
        Two real ways it might not: `message_id` is None (every caller not
        tied to a live Telegram message — the CLI's own `--prompt` one-shot
        being the one that matters, since it has no inbound message to react
        to at all), or the call itself fails (a chat can have reactions
        disabled entirely; Telegram reports that as an ordinary API error,
        not a silent no-op). Either way this falls back to the text, the
        same "don't fail closed" lesson `_send_or_edit_message` learned.
        Failure/blocked replies below are untouched regardless — a reaction
        only ever means "dispatched", never "failed", so there is nothing
        for it to stand in for on those paths.

        ⚠ No wait, no reply capture here — that used to be a `while not
        completed` loop polling for target_agent's reply, unbounded, run
        inline in the polling loop. It matched nothing about how delivery
        actually works: POST /agents/{agent}/envelopes returns 202
        immediately and always: the switch/port/api chain is fire-and-forget
        all the way to the destination's inbox stream, nothing in it waits on
        anything. The blocking was invented here, not required by the
        transport, and it broke badly in production — one chat waiting
        forever for a reply froze the poller for every other chat too
        (measured live on the acceptance VM). ReplyPusher is what delivers
        the eventual reply now, on its own schedule, matching the actual
        fire-and-forget model.
        """
        cid = str(chat_id)
        agent = agent_override or self._target_for(cid)
        if self.telegram:
            self.telegram.send_chat_action(cid)
        code, presence_data = self.mesh.get_presence(agent)
        state = presence_data.get("presence", {}).get("state") if code == 200 else "unknown"

        if state == "blocked":
            reply_text = f"{agent} is not accepting messages right now"
            if self.telegram:
                self.telegram.send_message(cid, reply_text)
            return reply_text

        # Start live activity watcher if enabled. `own_render` stays None when
        # push is disabled, and is what this prompt is allowed to clean up
        # later — see finalize_activity: cleaning up "the render for this chat
        # and agent" can mean cleaning up someone else's.
        own_render = None
        if not self.no_activity_push and self.telegram:
            tail_cursor = self._get_activity_tail(agent)
            render = own_render = ActivityRender(cid, agent)
            key = f"{cid}:{agent}"
            # Swap under the chat's lock: get-then-set is the same race as the
            # flow's, and two prompts to one agent in quick succession could
            # otherwise both read no render, both install one, and leave the
            # loser running with a live thread and no one left to finalize it.
            # ⚠ Only the SWAP is guarded. Finalizing flushes over the network,
            # and holding a chat's lock across that would stall its next
            # message for no benefit — by then the old render is already
            # unreachable from the map, so nothing else can touch it.
            render.stop_event = threading.Event()
            with self.chat_txn(cid):
                old_render = self.activity_renders.get(key)
                self.activity_renders[key] = render
            if old_render:
                # ⚠ Stop the thread, not just the render. Finalizing the old
                # render left its watcher running against the same agent until
                # its own deadline — four prompts, four live threads and four
                # held SSE connections for one agent. Its stop_event is
                # checked on every tick, and heartbeats guarantee ticks.
                old_stop = getattr(old_render, "stop_event", None)
                if old_stop is not None:
                    old_stop.set()
                old_render.finalize()
                old_render.flush(self.telegram, force=True)

            watcher = threading.Thread(
                target=self._watch_activity,
                args=(cid, agent, tail_cursor, render),
                daemon=True,
                name=f"activity-watcher-{agent}",
            )
            watcher.start()

        code, resp = self.mesh.send_command(agent, text) if raw else self.mesh.send_message(agent, text)
        if code != 202:
            # `own_render` may be None (activity push disabled): then this is
            # the by-key path and there is nothing of ours to confuse.
            self.finalize_activity(cid, agent, render=own_render)
            verb = "run on" if raw else "send message to"
            reply_text = f"Failed to {verb} {agent}: {resp.get('detail', 'error')}"
            if self.telegram:
                self.telegram.send_message(cid, reply_text)
            return reply_text

        reacted = False
        if message_id is not None and self.telegram:
            resp = self.telegram.set_message_reaction(cid, message_id, "👀")
            reacted = isinstance(resp, dict) and resp.get("ok", False)
            if not reacted:
                # WARNING, matching `_send_or_edit_message`'s failed
                # editMessageText: both are a real Telegram call failing and
                # both recover (here, by keeping the text confirmation below).
                # Handled is not invisible — a chat that never accepts a
                # reaction should be readable in an ordinary log.
                logger.warning(
                    f"setMessageReaction failed (chat={cid}, msg={message_id}): "
                    f"{resp.get('description') if isinstance(resp, dict) else resp}; "
                    "confirming by text instead"
                )

        reply_text = f"✅ Ran on {agent}." if raw else f"✅ Sent to {agent}."
        if not reacted and self.telegram:
            self.telegram.send_message(cid, reply_text)
        return reply_text

    def _decline_edited_message(self, chat_id: str, update_id, edited: dict) -> None:
        """An edit is not a send, and this bot acts on sends only.

        ⚠ THE DECISION, on the record. Telegram delivers an `edited_message`
        update whenever the operator edits ANY message of theirs from the last
        48 hours, carrying the full edited text. Before this, that update was
        handled as if it were a new message, which meant three things nobody
        chose: editing a typo in an old message could answer a question the
        flow was currently asking (with text having nothing to do with it),
        could re-prompt the target agent with a near-duplicate turn, and —
        worst — could re-run a `/run` command that had already executed once.
        None of those are acts the operator performed; they pressed "edit" and
        fixed a word. It arrived with the original port (6844f87) with no
        comment, no test and no mention in the README, so it was never decided,
        only inherited.

        Rejected alternatives, so a later reader knows they were considered:
        treating an edit as a correction of the answer it replaces needs the
        stage to be reversible, and by the time an edit lands a hire may
        already have been submitted; keeping the old behaviour and documenting
        it makes the same physical act mean different things depending on
        whether a flow happens to be open, which is precisely the invisible
        state-dependence that made be9cbedd undiagnosable.

        ⚠ Not filtered at `getUpdates` with `allowed_updates` instead, which
        would be tidier and is deliberately not done: that drops update types
        silently and at a distance, so the next handler someone adds would
        fail by never being called. Declining here is one visible line in the
        log for every edit.

        The operator is told ONLY when a flow is open, because that is the
        case where saying nothing leaves them waiting on an answer the bot has
        already discarded. An edit with no flow open is logged and otherwise
        left alone rather than answered with a lecture.
        """
        logger.info(
            f"update {update_id}: edited_message from chat={chat_id} "
            f"(msg={edited.get('message_id')}) not dispatched — an edit is not a send"
        )
        state = self.pending.get(chat_id)
        if not state or not self.telegram:
            return
        stage = state.get("stage")
        waiting = f"{state.get('flow')} is still waiting" + (f" for the {stage}" if stage else "")
        self.telegram.send_message(
            chat_id,
            f"✏️ Editing a message doesn't send it, so that wasn't read as an answer — {waiting}. "
            "Send it as a new message, or /cancel.",
        )

    @staticmethod
    def _update_chat_id(update: dict) -> str | None:
        """The chat an update belongs to, for routing only — `_dispatch_update`
        does its own extraction and its own authorisation check.

        ⚠ Returns None rather than the string "None" when the id is missing:
        a malformed update must not become a chat named None with a thread of
        its own."""
        callback = update.get("callback_query")
        if callback:
            chat_id = callback.get("message", {}).get("chat", {}).get("id")
        else:
            msg = update.get("message") or update.get("edited_message")
            chat_id = msg.get("chat", {}).get("id") if msg else None
        return None if chat_id is None else str(chat_id)

    def chat_worker(self, chat_id: int | str) -> ChatWorker:
        """That chat's serial worker, created under the same guard as its
        transaction — "fetch one or make one" is the check-then-mutate this
        whole branch is about, and two workers for one chat would reintroduce
        exactly the concurrency they exist to remove."""
        cid = str(chat_id)
        with self._chat_locks_guard:
            worker = self._chat_workers.get(cid)
            if worker is None:
                worker = self._chat_workers[cid] = ChatWorker(cid, self._dispatch_update)
            return worker

    def submit_update(self, update: dict) -> None:
        """Hand one update to its chat's worker. The polling loop never blocks
        here; ordering within a chat is the worker's job.

        ⚠ AUTHORISE BEFORE ALLOCATING. A worker is a permanent thread and a
        permanent queue, so creating one for any chat id that arrives makes
        unauthenticated traffic a resource-exhaustion vector: measured, 30
        updates from 30 unauthorised chats left 30 live daemon threads behind
        after every one of those updates had been correctly rejected. The
        rejection has to happen before anything is allocated, not after.
        `_dispatch_update` still re-checks — this is a gate, not a
        replacement for the authorisation it does."""
        cid = self._update_chat_id(update)
        if cid is None or not self._chat_allowed(cid):
            # Handled inline: logging and dropping costs nothing and needs no
            # thread of its own, and an unauthorised chat must never get one.
            self._dispatch_update(update)
            return
        self.chat_worker(cid).submit(update)

    def _dispatch_update(self, update: dict) -> None:
        """⚠ Logs the SHAPE of an update, never its text: update id, kind,
        chat, message id, and how many characters arrived. A callback's `data`
        is logged in full because it is one of this bot's own short codes
        ("hi", "at:agent"), not something a person typed."""
        update_id = update.get("update_id")
        callback = update.get("callback_query")
        if callback:
            # ⚠ .get, not [...]: a malformed update must be dropped with a log
            # line, not raise out of the dispatcher. Found by the test that
            # feeds it an update with no chat at all.
            chat_id = self._update_chat_id(update)
            if chat_id is None:
                logger.debug(f"update {update_id}: callback with no chat id, dropped")
                return
            logger.debug(
                f"update {update_id}: callback data={callback.get('data', '')!r} "
                f"chat={chat_id} msg={callback['message'].get('message_id')}"
            )
            if not self._chat_allowed(chat_id):
                # ⚠ No reply, no answered callback query, nothing — silence
                # tells an unauthorized sender less than a rejection would
                # (not even that a bot is listening on the other end). Silent
                # to the sender, not to the log: an ignored chat id is the
                # first thing to rule out when "the button does nothing".
                logger.info(f"update {update_id}: callback from chat={chat_id} ignored (not the allowed chat)")
                return
            message_id = callback["message"].get("message_id")
            self.handle_callback_query(chat_id, callback["id"], callback.get("data", ""), message_id)
            return

        edited = update.get("edited_message")
        msg = update.get("message") or edited
        if not msg:
            logger.debug(f"update {update_id}: no message or callback in it, nothing to dispatch")
            return

        chat_id = self._update_chat_id(update)
        if chat_id is None:
            logger.debug(f"update {update_id}: message with no chat id, dropped")
            return
        logger.debug(
            f"update {update_id}: message chat={chat_id} msg={msg.get('message_id')} "
            f"edited={edited is not None} "
            f"text={len(msg.get('text', '').strip())} chars "
            f"photo={bool(msg.get('photo'))} document={bool(msg.get('document'))} "
            f"reply_to={bool(msg.get('reply_to_message'))}"
        )
        if not self._chat_allowed(chat_id):
            logger.info(f"update {update_id}: message from chat={chat_id} ignored (not the allowed chat)")
            return

        if edited is not None:
            self._decline_edited_message(chat_id, update_id, edited)
            return


        photo_sizes = msg.get("photo")
        if photo_sizes:
            self.handle_photo_message(chat_id, photo_sizes, msg.get("caption", ""))
            return

        document = msg.get("document")
        if document:
            self.handle_document_message(chat_id, document, msg.get("caption", ""))
            return

        text = msg.get("text", "").strip()
        if not text:
            logger.debug(f"update {update_id}: message chat={chat_id} has no text, dropped")
            return

        self.handle_text_message(chat_id, text, msg.get("message_id"))

    def run_polling(self) -> None:
        """Run long-polling loop for Telegram updates.

        Does not call `enrol()` itself — the caller does that once,
        unconditionally, before dispatching to whichever mode runs (see
        `main()`). Enrolling here too would just be a second, redundant call
        with its own 60s retry budget stacked on top of the caller's.

        ⚠ Updates are handed to a per-chat worker (`submit_update`), not
        handled inline and no longer one thread per update. Inline handling
        froze the bot for EVERY chat behind one slow turn — measured live: a
        user's "hi" outlived architect's reply, and every later message went
        unfetched until that exchange resolved. They looked lost; they were
        never fetched. (`handle_user_prompt` no longer waits for a reply —
        that is `ReplyPusher`'s job — but a turn still makes several network
        calls, which is enough to freeze a shared loop.) Thread-per-update
        fixed the freeze and lost the ORDER: lock acquisition is not FIFO, so
        two answers could be applied back to front. One worker per chat keeps
        both properties — ordered within a chat, concurrent across chats.

        ⚠ AN UPDATE IS ACKNOWLEDGED TO TELEGRAM BEFORE IT IS HANDLED, and
        that is a deliberate loss boundary rather than an oversight. `offset`
        advances as soon as an update is queued, so a crash with a non-empty
        queue loses operator actions Telegram believes were delivered. The
        alternative — advancing `offset` only after processing — makes
        restart REDELIVER anything unacknowledged, and this client has no
        dedupe: a redelivered `/run` runs the command a second time and a
        redelivered prompt is sent twice. The asymmetry is the reason for the
        choice: a lost message is one the operator can send again, while a
        duplicated side effect cannot be un-run.

        ⚠ THE EXPOSURE IS "THE IN-PROGRESS UPDATE PLUS ANYTHING QUEUED BEHIND
        IT", not "the queue". Once `ChatWorker._run` has called `get()`, that
        update is off the queue — `qsize()` reads zero — while the handler
        spends up to tens of seconds in network calls, and it is just as
        acknowledged and just as lost if the process dies there. So an empty
        queue is not zero exposure, and there is always at least one update at
        risk while anything is being handled at all. `BACKLOG_WARN` announces
        a BACKLOG; it says nothing about a single in-flight update and never
        fires for one. Pinned by
        test_updates_are_acknowledged_to_telegram_before_they_are_handled so
        the boundary moves only on purpose.
        """
        if not self.telegram:
            logger.error("No Telegram token provided; long-polling loop disabled.")
            return

        if self.allowed_chat_id is None:
            logger.warning(
                "No --chat-id/TELEGRAM_CHAT_ID configured: every inbound message and "
                "button tap will be silently ignored (see _chat_allowed). This is not "
                "a hang — set one to let the bot respond to anyone."
            )

        logger.info(f"Telegram bot starting long-polling loop for {self.target_agent}...")
        offset = None

        while True:
            try:
                updates = self.telegram.get_updates(offset=offset, timeout=20)
                for update in updates:
                    offset = update["update_id"] + 1
                    self.submit_update(update)
            except Exception as exc:
                logger.error(f"Error in long-polling loop: {exc}")
                time.sleep(3.0)


class DryRunTelegramClient:
    """Dry-run Telegram client that prints formatted output to stdout.
    Allows running and reviewing Telegram bot workflows against real h-mesh
    data without requiring a Telegram bot token from BotFather.
    """

    def __init__(self):
        self.next_msg_id = 1

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> dict:
        msg_id = self.next_msg_id
        self.next_msg_id += 1
        extra = f"\n[keyboard: {reply_markup}]" if reply_markup else ""
        pm = f" [parse_mode={parse_mode}]" if parse_mode else ""
        print(f"[DRY-RUN Telegram] sendMessage (chat={chat_id}, msg_id={msg_id}){pm}:\n{text}{extra}\n")
        return {"ok": True, "result": {"message_id": msg_id, "chat": {"id": chat_id}, "text": text}}

    def send_voice(
        self,
        chat_id: int | str,
        voice: str | bytes | pathlib.Path,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        msg_id = self.next_msg_id
        self.next_msg_id += 1
        voice_desc = f"{len(voice)} bytes" if isinstance(voice, bytes) else str(voice)
        cap_str = f" caption={caption!r}" if caption else ""
        extra = f"\n[keyboard: {reply_markup}]" if reply_markup else ""
        print(f"[DRY-RUN Telegram] sendVoice (chat={chat_id}, msg_id={msg_id}, voice={voice_desc}){cap_str}:{extra}\n")
        return {
            "ok": True,
            "result": {
                "message_id": msg_id,
                "chat": {"id": chat_id},
                "voice": {"file_id": "dry_run_voice"},
                "caption": caption,
            },
        }

    def send_document(
        self,
        chat_id: int | str,
        filename: str,
        data: bytes,
        mime_type: str = "application/octet-stream",
        caption: str | None = None,
    ) -> dict:
        msg_id = self.next_msg_id
        self.next_msg_id += 1
        cap_str = f" caption={caption!r}" if caption else ""
        print(f"[DRY-RUN Telegram] sendDocument (chat={chat_id}, msg_id={msg_id}, filename={filename}, {len(data)} bytes, mime_type={mime_type}){cap_str}\n")
        return {
            "ok": True,
            "result": {
                "message_id": msg_id,
                "chat": {"id": chat_id},
                "document": {"file_id": "dry_run_document", "file_name": filename},
                "caption": caption,
            },
        }

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> dict:
        extra = f"\n[keyboard: {reply_markup}]" if reply_markup else ""
        pm = f" [parse_mode={parse_mode}]" if parse_mode else ""
        print(f"[DRY-RUN Telegram] editMessageText (chat={chat_id}, msg_id={message_id}){pm}:\n{text}{extra}\n")
        return {"ok": True, "result": {"message_id": message_id, "chat": {"id": chat_id}, "text": text}}

    def send_chat_action(self, chat_id: int | str, action: str = "typing") -> dict:
        print(f"[DRY-RUN Telegram] sendChatAction (chat={chat_id}, action={action})")
        return {"ok": True}

    def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> dict:
        kind = "alert" if show_alert else "toast"
        print(f"[DRY-RUN Telegram] answerCallbackQuery ({callback_query_id}) [{kind}]{f': {text}' if text else ''}")
        return {"ok": True}

    def edit_message_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: dict | None = None) -> dict:
        print(f"[DRY-RUN Telegram] editMessageReplyMarkup (chat={chat_id}, msg_id={message_id}):\n[keyboard: {reply_markup}]\n")
        return {"ok": True, "result": {"message_id": message_id, "chat": {"id": chat_id}}}

    def set_message_reaction(self, chat_id: int | str, message_id: int, emoji: str | None) -> dict:
        print(f"[DRY-RUN Telegram] setMessageReaction (chat={chat_id}, msg_id={message_id}): {emoji!r}")
        return {"ok": True}

    def set_chat_menu_button(self, chat_id: int | str | None = None, menu_button: dict | None = None) -> dict:
        print(f"[DRY-RUN Telegram] setChatMenuButton (chat={chat_id}): {menu_button}")
        return {"ok": True}

    def set_my_commands(self, commands: list[dict]) -> dict:
        print(f"[DRY-RUN Telegram] setMyCommands: {commands}")
        return {"ok": True}

    def get_updates(self, offset: int | None = None, timeout: int = 20) -> list[dict]:
        return []


def _door_ssl_context(api_url: str, ca_cert: str, insecure: bool) -> "ssl.SSLContext | None":
    """The context for talking to the h-mesh door, or None for plain HTTP.

    ⚠ `--insecure` is for a door with a self-signed certificate, which is what
    `setup.sh` generates. It disables verification entirely, so it says nothing
    about who answered — use `--ca-cert` wherever the certificate has an issuer
    worth checking.
    """
    if not api_url.lower().startswith("https://"):
        if ca_cert or insecure:
            logger.warning("--ca-cert/--insecure ignored: %s is not https", api_url)
        return None
    if insecure:
        logger.warning("TLS verification disabled for %s — traffic is encrypted, "
                       "but the door is not authenticated", api_url)
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return ssl.create_default_context(cafile=ca_cert or None)


def _sibling_path(path: str, suffix: str) -> str:
    """`cursor.json` -> `cursor.alerts.json`: a default alerts-cursor path
    that lives beside --cursor-file without colliding with it."""
    p = pathlib.Path(path)
    return str(p.with_name(f"{p.stem}.{suffix}{p.suffix}"))


def main() -> None:
    parser = argparse.ArgumentParser(description="h-mesh Telegram bot client")
    parser.add_argument("--api-url", default=os.getenv("H_MESH_API_URL", "http://localhost:8080"), help="h-mesh API base URL")
    parser.add_argument("--ca-cert", default=os.getenv("H_MESH_CA_CERT", ""),
                        help="verify the door's TLS certificate against this CA bundle (H_MESH_CA_CERT)")
    parser.add_argument("--insecure", action="store_true", default=(os.getenv("H_MESH_INSECURE") == "1"),
                        help="skip TLS verification (self-signed door certificate) (H_MESH_INSECURE=1)")
    parser.add_argument("--api-token", default=os.getenv("H_MESH_API_TOKEN", os.getenv("API_TOKEN", "")), help="h-mesh API Bearer token")
    parser.add_argument("--bot-token", default=os.getenv("TELEGRAM_BOT_TOKEN", ""), help="Telegram Bot API token")
    parser.add_argument("--cursor-file", default=os.getenv("H_MESH_CURSOR_FILE", os.getenv("CURSOR_FILE", DEFAULT_CURSOR_FILE)), help="File path to store message cursor")
    parser.add_argument("--agent", default="architect", help="Target agent name")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode (prints Telegram operations to stdout)")
    parser.add_argument("--prompt", type=str, default="", help="Prompt text to send in dry-run mode")
    parser.add_argument("--status", action="store_true", help="Check status in dry-run mode")
    parser.add_argument("--menu", action="store_true", help="Show the inline menu in dry-run mode")
    # A bot cannot start a conversation: Telegram only lets it reply to a chat
    # it has already heard from. --chat-id supplies one directly so the bot can
    # drive a known chat without waiting for an inbound message first.
    parser.add_argument("--chat-id", type=str, default=os.getenv("TELEGRAM_CHAT_ID", ""),
                        help="Drive this chat directly instead of polling for one")
    parser.add_argument("--alerts-cursor-file", default=os.getenv("ALERTS_CURSOR_FILE", ""),
                        help="File path to store the alerts-stream cursor (default: derived from --cursor-file)")
    parser.add_argument("--no-alert-push", action="store_true", default=os.getenv("NO_ALERT_PUSH") == "1",
                        help="Disable proactively pushing new watchdog alerts to --chat-id")
    parser.add_argument("--no-activity-push", action="store_true", default=os.getenv("NO_ACTIVITY_PUSH") == "1",
                        help="Disable live-updating activity message while an agent works")
    parser.add_argument("--voice", action="store_true", default=os.getenv("TELEGRAM_VOICE") == "1",
                        help="Enable spoken voice replies feature for this tenant")
    parser.add_argument("--tts-voice", default=os.getenv("TTS_VOICE", DEFAULT_TTS_VOICE),
                        help=f"Default edge-tts voice for spoken replies (default: {DEFAULT_TTS_VOICE})")
    parser.add_argument("--session-url", default=os.getenv("H_MESH_SESSION_URL", ""),
                        help="h-mesh Session WebSocket URL (default: derived from --api-url, port 8081)")
    parser.add_argument("--pane-watch-chrome-default", type=int,
                        default=int(os.getenv("PANE_WATCH_CHROME_DEFAULT", "4")),
                        help="/watch: bottom pane rows to crop as UI chrome (input box, hints) (default: 4)")
    parser.add_argument("--pane-watch-chrome-overrides", default=os.getenv("PANE_WATCH_CHROME_OVERRIDES", ""),
                        help="/watch: per-agent chrome-row exceptions, \"agent=n,agent2=n\" — "
                             "the bot cannot see which CLI an agent runs (API.md has no such field), "
                             "and Claude/agy and Codex do not agree on chrome height (see README §2c)")
    parser.add_argument("--pane-watch-tail-lines", type=int,
                        default=int(os.getenv("PANE_WATCH_TAIL_LINES", "12")),
                        help="/watch: how many rows back from the bottom of the pane to look (default: 12)")
    parser.add_argument("--pane-watch-refresh-seconds", type=float,
                        default=float(os.getenv("PANE_WATCH_REFRESH_SECONDS", "2.0")),
                        help="/watch: seconds between pane refreshes (default: 2.0)")
    parser.add_argument("--pane-watch-max-duration-seconds", type=float,
                        default=float(os.getenv("PANE_WATCH_MAX_DURATION_SECONDS", "600")),
                        help="/watch: auto-stop a forgotten watch after this many seconds (default: 600)")
    parser.add_argument("--mini-app-url", default=os.getenv("MINI_APP_URL", ""),
                        help="Public HTTPS URL for clients/web/mini.html — adds a 📊 Dashboard "
                             "web_app button to the sticky menu when set; omitted entirely otherwise")
    parser.add_argument("--run-allowed-commands",
                        default=os.getenv("RUN_ALLOWED_COMMANDS", ",".join(DEFAULT_RUN_ALLOWED_COMMANDS)),
                        help="/run: comma-separated exact-match allowlist of native CLI slash commands "
                             "(default: unrestricted -- this bot is already locked to one chat_id and the "
                             "agent already runs with permissions skipped); set this to bound /run to "
                             "specific commands instead")

    args = parser.parse_args()

    if not args.api_token:
        logger.error("Error: API token required (--api-token, H_MESH_API_TOKEN, or API_TOKEN env var)")
        sys.exit(1)

    ssl_context = _door_ssl_context(args.api_url, args.ca_cert, args.insecure)
    mesh_client = MeshClient(base_url=args.api_url, token=args.api_token, app_name="telegram",
                             ssl_context=ssl_context)
    cursor_store = CursorStore(filepath=args.cursor_file)

    is_dry_run = args.dry_run or not bool(args.bot_token)
    if is_dry_run:
        logger.info("Running in DRY-RUN mode (printing Telegram operations to stdout)...")
        telegram = DryRunTelegramClient()
    else:
        telegram = TelegramClient(bot_token=args.bot_token)

    bot = TelegramBot(
        mesh_client=mesh_client,
        telegram_client=telegram,
        cursor_store=cursor_store,
        target_agent=args.agent,
        allowed_chat_id=args.chat_id or None,
        default_tts_voice=args.tts_voice or None,
        voice_feature_enabled=args.voice,
        no_activity_push=args.no_activity_push,
        session_url=args.session_url or None,
        pane_watch_chrome_default=args.pane_watch_chrome_default,
        pane_watch_chrome_overrides=_parse_int_overrides(args.pane_watch_chrome_overrides),
        pane_watch_tail_span=args.pane_watch_tail_lines,
        pane_watch_refresh_s=args.pane_watch_refresh_seconds,
        pane_watch_max_duration_s=args.pane_watch_max_duration_seconds,
        mini_app_url=args.mini_app_url or None,
        run_allowed_commands=_parse_command_allowlist(args.run_allowed_commands),
    )

    # ⚠ Called once here, unconditionally, before any mode below runs — not
    # per-branch. container/entrypoint.sh forks this process and the api door
    # at essentially the same instant with no readiness wait, so enrolment can
    # lose that race; TelegramBot.enrol() retries with backoff to cover it
    # (see its docstring — this is what was silently broken in production).
    bot.enrol()

    if is_dry_run:
        if args.menu:
            bot.handle_menu_command("dry_run_chat")
        elif args.status:
            bot.handle_status_command("dry_run_chat")
        elif args.prompt:
            bot.handle_user_prompt("dry_run_chat", args.prompt)
        else:
            logger.info("Performing dry-run status check...")
            bot.handle_status_command("dry_run_chat")
    elif args.chat_id and args.prompt:
        bot.handle_user_prompt(args.chat_id, args.prompt)
    elif args.chat_id and args.status:
        bot.handle_status_command(args.chat_id)
    else:
        if args.chat_id:
            # cursor_store (--cursor-file) is entirely ReplyPusher's now — the
            # mailbox cursor it used to track for handle_user_prompt's old
            # wait loop moved here wholesale when that loop was removed.
            reply_pusher = ReplyPusher(
                mesh=mesh_client,
                telegram=telegram,
                chat_id=args.chat_id,
                cursor_store=cursor_store,
                tts_voice=args.tts_voice or None,
                voice_enabled_fn=bot.is_voice_enabled,
                activity_finalizer_fn=bot.finalize_activity,
            )
            threading.Thread(target=reply_pusher.run, daemon=True, name="reply-pusher").start()
            if not args.no_alert_push:
                alerts_cursor_file = args.alerts_cursor_file or _sibling_path(args.cursor_file, "alerts")
                pusher = AlertPusher(mesh=mesh_client, telegram=telegram, chat_id=args.chat_id, cursor_store=CursorStore(filepath=alerts_cursor_file))
                threading.Thread(target=pusher.run, daemon=True, name="alert-pusher").start()
        else:
            logger.info("TELEGRAM_CHAT_ID not set; live reply/alert push disabled (the menu still works on demand).")
        bot.run_polling()


if __name__ == "__main__":
    main()
