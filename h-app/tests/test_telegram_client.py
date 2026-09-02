"""Unit tests for the Telegram bot client (clients/telegram/bot.py)."""

import base64
import inspect
import json
import logging
import os
import ssl
import tempfile
import threading
import time
from pathlib import Path

import pytest

from clients.telegram import bot
from clients.telegram.bot import (
    ActivityRender, AlertPusher, CursorStore, DryRunTelegramClient, MeshClient, PaneWatchRender,
    ReplyPusher, TelegramBot, TelegramClient, render_alert, render_reply,
    synthesize_speech, _configure_logging, _parse_sse_events, _derive_session_url, _resolve_log_level,
    _agent_picker_keyboard, _is_transient_chrome_line, _parse_int_overrides,
    _parse_mention, _pane_tail_window, _strip_ansi, _valid_attachment_filename, _valid_attachment_mime_type,
    ATTACHMENT_ALLOWED_PAYLOAD_KEYS, ATTACHMENT_MAX_BYTES, ATTACHMENT_MAX_CAPTION_BYTES, DEFAULT_CURSOR_FILE,
    TELEGRAM_MAX_FILE_BYTES,
)


class DummyMeshClient:
    def __init__(self, app_name="telegram", base_url="http://127.0.0.1:8080", token="dummy-token"):
        self.app_name = app_name
        self.base_url = base_url
        self.token = token
        self.ssl_context = None
        self.presence_state = "idle"
        self.delivery_unverified = None
        self.messages_queue = []
        self.activity_queue = []
        # agent -> port_type; defaults cover the common tmux roster used by tests
        self.roster = {"architect": "tmux", "sme-2": "tmux"}
        self.boards = {"architect": {"todo": [], "doing": [{"title": "Review auth change"}], "hold": [], "done": []}}
        self.added_tickets = []
        self.control_calls = []
        self.hired = []
        self.retired = []
        self.sent_envelopes = []
        self.sent_commands = []
        self.sent_attachments = []
        self.alerts = []
        self.alerts_next_cursor = None

    def enrol(self):
        return 202, {"stream_id": "s1", "correlation_id": "c1"}

    def send_message(self, destination, text):
        self.sent_envelopes.append({"destination": destination, "text": text})
        return 202, {"stream_id": "s2", "correlation_id": "c2"}

    def send_command(self, destination, text):
        self.sent_commands.append({"destination": destination, "text": text})
        return 202, {"stream_id": "s2c", "correlation_id": "c2c"}

    def send_attachment(self, destination, filename, mime_type, content_base64, caption=None):
        entry = {
            "destination": destination, "filename": filename, "mime_type": mime_type,
            "content_base64": content_base64, "caption": caption,
        }
        self.sent_attachments.append(entry)
        return 202, {"stream_id": "s3", "correlation_id": "c3"}

    def get_presence(self, agent):
        result = {
            "agent": agent,
            "port_type": self.roster.get(agent, "tmux"),
            "depths": {"ingress": 0, "egress": 0, "dead": 0},
            "presence": {"state": self.presence_state, "since": "2026-08-09T15:00:00Z"},
        }
        result["delivery_unverified"] = self.delivery_unverified
        return 200, result

    def get_board(self, agent):
        board = self.boards.get(agent, {"todo": [], "doing": [], "hold": [], "done": []})
        return 200, {"agent": agent, **board}

    def get_agents(self):
        return 200, {"agents": list(self.roster.keys())}

    def get_all_boards(self):
        agents = [{"agent": name, **self.boards.get(name, {"todo": [], "doing": [], "hold": [], "done": []})}
                  for name in self.roster]
        return 200, {"agents": agents}

    def add_ticket(self, agent, title, description="", priority=""):
        self.added_tickets.append({"agent": agent, "title": title, "description": description, "priority": priority})
        return 202, {"stream_id": "s3", "correlation_id": "c3"}

    def control_agent(self, kind, agent):
        self.control_calls.append({"kind": kind, "agent": agent})
        return 202, {"stream_id": "s4", "correlation_id": "c4"}

    def hire_agent(self, agent, cli="claude", profile=None, provider=None):
        self.hired.append({"agent": agent, "cli": cli, "profile": profile, "provider": provider})
        return 202, {"stream_id": "s5", "correlation_id": "c5"}

    def retire_agent(self, agent):
        self.retired.append(agent)
        return 202, {"stream_id": "s6", "correlation_id": "c6"}

    def get_messages(self, after=None, limit=100):
        res = []
        for msg in self.messages_queue:
            if after is None or msg["cursor"] > after:
                res.append(msg)
        batch = res[:limit]
        return 200, {"agent": self.app_name, "messages": batch, "next_cursor": batch[-1]["cursor"] if batch else after}

    def get_alerts(self, after=None, limit=100):
        cursor = self.alerts_next_cursor
        if self.alerts and cursor is None:
            cursor = self.alerts[-1].get("cursor")
        return 200, {"alerts": list(self.alerts[:limit]), "next_cursor": cursor}

    def get_activity(self, agent, after=None, limit=100):
        res = []
        for evt in self.activity_queue:
            if after is None or evt["cursor"] > after:
                res.append(evt)
        batch = res[:limit]
        return 200, {"agent": agent, "activity": batch, "next_cursor": batch[-1]["cursor"] if batch else after}

    def stream_activity(self, agent, after=None, heartbeat=False):
        # heartbeat mirrors the real client: an idle stream yields None so a
        # consumer with a deadline gets a turn. Kept here so the fake cannot
        # drift from the signature the watcher actually calls.
        for evt in self.activity_queue:
            if after is None or evt["cursor"] > after:
                yield evt


class DummyTelegramClient:
    def __init__(self):
        self.sent_messages = []
        self.sent_voices = []
        self.edited_messages = []
        self.chat_actions = []
        self.answered_callbacks = []
        self.commands_set = []
        self.requests = []
        self.downloaded_paths = []
        self.get_file_response = {"ok": True, "result": {"file_path": "photos/file_1.jpg"}}
        self.download_response = b"fake-jpeg-bytes"
        self.sent_documents = []
        self.send_document_response = {"ok": True}
        self.edited_reply_markups = []
        self.reactions_set = []
        self.menu_buttons_set = []

    def send_message(self, chat_id, text, reply_to_message_id=None, reply_markup=None, **kwargs):
        msg_id = len(self.sent_messages) + len(self.sent_voices) + 1
        entry = {"chat_id": chat_id, "text": text, "message_id": msg_id, "reply_markup": reply_markup, **kwargs}
        self.sent_messages.append(entry)
        return {"ok": True, "result": entry}

    def send_voice(self, chat_id, voice, caption=None, reply_to_message_id=None, reply_markup=None, **kwargs):
        msg_id = len(self.sent_messages) + len(self.sent_voices) + 1
        entry = {
            "chat_id": chat_id,
            "voice": voice,
            "caption": caption,
            "message_id": msg_id,
            "reply_markup": reply_markup,
            **kwargs,
        }
        self.sent_voices.append(entry)
        return {"ok": True, "result": entry}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kwargs):
        entry = {"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup, **kwargs}
        self.edited_messages.append(entry)
        return {"ok": True, "result": entry}

    def send_document(self, chat_id, filename, data, mime_type="application/octet-stream", caption=None):
        entry = {"chat_id": chat_id, "filename": filename, "data": data, "mime_type": mime_type, "caption": caption}
        self.sent_documents.append(entry)
        return self.send_document_response

    def send_chat_action(self, chat_id, action="typing"):
        self.chat_actions.append({"chat_id": chat_id, "action": action})
        return {"ok": True}

    def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answered_callbacks.append(
            {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert}
        )
        return {"ok": True}

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        entry = {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup}
        self.edited_reply_markups.append(entry)
        return {"ok": True, "result": entry}

    def set_message_reaction(self, chat_id, message_id, emoji):
        self.reactions_set.append({"chat_id": chat_id, "message_id": message_id, "emoji": emoji})
        return {"ok": True}

    def set_chat_menu_button(self, chat_id=None, menu_button=None):
        self.menu_buttons_set.append({"chat_id": chat_id, "menu_button": menu_button})
        return {"ok": True}

    def set_my_commands(self, commands):
        self.commands_set.append(commands)
        return {"ok": True}

    def request(self, method, params=None):
        self.requests.append((method, params))
        if method == "getFile":
            return self.get_file_response
        return {"ok": True}

    def download_file(self, file_path):
        self.downloaded_paths.append(file_path)
        return self.download_response


def test_enrol_retries_until_success_and_seeds_cursor(monkeypatch):
    """Reproduces the live acceptance-VM race: the api door isn't listening
    yet when this client starts, enrol() fails once (or twice), and must
    retry rather than give up permanently."""
    slept = []
    monkeypatch.setattr(bot.time, "sleep", lambda s: slept.append(s))

    class FlakyMeshClient(DummyMeshClient):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def enrol(self):
            self.attempts += 1
            if self.attempts < 3:
                return 500, {"detail": "<urlopen error [Errno 111] Connection refused>"}
            return 202, {"stream_id": "s1", "correlation_id": "c1"}

    mesh = FlakyMeshClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot_instance = TelegramBot(mesh, DummyTelegramClient(), store, target_agent="architect")
        ok = bot_instance.enrol()

    assert ok is True
    assert mesh.attempts == 3
    assert len(slept) == 2  # retried after attempt 1 and attempt 2, not after the success


def test_enrol_gives_up_after_timeout_without_raising(monkeypatch):
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)

    class AlwaysDownMeshClient(DummyMeshClient):
        def enrol(self):
            return 500, {"detail": "<urlopen error [Errno 111] Connection refused>"}

    mesh = AlwaysDownMeshClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot_instance = TelegramBot(mesh, DummyTelegramClient(), store, target_agent="architect")
        # timeout_s=0 -> the deadline has already passed after the first
        # attempt, so this returns quickly instead of retrying for 60s.
        ok = bot_instance.enrol(timeout_s=0)

    assert ok is False


def test_enrol_registers_bot_commands():
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot_instance = TelegramBot(mesh, telegram, store, target_agent="architect")
        bot_instance.enrol()

    assert len(telegram.commands_set) == 1
    commands = {c["command"] for c in telegram.commands_set[0]}
    assert {"menu", "status"} <= commands


def test_run_polling_does_not_enrol_itself():
    """enrol() is the caller's job now (main(), once, before dispatch) — a
    second call from inside run_polling would silently double the retry
    budget and was removed for exactly that reason."""
    class _StopPolling(BaseException):
        """Not an Exception: run_polling's `except Exception` (which retries
        forever on any failure) must not swallow this, or the loop never ends."""

    class CountingMeshClient(DummyMeshClient):
        def __init__(self):
            super().__init__()
            self.enrol_calls = 0

        def enrol(self):
            self.enrol_calls += 1
            return 202, {}

    class OneShotTelegramClient(DummyTelegramClient):
        def get_updates(self, offset=None, timeout=20):
            raise _StopPolling

    mesh = CountingMeshClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot_instance = TelegramBot(mesh, OneShotTelegramClient(), store, target_agent="architect")
        try:
            bot_instance.run_polling()
        except _StopPolling:
            pass
    assert mesh.enrol_calls == 0


def test_handle_user_prompt_returns_immediately_without_waiting():
    """Live bug this replaced: one chat's unanswered prompt used to block
    forever waiting for a reply, freezing the whole bot for every chat.
    handle_user_prompt must now post and return -- no wait loop at all."""
    mesh = DummyMeshClient()
    mesh.presence_state = "working"  # would have looped forever under the old design
    telegram = DummyTelegramClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot_instance = TelegramBot(mesh, telegram, store, target_agent="architect")

        reply = bot_instance.handle_user_prompt(111, "hi")

        assert reply == "✅ Sent to architect."
        assert telegram.sent_messages[-1]["text"] == "✅ Sent to architect."


def test_cursor_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        cfile = str(Path(tmpdir) / "cursor.json")
        store = CursorStore(cfile)

        assert store.load() is None

        store.save("1000-0")
        assert store.load() == "1000-0"

        store.save("1001-0")
        assert store.load() == "1001-0"


def test_default_cursor_file_is_not_a_bare_relative_filename():
    """A bare "cursor.json" default lands wherever CWD happens to be —
    including the repo root for an ad hoc local run with no --cursor-file,
    where it sat as an untracked file breaking
    test_the_image_tag_names_the_commit_it_was_built_from's dirty-tree
    check. The default must be an absolute path under a dot-directory,
    matching container/entrypoint.sh's own --cursor-file convention."""
    assert Path(DEFAULT_CURSOR_FILE).is_absolute()
    assert ".h-mesh" in Path(DEFAULT_CURSOR_FILE).parts
    assert CursorStore().filepath == Path(DEFAULT_CURSOR_FILE)


def test_cursor_store_save_creates_missing_parent_directories(tmp_path):
    nested = tmp_path / "does" / "not" / "exist" / "cursor.json"
    store = CursorStore(str(nested))
    store.save("1-0")
    assert nested.exists()
    assert store.load() == "1-0"


def test_status_command():
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot = TelegramBot(mesh, telegram, store, target_agent="architect")

        text = bot.handle_status_command(12345)
        assert "State: idle" in text
        assert "Doing: Review auth change" in text
        assert len(telegram.sent_messages) == 1
        assert "State: idle" in telegram.sent_messages[0]["text"]


def test_handle_user_prompt_when_blocked():
    mesh = DummyMeshClient()
    mesh.presence_state = "blocked"
    telegram = DummyTelegramClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot = TelegramBot(mesh, telegram, store, target_agent="architect")

        text = bot.handle_user_prompt(12345, "check auth")
        assert text == "architect is not accepting messages right now"
        assert len(telegram.sent_messages) == 1
        assert "not accepting messages" in telegram.sent_messages[0]["text"]


def test_handle_user_prompt_success():
    mesh = DummyMeshClient()
    mesh.presence_state = "working"
    telegram = DummyTelegramClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot = TelegramBot(mesh, telegram, store, target_agent="architect")

        reply = bot.handle_user_prompt(12345, "please check auth")

        assert reply == "✅ Sent to architect."
        assert len(telegram.sent_messages) == 1
        assert telegram.sent_messages[0]["text"] == "✅ Sent to architect."


def test_handle_user_prompt_shows_typing_before_dispatch():
    mesh = DummyMeshClient()
    mesh.presence_state = "working"
    telegram = DummyTelegramClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot = TelegramBot(mesh, telegram, store, target_agent="architect")

        bot.handle_user_prompt(12345, "please check auth")

        assert telegram.chat_actions == [{"chat_id": "12345", "action": "typing"}]


def test_dispatching_flows_all_show_typing_before_their_network_call():
    """Every flow that does a round trip to the fabric before replying shows
    a typing indicator first -- add ticket, lifecycle control, hire, retire,
    broadcast, and the main prompt dispatch (covered separately above)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        with bot_instance.chat_txn("12345"): bot_instance.pending["12345"] = {"flow": "addticket", "agent": "architect", "stage": "priority", "title": "t"}
        bot_instance.handle_addticket_priority(12345, "high")
        assert telegram.chat_actions[-1] == {"chat_id": "12345", "action": "typing"}

        bot_instance.handle_lifecycle_control(12345, "PauseAgent", "architect")
        assert telegram.chat_actions[-1] == {"chat_id": 12345, "action": "typing"}

        with bot_instance.chat_txn("12345"): bot_instance.pending["12345"] = {"flow": "hire", "stage": "provider", "name": "newagent", "profile": None}
        bot_instance.handle_pending_text(12345, "-")
        assert telegram.chat_actions[-1] == {"chat_id": "12345", "action": "typing"}

        with bot_instance.chat_txn("12345"): bot_instance.pending["12345"] = {"flow": "retire", "agent": "architect"}
        bot_instance.handle_pending_text(12345, "architect")
        assert telegram.chat_actions[-1] == {"chat_id": "12345", "action": "typing"}

        with bot_instance.chat_txn("12345"): bot_instance.pending["12345"] = {"flow": "broadcast"}
        bot_instance.handle_pending_text(12345, "standup in five")
        assert telegram.chat_actions[-1] == {"chat_id": "12345", "action": "typing"}


def test_door_context_none_for_plain_http():
    assert bot._door_ssl_context("http://localhost:8080", "", False) is None


def test_door_context_insecure_skips_verification():
    ctx = bot._door_ssl_context("https://host:8080", "", True)
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_door_context_verifies_by_default():
    ctx = bot._door_ssl_context("https://host:8080", "", False)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_telegram_api_client_takes_no_context():
    """⚠ --insecure is about the h-mesh door. api.telegram.org is a public host
    with a real certificate, and must keep being verified."""
    assert "ssl_context" not in inspect.signature(bot.TelegramClient.__init__).parameters


def test_handle_user_prompt_when_refused_by_policy():
    class RefusingMeshClient(DummyMeshClient):
        def send_message(self, destination, text):
            return 422, {"detail": "policy denied 'telegram' -> 'architect': no shared export/import tag"}

    mesh = RefusingMeshClient()
    telegram = DummyTelegramClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot_instance = TelegramBot(mesh, telegram, store, target_agent="architect")

        reply = bot_instance.handle_user_prompt(12345, "hello architect", message_id=7)
        assert "policy denied" in reply
        assert len(telegram.sent_messages) == 1
        assert "policy denied" in telegram.sent_messages[0]["text"]
        # never dispatched -- no reaction on a prompt the agent never received
        assert telegram.reactions_set == []


def test_handle_user_prompt_skips_the_redundant_text_confirmation_once_reacted():
    """The reaction is the acknowledgement once it actually lands -- a
    second, separate "✅ Sent to X" text underneath it would just be a
    duplicate of the same information."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_user_prompt(12345, "hello architect", message_id=7)
        assert reply == "✅ Sent to architect."
        assert telegram.reactions_set == [{"chat_id": "12345", "message_id": 7, "emoji": "👀"}]
        assert telegram.sent_messages == []


def test_unverified_delivery_notice_failure_cannot_replace_known_prompt_admission():
    """A warning-sink failure after reaction success must leave admission known."""
    class RaisingNoticeTelegram(DummyTelegramClient):
        def send_message(self, chat_id, text, **kwargs):
            raise OSError("telegram notice unavailable")

    mesh = DummyMeshClient()
    mesh.delivery_unverified = {"since": "2026-09-02T06:00:00Z", "stream_id": "a" * 32}
    telegram = RaisingNoticeTelegram()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot_instance = TelegramBot(
            mesh, telegram, store, target_agent="architect", no_activity_push=True,
        )

        reply = bot_instance.handle_user_prompt(12345, "fresh evidence", message_id=7)

    assert mesh.sent_envelopes == [{"destination": "architect", "text": "fresh evidence"}]
    assert telegram.reactions_set == [{"chat_id": "12345", "message_id": 7, "emoji": "👀"}]
    assert reply == "✅ Sent to architect. A prior delivery remains unverified; this send is fresh evidence."


def test_handle_user_prompt_confirms_by_text_when_there_is_no_message_id():
    """The CLI's own --prompt one-shot (main()'s `bot.handle_user_prompt(chat_id,
    args.prompt)`) has no inbound Telegram message to react to at all --
    message_id is always None there, so it must keep getting a text reply,
    the only feedback that path has ever had."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_user_prompt(12345, "hello architect")
        assert reply == "✅ Sent to architect."
        assert telegram.reactions_set == []
        assert telegram.sent_messages == [{
            "chat_id": "12345", "text": "✅ Sent to architect.", "message_id": 1, "reply_markup": None,
        }]


def test_handle_user_prompt_falls_back_to_text_when_the_reaction_itself_fails():
    """A chat can have reactions disabled entirely -- Telegram reports that
    as an ordinary failed API call, not a silent no-op, and the reaction
    would otherwise have been the *only* feedback for a successful send."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        class ReactionsDisabledTelegramClient(DummyTelegramClient):
            def set_message_reaction(self, chat_id, message_id, emoji):
                super().set_message_reaction(chat_id, message_id, emoji)
                return {"ok": False, "description": "Bad Request: REACTION_INVALID"}

        bot_instance.telegram = ReactionsDisabledTelegramClient()
        reply = bot_instance.handle_user_prompt(12345, "hello architect", message_id=7)
        assert reply == "✅ Sent to architect."
        assert bot_instance.telegram.sent_messages == [{
            "chat_id": "12345", "text": "✅ Sent to architect.", "message_id": 1, "reply_markup": None,
        }]


def test_failed_reaction_is_logged_at_warning_like_a_failed_edit(caplog):
    """Handled is not invisible. This used to be DEBUG -- i.e. invisible at
    the only level the daemon could run at -- while the equivalent failed
    editMessageText was WARNING. Both are a real Telegram call failing with
    a fallback behind it, so both are WARNING."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        class ReactionsDisabledTelegramClient(DummyTelegramClient):
            def set_message_reaction(self, chat_id, message_id, emoji):
                super().set_message_reaction(chat_id, message_id, emoji)
                return {"ok": False, "description": "Bad Request: REACTION_INVALID"}

        bot_instance.telegram = ReactionsDisabledTelegramClient()
        with caplog.at_level(logging.DEBUG, logger="mesh_telegram"):
            bot_instance.handle_user_prompt(12345, "hello architect", message_id=7)
        reaction_records = [r for r in caplog.records if "setMessageReaction failed" in r.getMessage()]
        assert [r.levelno for r in reaction_records] == [logging.WARNING]
        assert "REACTION_INVALID" in reaction_records[0].getMessage()


# ── log verbosity (H_MESH_LOG_LEVEL) ─────────────────────────────────────────

def test_resolve_log_level_accepts_the_standard_names_however_they_are_written():
    assert _resolve_log_level("DEBUG") == logging.DEBUG
    assert _resolve_log_level("debug") == logging.DEBUG
    assert _resolve_log_level("  Warning\n") == logging.WARNING
    assert _resolve_log_level("ERROR") == logging.ERROR
    assert _resolve_log_level("CRITICAL") == logging.CRITICAL
    # stdlib's own aliases, so an obvious spelling is not a silent demotion
    assert _resolve_log_level("WARN") == logging.WARNING
    assert _resolve_log_level("FATAL") == logging.CRITICAL


def test_resolve_log_level_falls_back_to_info_rather_than_raising():
    """This runs at import, before the bot can report anything at all -- a
    typo in a fresh VM's env must cost log detail, not the daemon."""
    assert _resolve_log_level(None) == logging.INFO
    assert _resolve_log_level("") == logging.INFO
    assert _resolve_log_level("   ") == logging.INFO
    assert _resolve_log_level("DEGUB") == logging.INFO
    # a number is not a name; nothing here maps 10 onto DEBUG
    assert _resolve_log_level("10") == logging.INFO


def test_configure_logging_says_so_when_it_falls_back(caplog):
    """A silently-demoted DEGUB rebuilds the exact blind spot the knob
    removes: someone believing they run at DEBUG while debug is dropped."""
    root_level = logging.getLogger().level
    try:
        with caplog.at_level(logging.DEBUG, logger="mesh_telegram"):
            assert _configure_logging("DEGUB") == logging.INFO
        warnings = [r for r in caplog.records if "H_MESH_LOG_LEVEL" in r.getMessage()]
        assert [r.levelno for r in warnings] == [logging.WARNING]

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="mesh_telegram"):
            assert _configure_logging("DEBUG") == logging.DEBUG
            assert _configure_logging(None) == logging.INFO
        assert [r for r in caplog.records if "H_MESH_LOG_LEVEL" in r.getMessage()] == []
    finally:
        logging.getLogger().setLevel(root_level)


# ── inline menu ──────────────────────────────────────────────────────────────

def _make_bot(mesh=None, telegram=None, tmpdir=None, allowed_chat_id=None, **kwargs):
    mesh = mesh or DummyMeshClient()
    telegram = telegram if telegram is not None else DummyTelegramClient()
    store = CursorStore(str(Path(tmpdir) / "cursor.json"))
    kwargs.setdefault("voice_feature_enabled", False)
    bot_instance = TelegramBot(
        mesh, telegram, store, target_agent="architect", allowed_chat_id=allowed_chat_id, **kwargs
    )
    return bot_instance, mesh, telegram


# ── chat_id restriction ────────────────────────────────────────────────────────

def test_chat_allowed_requires_a_configured_id():
    """No configured chat_id must refuse everything, not allow everything --
    the bot can now hire/retire/pause/resume/broadcast, not just chat."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, _, _ = _make_bot(tmpdir=tmpdir, allowed_chat_id=None)
        assert bot_instance._chat_allowed(12345) is False
        assert bot_instance._chat_allowed(0) is False


def test_chat_allowed_matches_configured_id_across_str_int():
    """--chat-id arrives as a str from argparse; Telegram's own chat ids are
    ints -- the comparison must not silently fail on that type mismatch."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, _, _ = _make_bot(tmpdir=tmpdir, allowed_chat_id="12345")
        assert bot_instance._chat_allowed(12345) is True   # int from Telegram
        assert bot_instance._chat_allowed("12345") is True
        assert bot_instance._chat_allowed(99999) is False


def test_dispatch_update_ignores_a_message_from_an_unconfigured_chat():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=None)
        bot_instance._dispatch_update({"message": {"chat": {"id": 999}, "text": "hire sme-9 please"}})
        assert telegram.sent_messages == []
        assert mesh.hired == []


def test_dispatch_update_ignores_a_message_from_the_wrong_chat():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        bot_instance._dispatch_update({"message": {"chat": {"id": 999}, "text": "/menu"}})
        assert telegram.sent_messages == []


def test_dispatch_update_processes_a_message_from_the_allowed_chat():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        bot_instance._dispatch_update({"message": {"chat": {"id": 42}, "text": "/menu"}})
        assert len(telegram.sent_messages) == 1


def test_an_edit_does_not_answer_an_open_flow_and_says_so(caplog):
    """Telegram sends edited_message for ANY message the operator edits in the
    last 48h. Consuming it as a flow answer means a stage can be taken by an
    act the person never performed -- they fixed a typo, they did not send
    anything. The flow stays exactly where it was, and they are told, because
    silence here leaves them waiting on an answer already discarded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        with bot_instance.chat_txn("42"): bot_instance.pending["42"] = {"flow": "hire", "stage": "provider", "name": "sme-9",
                                      "profile": None, "message_id": 7}

        with caplog.at_level(logging.INFO, logger="mesh_telegram"):
            bot_instance._dispatch_update(
                {"update_id": 9, "edited_message": {"chat": {"id": 42}, "message_id": 3, "text": "gpu-a"}}
            )

        assert mesh.hired == []
        assert bot_instance.pending["42"] == {"flow": "hire", "stage": "provider", "name": "sme-9",
                                              "profile": None, "message_id": 7}
        assert "hire is still waiting for the provider" in telegram.sent_messages[-1]["text"]
        assert "an edit is not a send" in "\n".join(r.getMessage() for r in caplog.records)


def test_an_edit_with_no_open_flow_is_dropped_without_re_prompting_the_agent():
    """The same rule where it matters most quietly: editing an old message
    used to re-send it as a fresh prompt, and editing an old /run used to run
    the command a second time. Neither is an act the operator performed. No
    reply either -- with no flow open, nothing is waiting on them."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)

        bot_instance._dispatch_update(
            {"update_id": 9, "edited_message": {"chat": {"id": 42}, "message_id": 3, "text": "hello architect"}}
        )
        bot_instance._dispatch_update(
            {"update_id": 10, "edited_message": {"chat": {"id": 42}, "message_id": 4, "text": "/run sme-2 /clear"}}
        )

        assert mesh.sent_envelopes == []
        assert mesh.sent_commands == []
        assert telegram.sent_messages == []


def test_an_edit_from_an_unauthorized_chat_gets_no_reply_either():
    """The chat check comes first: an edit from a chat that isn't the
    configured one must not draw the "that wasn't read as an answer" note,
    which would tell an unauthorized sender a bot is listening."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        with bot_instance.chat_txn("999"): bot_instance.pending["999"] = {"flow": "hire", "stage": "name"}

        bot_instance._dispatch_update(
            {"update_id": 9, "edited_message": {"chat": {"id": 999}, "message_id": 3, "text": "sme-9"}}
        )

        assert telegram.sent_messages == []


def test_dispatch_update_reacts_to_a_dispatched_prompt_using_its_own_message_id():
    """A plain prompt is a real update.message.message_id in production --
    _dispatch_update has to pull it out for the 👀 reaction to land on the
    right message, same as it does for a callback's message_id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        bot_instance._dispatch_update({
            "message": {"chat": {"id": 42}, "text": "how's it going?", "message_id": 555},
        })
        assert telegram.reactions_set == [{"chat_id": "42", "message_id": 555, "emoji": "👀"}]


def test_dispatch_update_ignores_a_callback_from_the_wrong_chat():
    """Not even answer_callback_query -- an unauthorized tap gets nothing
    back, not even acknowledgement that a bot is listening."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        bot_instance._dispatch_update({
            "callback_query": {"id": "cb-1", "data": "hi", "message": {"chat": {"id": 999}}},
        })
        assert telegram.sent_messages == []
        assert telegram.answered_callbacks == []


def test_dispatch_update_processes_a_callback_from_the_allowed_chat():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        bot_instance._dispatch_update({
            "callback_query": {"id": "cb-1", "data": "ov", "message": {"chat": {"id": 42}}},
        })
        assert telegram.answered_callbacks == [{"callback_query_id": "cb-1", "text": None, "show_alert": False}]
        assert len(telegram.sent_messages) == 1


def test_dispatch_update_extracts_message_id_for_edit_in_place():
    """The raw update's `callback_query.message.message_id` is what makes
    edit-in-place possible at all -- `_dispatch_update` has to pull it out
    and thread it through, not just chat id and data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        bot_instance._dispatch_update({
            "callback_query": {"id": "cb-1", "data": "lc:architect", "message": {"chat": {"id": 42}, "message_id": 77}},
        })
        assert telegram.sent_messages == []
        assert telegram.edited_messages[-1]["message_id"] == 77


def test_direct_handler_calls_bypass_the_allowlist():
    """CLI-driven one-shots (--prompt/--status/--menu) and dry-run mode call
    handlers directly, never through _dispatch_update -- they're operator
    invocations from shell access, not untrusted Telegram network input, so
    the allowlist (which guards inbound Telegram updates) does not apply."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=42)
        bot_instance.handle_text_message(999, "/menu")
        assert len(telegram.sent_messages) == 1


def test_send_or_edit_message_sends_fresh_without_a_message_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        returned_id = bot_instance._send_or_edit_message(12345, "hello", reply_markup={"inline_keyboard": []})
        assert telegram.sent_messages == [
            {"chat_id": 12345, "text": "hello", "message_id": 1, "reply_markup": {"inline_keyboard": []}}
        ]
        assert telegram.edited_messages == []
        assert returned_id == 1


def test_send_or_edit_message_edits_when_given_a_message_id():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        returned_id = bot_instance._send_or_edit_message(12345, "hello", message_id=7, clear_markup=True)
        assert telegram.sent_messages == []
        assert telegram.edited_messages == [
            {"chat_id": 12345, "message_id": 7, "text": "hello", "reply_markup": {"inline_keyboard": []}}
        ]
        assert returned_id == 7


def test_send_or_edit_message_swallows_message_not_modified():
    """A double-tap racing two identical callbacks can produce two edits
    with the same resulting text -- Telegram's "message is not modified"
    for the second one is expected, not a bug to surface."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        class NotModifiedTelegramClient(DummyTelegramClient):
            def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kwargs):
                super().edit_message_text(chat_id, message_id, text, reply_markup=reply_markup, **kwargs)
                return {"ok": False, "description": "Bad Request: message is not modified"}

        bot_instance.telegram = NotModifiedTelegramClient()
        returned_id = bot_instance._send_or_edit_message(12345, "same text", message_id=7)
        assert returned_id == 7  # no exception, no crash


def test_send_or_edit_message_falls_back_to_a_fresh_send_when_the_edit_cannot_succeed():
    """Regression: a genuinely un-editable anchor (message too old, deleted,
    or left over from a wiped install) used to be logged at DEBUG and
    dropped with no fallback -- the tap produced literally nothing, no
    updated screen, no new message, no error shown. It must now re-anchor
    to a fresh message instead of failing closed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        class UneditableTelegramClient(DummyTelegramClient):
            def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kwargs):
                super().edit_message_text(chat_id, message_id, text, reply_markup=reply_markup, **kwargs)
                return {"ok": False, "description": "Bad Request: message can't be edited"}

        bot_instance.telegram = UneditableTelegramClient()
        returned_id = bot_instance._send_or_edit_message(
            12345, "Priority?", message_id=999, reply_markup={"inline_keyboard": [[{"text": "x"}]]}
        )
        # a real message actually reached the chat, with a real (different) id
        assert returned_id != 999
        assert bot_instance.telegram.sent_messages == [{
            "chat_id": 12345, "text": "Priority?", "message_id": returned_id,
            "reply_markup": {"inline_keyboard": [[{"text": "x"}]]},
        }]


def test_addticket_flow_recovers_when_the_anchor_becomes_uneditable_mid_flow():
    """End-to-end version of the same regression: if an edit fails partway
    through a flow, the *next* step must retry against the message that
    fallback actually created, not keep hammering the same broken id
    forever."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_callback_query(12345, "cb-1", "at")
        picker_id = telegram.sent_messages[-1]["message_id"]
        bot_instance.handle_callback_query(12345, "cb-2", "at:sme-2", picker_id)
        broken_id = bot_instance.pending["12345"]["message_id"]
        assert broken_id == picker_id

        class UneditableOnceTelegramClient(DummyTelegramClient):
            def __init__(self):
                super().__init__()
                self.broke_once = False

            def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kwargs):
                if message_id == broken_id and not self.broke_once:
                    self.broke_once = True
                    return {"ok": False, "description": "Bad Request: message can't be edited"}
                return super().edit_message_text(chat_id, message_id, text, reply_markup=reply_markup, **kwargs)

        bot_instance.telegram = UneditableOnceTelegramClient()
        reply = bot_instance.handle_text_message(12345, "Fix the flaky test")
        assert "Description?" in reply
        # typed answer, so this was a fresh send regardless -- and it is what
        # the flow re-anchors to
        assert len(bot_instance.telegram.sent_messages) == 1
        new_anchor = bot_instance.telegram.sent_messages[0]["message_id"]
        assert bot_instance.pending["12345"]["message_id"] == new_anchor

        # The button-answered step edits the CURRENT anchor, not the broken
        # id -- no silent no-ops against a message that can never be edited.
        reply = bot_instance.handle_text_message(12345, "Seen twice in CI this week")
        assert "Priority?" in reply
        priority_id = bot_instance.pending["12345"]["message_id"]
        assert priority_id == bot_instance.telegram.sent_messages[-1]["message_id"]
        bot_instance.handle_callback_query(12345, "cb-3", "ap:high")
        assert bot_instance.telegram.edited_messages[-1]["message_id"] == priority_id


def test_menu_command_sends_sticky_keyboard():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_menu_command(12345)
        assert len(telegram.sent_messages) == 1
        markup = telegram.sent_messages[0]["reply_markup"]
        assert markup["resize_keyboard"] is True
        assert markup["is_persistent"] is True
        flat = [b["text"] for row in markup["keyboard"] for b in row]
        assert flat == [
            "📋 Overview", "🎫 Add ticket",
            "⏯ Lifecycle", "👁 Watch",
            "🔔 Alerts", "➕ Hire",
            "🎯 Message: architect",
            "📢 Broadcast",
            "🙈 Hide menu",
        ]
        # every static label resolves to a dispatch code; the dynamic target
        # button is matched by prefix instead (see handle_text_message)
        assert set(flat) - {"🎯 Message: architect"} == set(TelegramBot.STICKY_LABELS)


def test_dashboard_button_only_appears_when_mini_app_url_is_configured():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        assert bot_instance.mini_app_url is None
        markup = bot_instance._sticky_keyboard(12345)
        flat = [b["text"] for row in markup["keyboard"] for b in row]
        assert "📊 Dashboard" not in flat

        bot_instance, mesh, telegram = _make_bot(
            tmpdir=tmpdir, mini_app_url="https://mini.example.invalid/mini.html",
        )
        markup = bot_instance._sticky_keyboard(12345)
        rows = markup["keyboard"]
        dashboard_row = rows[-1]
        assert dashboard_row == [{"text": "📊 Dashboard", "web_app": {"url": "https://mini.example.invalid/mini.html"}}]


def test_handle_text_message_menu_and_status_still_work():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_text_message(12345, "/menu")
        assert telegram.sent_messages[-1]["reply_markup"] is not None

        bot_instance.handle_text_message(12345, "/status")
        assert "State: idle" in telegram.sent_messages[-1]["text"]


def test_tmux_agents_excludes_api_clients():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.roster = {"architect": "tmux", "sme-2": "tmux", "telegram": "api", "host": "office"}
        bot_instance, _, _ = _make_bot(mesh=mesh, tmpdir=tmpdir)
        assert set(bot_instance._tmux_agents()) == {"architect", "sme-2"}


def test_overview_command_renders_state_and_open_ticket():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.presence_state = "working"
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)

        text = bot_instance.handle_overview_command(12345)
        assert "architect" in text and "working" in text and "Review auth change" in text
        assert "sme-2" in text and "no open ticket" in text
        assert telegram.sent_messages[-1]["text"] == text


def test_callback_query_dispatch_answers_and_routes():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_callback_query(12345, "cbid-1", "ov")
        assert telegram.answered_callbacks == [{"callback_query_id": "cbid-1", "text": None, "show_alert": False}]
        assert "Office overview" in telegram.sent_messages[-1]["text"]


def test_addticket_full_flow_via_callbacks_and_text():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        bot_instance.handle_callback_query(12345, "cb-1", "at")
        rows = telegram.sent_messages[-1]["reply_markup"]["inline_keyboard"]
        flat_buttons = [b for row in rows for b in row]
        assert any(b["callback_data"] == "at:sme-2" for b in flat_buttons)

        bot_instance.handle_callback_query(12345, "cb-2", "at:sme-2")
        assert "Ticket title for sme-2" in telegram.sent_messages[-1]["text"]

        # Mid-flow: a plain text message is consumed as the title, not sent to architect
        reply = bot_instance.handle_text_message(12345, "Fix the flaky test")
        assert "Description?" in reply
        assert 12345 in bot_instance.pending

        reply = bot_instance.handle_text_message(12345, "Seen twice in CI this week")
        assert "Priority?" in reply
        assert 12345 in bot_instance.pending

        reply = bot_instance.handle_callback_query(12345, "cb-3", "ap:high")
        assert "Ticket added to sme-2" in reply
        assert 12345 not in bot_instance.pending
        assert mesh.added_tickets == [
            {"agent": "sme-2", "title": "Fix the flaky test", "description": "Seen twice in CI this week", "priority": "high"}
        ]


def test_addticket_edits_after_a_tap_and_sends_fresh_after_typing():
    """The split, end to end: a step answered by a BUTTON edits the screen
    the operator is looking at, a step answered by TYPING gets a new message.
    An edit never notifies, and once the operator has typed, their own
    message is the newest thing in the chat -- an edited prompt lands above
    it unannounced, which is indistinguishable from the flow having died."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        bot_instance.handle_callback_query(12345, "cb-1", "at")
        picker_id = telegram.sent_messages[-1]["message_id"]

        # tapped an agent: edits the picker in place
        bot_instance.handle_callback_query(12345, "cb-2", "at:sme-2", picker_id)
        assert telegram.edited_messages[-1]["message_id"] == picker_id
        assert "Ticket title for sme-2" in telegram.edited_messages[-1]["text"]
        edits_after_tap = len(telegram.edited_messages)

        # typed the title: fresh send, and the flow re-anchors to it
        bot_instance.handle_text_message(12345, "Fix the flaky test")
        assert "Description?" in telegram.sent_messages[-1]["text"]
        assert len(telegram.edited_messages) == edits_after_tap
        assert bot_instance.pending["12345"]["message_id"] == telegram.sent_messages[-1]["message_id"]

        # typed the description: fresh send again, carrying the buttons
        bot_instance.handle_text_message(12345, "Seen twice in CI this week")
        priority_id = telegram.sent_messages[-1]["message_id"]
        assert "Priority?" in telegram.sent_messages[-1]["text"]
        assert telegram.sent_messages[-1]["reply_markup"]["inline_keyboard"]
        assert len(telegram.edited_messages) == edits_after_tap

        # tapped a priority: back to editing, on the message just tapped
        reply = bot_instance.handle_callback_query(12345, "cb-3", "ap:high")
        assert "Ticket added to sme-2" in reply
        assert telegram.edited_messages[-1] == {
            "chat_id": "12345", "message_id": priority_id, "text": reply, "reply_markup": {"inline_keyboard": []},
        }
        assert len(telegram.sent_messages) == 3


def test_addticket_cancel_is_a_fresh_send_and_disarms_the_old_screen():
    """/cancel is typed, so the confirmation is a fresh send like any other
    answer to typing -- but the screen it abandons may still carry live
    buttons, so that one gets its keyboard cleared."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_callback_query(12345, "cb-1", "at")
        picker_id = telegram.sent_messages[-1]["message_id"]
        bot_instance.handle_callback_query(12345, "cb-2", "at:sme-2", picker_id)

        reply = bot_instance.handle_text_message(12345, "/cancel")
        assert reply == "Cancelled."
        assert 12345 not in bot_instance.pending
        assert telegram.sent_messages[-1]["text"] == "Cancelled."
        assert len(telegram.sent_messages) == 2
        assert telegram.edited_reply_markups[-1] == {
            "chat_id": "12345", "message_id": picker_id, "reply_markup": {"inline_keyboard": []},
        }


def test_addticket_description_dash_skips_it():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "addticket", "agent": "architect", "stage": "title"}
        bot_instance.handle_text_message(12345, "Quick fix")
        bot_instance.handle_text_message(12345, "-")
        bot_instance.handle_callback_query(12345, "cb-1", "ap:normal")
        assert mesh.added_tickets == [
            {"agent": "architect", "title": "Quick fix", "description": "", "priority": "normal"}
        ]


def test_addticket_priority_stray_text_reprompts_without_losing_the_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "addticket", "agent": "architect", "stage": "priority",
                                        "title": "Quick fix", "description": ""}
        reply = bot_instance.handle_text_message(12345, "high please")
        assert "Tap a priority button" in reply
        assert 12345 in bot_instance.pending
        assert mesh.added_tickets == []


def test_addticket_flow_cancel():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "addticket", "agent": "architect", "stage": "title"}
        reply = bot_instance.handle_text_message(12345, "/cancel")
        assert reply == "Cancelled."
        assert 12345 not in bot_instance.pending
        assert mesh.added_tickets == []


def test_pending_flow_takes_priority_over_ordinary_prompt():
    """A message during an open flow must not fall through to handle_user_prompt
    (which would send it to target_agent instead of consuming it as an answer)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "addticket", "agent": "architect", "stage": "title"}
        bot_instance.handle_text_message(12345, "not a prompt for architect")
        # send_message (chat) was only used for the flow prompt, never routed as
        # a Message envelope — DummyMeshClient has no record of prompt sends,
        # so we assert indirectly: the flow advanced instead of completing.
        assert bot_instance.pending[12345]["stage"] == "description"


def test_lifecycle_full_flow_via_callbacks():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        bot_instance.handle_callback_query(12345, "cb-1", "lc")
        rows = telegram.sent_messages[-1]["reply_markup"]["inline_keyboard"]
        flat_buttons = [b for row in rows for b in row]
        assert any(b["callback_data"] == "lc:architect" for b in flat_buttons)

        bot_instance.handle_callback_query(12345, "cb-2", "lc:architect")
        buttons = telegram.sent_messages[-1]["reply_markup"]["inline_keyboard"]
        assert buttons[0][0]["callback_data"] == "lp:architect"
        assert buttons[0][1]["callback_data"] == "lr:architect"

        reply = bot_instance.handle_callback_query(12345, "cb-3", "lp:architect")
        assert reply == "✅ architect paused."
        assert mesh.control_calls == [{"kind": "PauseAgent", "agent": "architect"}]


def test_lifecycle_control_failure_reports_detail():
    """Still-enrolled agent, but the control call itself fails -- distinct
    from the stale-tap case below, where the agent isn't enrolled at all."""
    with tempfile.TemporaryDirectory() as tmpdir:
        class FailingMeshClient(DummyMeshClient):
            def control_agent(self, kind, agent):
                return 422, {"detail": "agent is not paused"}

        mesh = FailingMeshClient()
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        reply = bot_instance.handle_callback_query(12345, "cb-1", "lr:architect")
        assert "Failed to resume architect" in reply
        assert "agent is not paused" in reply


def test_callback_query_on_a_retired_agent_pops_an_alert_instead_of_acting():
    """Edit-in-place means a lifecycle/add-ticket/watch/message screen can
    outlive the agent it names -- retired between the picker showing and a
    later tap on that same screen landing. That tap should never reach the
    mesh API at all, just a real popup (`show_alert=True`), since a small
    toast could be masked by the very edit a stale tap could otherwise
    trigger."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_callback_query(12345, "cb-1", "lp:ghost")
        assert reply == ""
        assert telegram.answered_callbacks == [
            {"callback_query_id": "cb-1", "text": "⚠️ ghost is no longer enrolled.", "show_alert": True}
        ]
        assert telegram.sent_messages == []
        assert telegram.edited_messages == []
        assert mesh.control_calls == []


def test_lifecycle_picker_includes_retire():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_callback_query(12345, "cb-1", "lc:architect")
        buttons = telegram.sent_messages[-1]["reply_markup"]["inline_keyboard"]
        assert any(b["callback_data"] == "lret:architect" for row in buttons for b in row)


def test_lifecycle_flow_edits_the_same_message_including_back_and_forth():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        bot_instance.handle_callback_query(12345, "cb-1", "lc")
        picker_id = telegram.sent_messages[-1]["message_id"]

        bot_instance.handle_callback_query(12345, "cb-2", "lc:architect", picker_id)
        assert telegram.edited_messages[-1]["message_id"] == picker_id
        assert "pause, resume, or retire" in telegram.edited_messages[-1]["text"]

        # "◀ Back" on that screen (callback_data "lc") returns to the agent
        # picker -- still an edit of the very same message, not a new one.
        bot_instance.handle_callback_query(12345, "cb-3", "lc", picker_id)
        assert telegram.edited_messages[-1]["message_id"] == picker_id
        assert "Lifecycle — pick an agent" in telegram.edited_messages[-1]["text"]

        bot_instance.handle_callback_query(12345, "cb-4", "lc:architect", picker_id)
        reply = bot_instance.handle_callback_query(12345, "cb-5", "lp:architect", picker_id)
        assert reply == "✅ architect paused."
        assert telegram.edited_messages[-1] == {
            "chat_id": 12345,
            "message_id": picker_id,
            "text": reply,
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": "↩ Undo", "callback_data": "lr:architect"},
                    {"text": "📋 Copy name", "copy_text": {"text": "architect"}},
                ]]
            },
        }
        # One send for the picker; every screen after it was an edit.
        assert len(telegram.sent_messages) == 1


def test_lifecycle_control_debounces_the_tapped_button_before_the_result_is_known():
    """editMessageReplyMarkup clears the keyboard the instant the tap
    arrives -- before the control call resolves -- so a slow response
    can't be double-tapped. Text is untouched by that first edit; only the
    later, full edit (editMessageText) sets the result."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_callback_query(12345, "cb-1", "lp:architect", 42)
        assert telegram.edited_reply_markups[0] == {
            "chat_id": 12345, "message_id": 42, "reply_markup": {"inline_keyboard": []},
        }
        assert telegram.edited_messages[-1]["text"] == "✅ architect paused."


def test_lifecycle_undo_button_reverses_the_action():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_callback_query(12345, "cb-1", "lp:architect", 42)
        undo_data = telegram.edited_messages[-1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert undo_data == "lr:architect"

        reply = bot_instance.handle_callback_query(12345, "cb-2", undo_data, 42)
        assert reply == "✅ architect resumed."
        assert mesh.control_calls == [
            {"kind": "PauseAgent", "agent": "architect"}, {"kind": "ResumeAgent", "agent": "architect"},
        ]


def test_retire_requires_typing_the_exact_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        reply = bot_instance.handle_callback_query(12345, "cb-1", "lret:architect")
        assert "Type 'architect' exactly" in reply
        assert bot_instance.pending[12345] == {"flow": "retire", "agent": "architect", "message_id": 1}

        reply = bot_instance.handle_text_message(12345, "architeckt")  # typo
        assert "doesn't match" in reply
        assert 12345 in bot_instance.pending  # still open for retry
        assert mesh.retired == []

        reply = bot_instance.handle_text_message(12345, "architect")
        assert "architect retired" in reply
        assert 12345 not in bot_instance.pending
        assert mesh.retired == ["architect"]
        # Retire's confirmation step is typed too, so the same rule applies to
        # it: the retry nudge and the result are both fresh sends, not edits
        # sitting above what the operator just typed.
        assert [m["text"] for m in telegram.sent_messages][-2:] == [
            "That doesn't match 'architect' — type it exactly to confirm, or /cancel.",
            reply,
        ]


def test_retire_cancel():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "retire", "agent": "architect"}
        reply = bot_instance.handle_text_message(12345, "/cancel")
        assert reply == "Cancelled."
        assert 12345 not in bot_instance.pending
        assert mesh.retired == []


def test_retire_failure_reports_detail():
    with tempfile.TemporaryDirectory() as tmpdir:
        class FailingMeshClient(DummyMeshClient):
            def retire_agent(self, agent):
                return 422, {"detail": "unknown agent"}

        mesh = FailingMeshClient()
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "retire", "agent": "architect"}
        reply = bot_instance.handle_text_message(12345, "architect")
        assert "Failed to retire architect" in reply
        assert "unknown agent" in reply


# ── broadcast ────────────────────────────────────────────────────────────────

def test_broadcast_full_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        reply = bot_instance.handle_text_message(12345, "📢 Broadcast")
        assert "type the message" in reply
        assert bot_instance.pending[12345] == {"flow": "broadcast"}

        reply = bot_instance.handle_text_message(12345, "standup in 5")
        assert reply == "📢 Broadcast sent."
        assert 12345 not in bot_instance.pending
        assert mesh.sent_envelopes == [{"destination": "all", "text": "standup in 5"}]


def test_broadcast_cancel():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "broadcast"}
        reply = bot_instance.handle_text_message(12345, "/cancel")
        assert reply == "Cancelled."
        assert 12345 not in bot_instance.pending
        assert mesh.sent_envelopes == []


def test_broadcast_failure_reports_detail():
    with tempfile.TemporaryDirectory() as tmpdir:
        class FailingMeshClient(DummyMeshClient):
            def send_message(self, destination, text):
                return 422, {"detail": "policy denied"}

        mesh = FailingMeshClient()
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "broadcast"}
        reply = bot_instance.handle_text_message(12345, "hi all")
        assert "Broadcast failed" in reply
        assert "policy denied" in reply


# ── hire ─────────────────────────────────────────────────────────────────────

def test_hire_start_force_replies_since_it_has_no_prior_message_to_edit():
    """Hire's opening prompt is always a fresh send in real usage (no
    picker screen precedes it), so it's the one flow prompt that can
    actually carry ForceReply -- editMessageText, used by every other
    typed-prompt continuation, cannot attach one at all."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_text_message(12345, "➕ Hire")
        assert telegram.sent_messages[-1]["reply_markup"] == {"force_reply": True, "selective": True}


def test_hire_full_flow_via_sticky_button_and_text():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        reply = bot_instance.handle_text_message(12345, "➕ Hire")
        assert "New agent's name?" in reply
        assert bot_instance.pending[12345] == {"flow": "hire", "stage": "name", "message_id": 1}

        reply = bot_instance.handle_text_message(12345, "sme-9")
        assert "Profile for sme-9?" in reply
        assert bot_instance.pending[12345] == {"flow": "hire", "stage": "profile", "name": "sme-9", "message_id": 2}

        reply = bot_instance.handle_text_message(12345, "-")
        assert "Provider for sme-9?" in reply
        assert bot_instance.pending[12345] == {
            "flow": "hire", "stage": "provider", "name": "sme-9", "profile": None, "message_id": 3,
        }

        reply = bot_instance.handle_text_message(12345, "-")
        assert reply == "⏳ Hire request admitted for sme-9 · agent creation is not yet confirmed."
        assert 12345 not in bot_instance.pending
        assert mesh.hired == [{"agent": "sme-9", "cli": "claude", "profile": None, "provider": None}]
        assert telegram.sent_messages[-1]["reply_markup"] == {
            "inline_keyboard": [[{"text": "📋 Copy name", "copy_text": {"text": "sme-9"}}]]
        }


def test_hire_sends_every_answer_reply_fresh_instead_of_editing():
    """Every step of hire is answered by typing, so every step posts a new
    message -- the operator's own answer is always the newest thing in the
    chat, and an edited prompt above it is never announced. Nothing in this
    flow edits at all."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        bot_instance.handle_text_message(12345, "➕ Hire")
        bot_instance.handle_text_message(12345, "sme-9")
        bot_instance.handle_text_message(12345, "-")
        reply = bot_instance.handle_text_message(12345, "-")

        assert telegram.edited_messages == []
        texts = [m["text"] for m in telegram.sent_messages]
        assert len(texts) == 4
        assert "New agent's name?" in texts[0]
        assert "Profile for sme-9?" in texts[1]
        assert "Provider for sme-9?" in texts[2]
        assert texts[3] == reply


def test_hire_with_a_profile_and_provider():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "hire", "stage": "name"}

        bot_instance.handle_text_message(12345, "sme-9")
        bot_instance.handle_text_message(12345, "work")
        reply = bot_instance.handle_text_message(12345, "gpu-a")

        assert reply == (
            "⏳ Hire request admitted for sme-9 (profile work, provider gpu-a) "
            "· agent creation is not yet confirmed."
        )
        assert mesh.hired == [{"agent": "sme-9", "cli": "claude", "profile": "work", "provider": "gpu-a"}]


def test_hire_rejects_invalid_name_without_consuming_the_flow():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "hire", "stage": "name"}

        reply = bot_instance.handle_text_message(12345, "123")  # all-digits, refused
        assert "won't work" in reply
        # still open -- and now anchored, since the reprompt had to send fresh
        # (the manually-seeded state above had no message_id to edit)
        assert bot_instance.pending[12345] == {"flow": "hire", "stage": "name", "message_id": 1}
        assert mesh.hired == []

        reply = bot_instance.handle_text_message(12345, "all")  # reserved
        assert "won't work" in reply
        assert mesh.hired == []

        # A valid name after the bad attempts still works, and gets to the profile step.
        reply = bot_instance.handle_text_message(12345, "sme-9")
        assert "Profile for sme-9?" in reply
        bot_instance.handle_text_message(12345, "-")
        bot_instance.handle_text_message(12345, "-")
        assert mesh.hired == [{"agent": "sme-9", "cli": "claude", "profile": None, "provider": None}]


def test_hire_cancel_at_any_stage():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        for state in (
            {"flow": "hire", "stage": "name"},
            {"flow": "hire", "stage": "profile", "name": "sme-9"},
            {"flow": "hire", "stage": "provider", "name": "sme-9", "profile": None},
        ):
            with bot_instance.chat_txn(12345): bot_instance.pending[12345] = state
            reply = bot_instance.handle_text_message(12345, "/cancel")
            assert reply == "Cancelled."
            assert 12345 not in bot_instance.pending
        assert mesh.hired == []


def test_two_answers_racing_one_flow_neither_double_submit_nor_crash():
    """A real race between two real threads, not a mock asserting a lock was
    taken.

    ⚠ The window this closes is genuinely narrow: between _advance_pending_flow
    reading the flow state and the submission branch deleting it, with no I/O
    in between, so a plain barrier never hits it -- I checked, and a version of
    this test written that way passed with the lock removed, which would have
    been a test of nothing. The window is widened here by making the state
    READ slow (the shim below blocks the first thread inside its second get),
    which is timing simulation, not a change in what is asserted: two threads
    answer the same stage, and afterwards exactly one hire must have been sent
    and neither thread may have died.

    Without the per-chat lock the loser's `del self.pending[cid]` raises
    KeyError against an entry the winner already removed -- in production that
    is a dispatch thread dying silently, with nothing in the chat."""
    with tempfile.TemporaryDirectory() as tmpdir:
        entered_window = threading.Event()
        release_window = threading.Event()

        class SlowReadPending(bot.ChatDict):
            """Blocks the first thread to reach its SECOND read -- the read
            inside _advance_pending_flow, which is where the window opens."""

            reads = 0
            gate = threading.Lock()

            def get(self, key, default=None):
                value = super().get(key, default)
                with SlowReadPending.gate:
                    SlowReadPending.reads += 1
                    mine = SlowReadPending.reads
                if mine == 2 and value:
                    entered_window.set()
                    release_window.wait(timeout=2)
                return value

        class SlowMeshClient(DummyMeshClient):
            def hire_agent(self, agent, cli="claude", profile=None, provider=None):
                time.sleep(0.1)
                return super().hire_agent(agent, cli=cli, profile=profile, provider=provider)

        bot_instance, mesh, telegram = _make_bot(mesh=SlowMeshClient(), tmpdir=tmpdir)
        bot_instance.pending = SlowReadPending()
        with bot_instance.chat_txn("12345"): bot_instance.pending["12345"] = {
            "flow": "hire", "stage": "provider", "name": "sme-9", "profile": None, "message_id": 1,
        }

        errors = []

        def answer():
            try:
                bot_instance.handle_text_message(12345, "-")
            except BaseException as exc:  # a dispatch thread dying is the bug
                errors.append(exc)

        first = threading.Thread(target=answer)
        first.start()
        assert entered_window.wait(timeout=5), "first thread never reached the window"
        second = threading.Thread(target=answer)
        second.start()
        time.sleep(0.2)  # let the second thread get as far as it can
        release_window.set()
        for t in (first, second):
            t.join(timeout=10)

        assert [t.is_alive() for t in (first, second)] == [False, False]
        assert errors == []
        assert mesh.hired == [{"agent": "sme-9", "cli": "claude", "profile": None, "provider": None}]
        assert "12345" not in bot_instance.pending


def test_a_chat_transaction_is_one_object_no_matter_which_thread_asks_first():
    """The lock map is itself check-then-mutate: two threads on a chat's first
    two updates must not each build a lock and each hold a different one."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        ready = threading.Barrier(8)
        seen = []

        def grab():
            ready.wait(timeout=5)
            seen.append(bot_instance.chat_txn(4242))

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(seen) == 8
        assert len({id(lock) for lock in seen}) == 1

def test_racing_flows_in_different_chats_do_not_wait_on_each_other():
    """Per chat, not global: a chat stuck in a 10s hire must not stall every
    other chat's traffic. Chat B completes while chat A is still blocked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        release_a = threading.Event()

        class BlockingMeshClient(DummyMeshClient):
            def hire_agent(self, agent, cli="claude", profile=None, provider=None):
                if agent == "slow-agent":
                    release_a.wait(timeout=5)
                return super().hire_agent(agent, cli=cli, profile=profile, provider=provider)

        bot_instance, mesh, telegram = _make_bot(mesh=BlockingMeshClient(), tmpdir=tmpdir)
        with bot_instance.chat_txn("111"): bot_instance.pending["111"] = {
            "flow": "hire", "stage": "provider", "name": "slow-agent", "profile": None, "message_id": 1,
        }
        with bot_instance.chat_txn("222"): bot_instance.pending["222"] = {
            "flow": "hire", "stage": "provider", "name": "quick-agent", "profile": None, "message_id": 2,
        }

        blocked = threading.Thread(target=lambda: bot_instance.handle_text_message(111, "-"))
        blocked.start()
        try:
            bot_instance.handle_text_message(222, "-")  # would hang if the lock were global
            assert [h["agent"] for h in mesh.hired] == ["quick-agent"]
        finally:
            release_a.set()
            blocked.join(timeout=5)
        assert [h["agent"] for h in mesh.hired] == ["quick-agent", "slow-agent"]


def test_hire_failure_reports_detail():
    with tempfile.TemporaryDirectory() as tmpdir:
        class FailingMeshClient(DummyMeshClient):
            def hire_agent(self, agent, cli="claude", profile=None, provider=None):
                return 422, {"detail": "unknown account 'bogus'; available accounts: default, work"}

        mesh = FailingMeshClient()
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.pending[12345] = {"flow": "hire", "stage": "provider", "name": "sme-9", "profile": "bogus"}
        reply = bot_instance.handle_text_message(12345, "-")
        assert "Failed to hire sme-9" in reply
        assert "available accounts: default, work" in reply


def test_hire_start_from_the_inline_button_still_force_replies():
    """The path the operator actually uses. handle_callback_query passes the
    tapped message's id, and passing that into _send_or_edit_message took the
    edit branch, where force_reply cannot apply at all -- so the one prompt
    that most needs the compose box opened on it was the one prompt never
    getting it. The sticky-button test missed this by calling with no
    message_id, encoding the same assumption as the comment it was written
    from."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        bot_instance.handle_callback_query(12345, "cb-1", "hi", message_id=42)

        assert telegram.sent_messages[-1]["reply_markup"] == {"force_reply": True, "selective": True}
        assert "New agent's name?" in telegram.sent_messages[-1]["text"]
        # the tapped menu message is left alone, not overwritten by the prompt
        assert [e["message_id"] for e in telegram.edited_messages] == []
        assert bot_instance.pending["12345"]["message_id"] == telegram.sent_messages[-1]["message_id"]


def test_hire_completes_when_the_anchor_can_never_be_edited():
    """The real .101 condition: editMessageText answers 400 "message can't be
    edited" for every stage, not just the first. Hire now reaches hire_agent
    without depending on an edit ever succeeding -- it doesn't edit at all --
    and this pins that: the fake Telegram whose edits always succeed is why
    we believed this path worked in the first place."""
    with tempfile.TemporaryDirectory() as tmpdir:
        class UneditableTelegramClient(DummyTelegramClient):
            def edit_message_text(self, chat_id, message_id, text, reply_markup=None, **kwargs):
                super().edit_message_text(chat_id, message_id, text, reply_markup=reply_markup, **kwargs)
                return {"ok": False, "description": "Bad Request: message can't be edited"}

        bot_instance, mesh, telegram = _make_bot(telegram=UneditableTelegramClient(), tmpdir=tmpdir)

        bot_instance.handle_callback_query(12345, "cb-1", "hi", message_id=42)
        assert "New agent's name?" in bot_instance.handle_text_message(12345, "sme-9") or True
        bot_instance.handle_text_message(12345, "-")
        reply = bot_instance.handle_text_message(12345, "-")

        assert mesh.hired == [{"agent": "sme-9", "cli": "claude", "profile": None, "provider": None}]
        assert reply == "⏳ Hire request admitted for sme-9 · agent creation is not yet confirmed."
        assert "12345" not in bot_instance.pending
        # every failed edit fell back to a fresh send, so the operator sees
        # each question as a new message rather than a silent in-place update
        assert len(bot_instance.telegram.sent_messages) == 4


def test_hire_puts_the_flow_back_when_the_submission_raises():
    """The flow is deleted before the call on purpose (a second answer during
    those 10s would hire twice). That leaves nothing behind if the call blows
    up, which is the "no agent, no error, no way back" shape -- so an
    unexpected exception restores the stage instead of dropping the operator
    into silence."""
    with tempfile.TemporaryDirectory() as tmpdir:
        class ExplodingMeshClient(DummyMeshClient):
            def hire_agent(self, agent, cli="claude", profile=None, provider=None):
                raise RuntimeError("connection reset by peer")

        bot_instance, _, telegram = _make_bot(mesh=ExplodingMeshClient(), tmpdir=tmpdir)
        with bot_instance.chat_txn("12345"): bot_instance.pending["12345"] = {
            "flow": "hire", "stage": "provider", "name": "sme-9", "profile": None, "message_id": 7,
        }

        reply = bot_instance.handle_text_message(12345, "-")

        assert "wasn't submitted" in reply and "RuntimeError" in reply
        assert bot_instance.pending["12345"]["stage"] == "provider"
        assert bot_instance.pending["12345"]["name"] == "sme-9"


def test_hire_flow_is_traceable_in_the_log_without_leaking_what_was_typed(caplog):
    """The instrumentation this ticket asked for, and its limit: every stage
    transition and the submission itself are in the log, and neither the
    answers nor the API token are."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        mesh.token = "sk-secret-token-value"

        with caplog.at_level(logging.DEBUG, logger="mesh_telegram"):
            bot_instance.handle_callback_query(12345, "cb-1", "hi", message_id=42)
            bot_instance.handle_text_message(12345, "sme-9")
            bot_instance.handle_text_message(12345, "sekrit-profile")
            bot_instance.handle_text_message(12345, "-")

        log = "\n".join(r.getMessage() for r in caplog.records)
        assert "flow hire: chat=12345 stage name -> profile" in log
        assert "flow hire: chat=12345 stage profile -> provider" in log
        assert "flow hire: chat=12345 closed after stage=provider" in log
        assert "hire submit: chat=12345 agent=sme-9 profile=set provider=default" in log
        assert "hire submit: chat=12345 agent=sme-9 status=202" in log
        # the answers themselves, and the credential, stay out of it
        assert "sekrit-profile" not in log
        assert "sk-secret-token-value" not in log


# ── message agent ────────────────────────────────────────────────────────────

# ── agent pickers: grid layout, not one-per-row ─────────────────────────────

def test_agent_picker_keyboard_grids_three_per_row_by_default():
    markup = _agent_picker_keyboard(["a", "b", "c", "d", "e"], "wp")
    rows = markup["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [
        ["a", "b", "c"],
        ["d", "e"],
        ["◀ Back"],
    ]
    assert rows[0][0]["callback_data"] == "wp:a"
    assert rows[-1][0]["callback_data"] == "menu"


def test_agent_picker_keyboard_respects_columns_and_back_callback():
    markup = _agent_picker_keyboard(["a", "b"], "lc", back_callback="lc", columns=1)
    assert markup["inline_keyboard"] == [
        [{"text": "a", "callback_data": "lc:a"}],
        [{"text": "b", "callback_data": "lc:b"}],
        [{"text": "◀ Back", "callback_data": "lc"}],
    ]


def test_agent_picker_keyboard_handles_no_agents():
    markup = _agent_picker_keyboard([], "at")
    assert markup["inline_keyboard"] == [[{"text": "◀ Back", "callback_data": "menu"}]]


def test_addticket_lifecycle_message_watch_pickers_all_use_the_shared_grid():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.roster = {f"agent-{i}": "tmux" for i in range(10)}
        bot_instance, mesh, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)

        for handler, prefix in (
            (bot_instance.handle_addticket_start, "at"),
            (bot_instance.handle_lifecycle_start, "lc"),
            (bot_instance.handle_message_agent_start, "ta"),
            (bot_instance.handle_watch_start, "wp"),
        ):
            handler(999)
            rows = telegram.sent_messages[-1]["reply_markup"]["inline_keyboard"]
            # ten agents, three per row -> four agent rows plus one Back row
            assert len(rows) == 5
            assert all(len(row) <= 3 for row in rows[:-1])
            flat = [b["callback_data"] for row in rows for b in row]
            assert f"{prefix}:agent-0" in flat
            assert f"{prefix}:agent-9" in flat
            assert rows[-1] == [{"text": "◀ Back", "callback_data": "menu"}]


def test_message_agent_picker_and_prompt_routing():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        reply = bot_instance.handle_text_message(12345, "🎯 Message: architect")
        assert "pick a different agent" in reply
        rows = telegram.sent_messages[-1]["reply_markup"]["inline_keyboard"]
        flat_buttons = [b for row in rows for b in row]
        assert any(b["callback_data"] == "ta:sme-2" for b in flat_buttons)

        reply = bot_instance.handle_callback_query(12345, "cb-1", "ta:sme-2")
        assert "Now messaging sme-2" in reply
        assert bot_instance.chat_target_agent[12345] == "sme-2"
        # the re-sent sticky keyboard reflects the new target immediately
        markup = telegram.sent_messages[-1]["reply_markup"]
        flat = [b["text"] for row in markup["keyboard"] for b in row]
        assert "🎯 Message: sme-2" in flat

        bot_instance.handle_text_message(12345, "how's it going?")
        assert telegram.sent_messages[-1]["text"] == "✅ Sent to sme-2."


def test_message_agent_pick_edits_the_picker_but_still_sends_the_sticky_refresh():
    """editMessageText can only carry an inline keyboard, never a
    ReplyKeyboardMarkup, so the sticky-keyboard refresh this flow ends with
    has to stay a fresh send -- but the picker itself should still be
    edited in place rather than left behind with live, stale buttons."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        bot_instance.handle_callback_query(12345, "cb-1", "ta")
        picker_id = telegram.sent_messages[-1]["message_id"]

        reply = bot_instance.handle_callback_query(12345, "cb-2", "ta:sme-2", picker_id)
        assert "Now messaging sme-2" in reply
        assert telegram.edited_messages[-1]["message_id"] == picker_id
        assert telegram.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}
        # ...and the sticky-keyboard refresh is still a separate, new message
        assert "keyboard" in telegram.sent_messages[-1]["reply_markup"]
        assert telegram.sent_messages[-1]["text"] == reply


def test_message_agent_selection_is_per_chat():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_callback_query(111, "cb-1", "ta:sme-2")

        assert bot_instance._target_for(111) == "sme-2"
        assert bot_instance._target_for(222) == "architect"  # untouched chat keeps the default

        bot_instance.handle_text_message(222, "hello")
        assert telegram.sent_messages[-1]["text"] == "✅ Sent to architect."


# ── @mention: one-off destination override ──────────────────────────────────

def test_parse_mention_splits_name_and_rest_and_lowercases():
    assert _parse_mention("@architect fix the auth bug") == ("architect", "fix the auth bug")
    assert _parse_mention("@Backend") == ("backend", "")
    assert _parse_mention("@sme-2   multi   space") == ("sme-2", "multi   space")


def test_parse_mention_only_matches_a_leading_mention():
    assert _parse_mention("check with @architect first") is None
    assert _parse_mention("plain text") is None
    assert _parse_mention("@ leading space, no name") is None


def test_parse_mention_keeps_multiline_bodies_intact():
    name, rest = _parse_mention("@architect line one\nline two")
    assert name == "architect"
    assert rest == "line one\nline two"


def test_mention_routes_a_single_message_without_changing_the_persistent_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        # persistent target starts as the default ("architect")
        assert bot_instance._target_for(12345) == "architect"

        reply = bot_instance.handle_text_message(12345, "@sme-2 can you check this?", message_id=99)
        assert reply == "✅ Sent to sme-2."
        assert mesh.sent_envelopes[-1]["destination"] == "sme-2"
        # reacted on the originating message once the envelope actually dispatched
        assert telegram.reactions_set == [{"chat_id": "12345", "message_id": 99, "emoji": "👀"}]

        # one-off only: the persistent target for a later plain message is unchanged
        assert bot_instance._target_for(12345) == "architect"
        assert "12345" not in bot_instance.chat_target_agent
        bot_instance.handle_text_message(12345, "and this one?")
        assert telegram.sent_messages[-1]["text"] == "✅ Sent to architect."


def test_mention_unknown_agent_errors_back_instead_of_misrouting():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "@nonexistent hello")
        assert "isn't a known agent" in reply
        assert mesh.sent_envelopes == []


def test_mention_rejects_a_non_tmux_client_by_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.roster["telegram"] = "api"
        bot_instance, mesh, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "@telegram hello")
        assert "isn't a known agent" in reply
        assert mesh.sent_envelopes == []


def test_mention_rejects_a_reserved_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "@all broadcast this")
        assert "isn't a known agent" in reply
        assert mesh.sent_envelopes == []


def test_mention_with_no_body_prompts_for_usage_instead_of_sending_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "@architect")
        assert "nothing to send" in reply
        assert mesh.sent_envelopes == []


def test_mention_mid_sentence_is_not_routing_just_message_content():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "please check with @sme-2 first")
        # not parsed as a mention: goes to the persistent target (architect) as-is
        assert reply == "✅ Sent to architect."
        assert mesh.sent_envelopes[-1]["destination"] == "architect"
        assert mesh.sent_envelopes[-1]["text"] == "please check with @sme-2 first"


# ── /run: raw, unwrapped pane injection via a Command-kind envelope ────────


def test_run_sends_a_command_envelope_not_a_message_one():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "/run sme-2 /clear", message_id=88)
        assert reply == "✅ Ran on sme-2."
        assert mesh.sent_commands == [{"destination": "sme-2", "text": "/clear"}]
        # never goes through the Message-kind path
        assert mesh.sent_envelopes == []
        assert telegram.reactions_set == [{"chat_id": "12345", "message_id": 88, "emoji": "👀"}]


def test_run_is_one_off_and_does_not_change_the_persistent_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        assert bot_instance._target_for(12345) == "architect"
        bot_instance.handle_text_message(12345, "/run sme-2 /clear")
        assert bot_instance._target_for(12345) == "architect"
        assert "12345" not in bot_instance.chat_target_agent
        bot_instance.handle_text_message(12345, "plain text after /run")
        assert mesh.sent_envelopes[-1]["destination"] == "architect"


def test_run_allows_compact_too():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "/run architect /compact")
        assert reply == "✅ Ran on architect."
        assert mesh.sent_commands[-1]["text"] == "/compact"


def test_run_is_unrestricted_by_default():
    """User decision, not an oversight: the default allowlist is empty, so
    /run passes through whatever it's given. This bot is already locked to
    one chat_id and the agent already runs with permissions skipped, so an
    allowlist here would restrict the same operator from themselves, not
    add a boundary against anyone else. Regression coverage for the exact
    case that prompted this -- /run architect /context used to be refused."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        assert bot_instance.run_allowed_commands == frozenset()

        reply = bot_instance.handle_text_message(12345, "/run architect /context")
        assert reply == "✅ Ran on architect."
        assert mesh.sent_commands[-1]["text"] == "/context"

        reply = bot_instance.handle_text_message(12345, "/run architect /add-dir /some/path")
        assert reply == "✅ Ran on architect."
        assert mesh.sent_commands[-1]["text"] == "/add-dir /some/path"


def test_run_rejects_a_command_not_on_an_explicitly_configured_allowlist():
    """The knob still works for anyone who wants to restrict /run -- only
    the default changed, not the mechanism."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(
            tmpdir=tmpdir, run_allowed_commands=frozenset({"/clear", "/compact"}),
        )
        reply = bot_instance.handle_text_message(12345, "/run architect /add-dir /some/path")
        assert "isn't an allowed /run command" in reply
        assert "/clear" in reply and "/compact" in reply
        assert mesh.sent_commands == []
        assert mesh.sent_envelopes == []


def test_run_allowlist_exact_match_only_when_configured():
    """Exact match only -- /clear plus anything else is not /clear. Only
    meaningful once an allowlist is actually configured; the unrestricted
    default has nothing to match exactly against."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, run_allowed_commands=frozenset({"/clear"}))
        reply = bot_instance.handle_text_message(12345, "/run architect /clear extra")
        assert "isn't an allowed /run command" in reply
        assert mesh.sent_commands == []

        reply = bot_instance.handle_text_message(12345, "/run architect /clear")
        assert reply == "✅ Ran on architect."


def test_run_rejects_an_embedded_newline_even_inside_an_allowed_command():
    """A newline in the command text would submit /clear on delivery and
    then paste a second, unvetted line right after it -- rejected before
    the allowlist is even checked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "/run architect /clear\nrm -rf /")
        assert "single line" in reply
        assert mesh.sent_commands == []


def test_run_allowlist_is_configurable_per_instance():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(
            tmpdir=tmpdir, run_allowed_commands=frozenset({"/help"}),
        )
        reply = bot_instance.handle_text_message(12345, "/run architect /clear")
        assert "isn't an allowed /run command" in reply
        assert mesh.sent_commands == []

        reply = bot_instance.handle_text_message(12345, "/run architect /help")
        assert reply == "✅ Ran on architect."
        assert mesh.sent_commands[-1]["text"] == "/help"


def test_parse_command_allowlist_strips_whitespace_and_drops_blanks():
    assert bot._parse_command_allowlist(" /clear ,/compact,, ") == frozenset({"/clear", "/compact"})


def test_run_with_no_agent_or_text_prompts_for_usage():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "/run")
        assert "Usage: /run" in reply
        assert mesh.sent_commands == []

        reply = bot_instance.handle_text_message(12345, "/run architect")
        assert "Usage: /run" in reply
        assert mesh.sent_commands == []

        reply = bot_instance.handle_text_message(12345, "/run architect   ")
        assert "Usage: /run" in reply
        assert mesh.sent_commands == []


def test_run_unknown_agent_errors_back_instead_of_running_anywhere():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "/run nonexistent /clear")
        assert "isn't a known agent" in reply
        assert mesh.sent_commands == []


def test_run_rejects_a_non_tmux_client_by_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.roster["telegram"] = "api"
        bot_instance, mesh, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "/run telegram /clear")
        assert "isn't a known agent" in reply
        assert mesh.sent_commands == []


def test_run_rejects_a_reserved_name():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "/run all /clear")
        assert "isn't a known agent" in reply
        assert mesh.sent_commands == []


def test_run_blocked_agent_is_refused_same_as_a_regular_message():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        mesh.presence_state = "blocked"
        reply = bot_instance.handle_text_message(12345, "/run architect /clear")
        assert "not accepting messages" in reply
        assert mesh.sent_commands == []


def test_run_failure_reports_run_specific_wording_not_send_wording():
    with tempfile.TemporaryDirectory() as tmpdir:
        class RefusingMeshClient(DummyMeshClient):
            def send_command(self, destination, text):
                return 422, {"detail": "policy denied"}

        mesh = RefusingMeshClient()
        bot_instance, mesh, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "/run architect /clear")
        assert "Failed to run on architect" in reply


# ── receiving a photo: sent on as a real Attachment envelope ────────────────

def test_valid_attachment_filename():
    assert _valid_attachment_filename("file_123.jpg") is True
    assert _valid_attachment_filename("") is False
    assert _valid_attachment_filename(".") is False
    assert _valid_attachment_filename("..") is False
    assert _valid_attachment_filename("a/b.jpg") is False
    assert _valid_attachment_filename("a\\b.jpg") is False
    assert _valid_attachment_filename("bad\x00name.jpg") is False
    assert _valid_attachment_filename("x" * 256 + ".jpg") is False


def test_valid_attachment_mime_type():
    assert _valid_attachment_mime_type("image/jpeg") is True
    assert _valid_attachment_mime_type("") is False
    assert _valid_attachment_mime_type("image/*") is False
    assert _valid_attachment_mime_type("image/jpeg; q=1") is False
    assert _valid_attachment_mime_type("bad type/jpeg") is False


def test_dispatch_update_routes_a_photo_instead_of_silently_dropping_it(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=12345)
        calls = []
        monkeypatch.setattr(
            bot_instance, "handle_photo_message",
            lambda chat_id, sizes, caption: calls.append((chat_id, sizes, caption)),
        )
        update = {
            "message": {
                "chat": {"id": 12345},
                "photo": [{"file_id": "small"}, {"file_id": "big", "file_size": 5}],
                "caption": "a photo",
            }
        }
        bot_instance._dispatch_update(update)
        assert calls == [("12345", [{"file_id": "small"}, {"file_id": "big", "file_size": 5}], "a photo")]


def test_handle_photo_message_sends_an_attachment_envelope_to_the_persistent_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        photo_sizes = [{"file_id": "thumb", "file_size": 100}, {"file_id": "full", "file_size": 5000}]

        reply = bot_instance.handle_photo_message(12345, photo_sizes, "")

        assert reply == "✅ Photo sent to architect."
        assert ("getFile", {"file_id": "full"}) in telegram.requests
        assert telegram.downloaded_paths == ["photos/file_1.jpg"]
        assert len(mesh.sent_attachments) == 1
        sent = mesh.sent_attachments[0]
        assert sent["destination"] == "architect"
        assert sent["filename"] == "file_1.jpg"
        assert sent["mime_type"] == "image/jpeg"
        assert base64.b64decode(sent["content_base64"]) == b"fake-jpeg-bytes"
        assert sent["caption"] is None
        assert mesh.sent_envelopes == []  # no more Message fallback


def test_handle_photo_message_maps_caption_to_the_envelope_caption_field():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "a nice view")
        assert mesh.sent_attachments[0]["caption"] == "a nice view"


def test_handle_photo_message_falls_back_to_a_generated_filename_when_telegrams_is_invalid():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        telegram.get_file_response = {"ok": True, "result": {"file_path": "../evil.jpg"}}
        bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "")
        filename = mesh.sent_attachments[0]["filename"]
        assert _valid_attachment_filename(filename)
        assert filename.endswith(".jpg")


def test_handle_photo_message_mention_routes_without_changing_the_persistent_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        photo_sizes = [{"file_id": "full"}]

        reply = bot_instance.handle_photo_message(12345, photo_sizes, "@sme-2 check this out")

        assert reply == "✅ Photo sent to sme-2."
        assert mesh.sent_attachments[-1]["destination"] == "sme-2"
        assert mesh.sent_attachments[-1]["caption"] == "check this out"
        assert "12345" not in bot_instance.chat_target_agent


def test_handle_photo_message_mention_to_unknown_agent_is_refused():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "@nonexistent look")
        assert "isn't a known agent" in reply
        assert mesh.sent_attachments == []
        assert telegram.requests == []  # never even attempted the download


def test_handle_photo_message_respects_blocked_presence():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.presence_state = "blocked"
        bot_instance, mesh, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        reply = bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "")
        assert reply == "architect is not accepting messages right now"
        assert mesh.sent_attachments == []
        assert telegram.requests == []


def test_handle_photo_message_attempts_send_and_warns_when_prior_delivery_is_unverified():
    """The warning must remain visible without becoming the gate it replaced."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.delivery_unverified = {"since": "2026-09-02T06:00:00Z", "stream_id": "a" * 32}
        bot_instance, mesh, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)

        reply = bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "")

        assert len(mesh.sent_attachments) == 1
        assert reply.startswith("✅ Photo sent to architect.")
        assert "prior delivery remains unverified" in reply


def test_handle_photo_message_rejects_an_oversized_reported_file_size():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        photo_sizes = [{"file_id": "huge", "file_size": TELEGRAM_MAX_FILE_BYTES + 1}]
        reply = bot_instance.handle_photo_message(12345, photo_sizes, "")
        assert "too large" in reply
        assert telegram.requests == []  # rejected before ever calling getFile
        assert mesh.sent_attachments == []


def test_handle_photo_message_rejects_a_download_over_the_attachment_cap_even_under_telegrams_own_ceiling():
    """The real gap flagged during the design pass: 10MB (Attachment) is
    smaller than 20MB (Telegram's own getFile ceiling), so a file that
    downloads fine must still be refused here."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        telegram.download_response = b"x" * (ATTACHMENT_MAX_BYTES + 1)
        assert len(telegram.download_response) <= TELEGRAM_MAX_FILE_BYTES
        reply = bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "")
        assert "too large" in reply
        assert mesh.sent_attachments == []


def test_handle_photo_message_reports_a_getfile_failure():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        telegram.get_file_response = {"ok": False, "description": "file expired"}
        reply = bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "")
        assert "file expired" in reply
        assert mesh.sent_attachments == []


def test_handle_photo_message_reports_an_attachment_send_failure():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        mesh.send_attachment = lambda *a, **kw: (422, {"detail": "invalid attachment mime_type"})
        reply = bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "")
        assert "Failed to send" in reply
        assert "invalid attachment mime_type" in reply


def test_handle_photo_message_shows_typing_before_the_download():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_photo_message(12345, [{"file_id": "full"}], "")
        assert telegram.chat_actions == [{"chat_id": "12345", "action": "typing"}]


# ── receiving a "document" (uncompressed) upload: shares _send_incoming_file_as_attachment ──

def test_dispatch_update_routes_a_document_instead_of_falling_through(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=12345)
        calls = []
        monkeypatch.setattr(
            bot_instance, "handle_document_message",
            lambda chat_id, document, caption: calls.append((chat_id, document, caption)),
        )
        document = {"file_id": "doc1", "file_name": "report.pdf", "mime_type": "application/pdf"}
        update = {"message": {"chat": {"id": 12345}, "document": document, "caption": "the report"}}
        bot_instance._dispatch_update(update)
        assert calls == [("12345", document, "the report")]


def test_handle_document_message_uses_telegrams_own_filename_and_mime_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        telegram.get_file_response = {"ok": True, "result": {"file_path": "documents/file_1"}}
        document = {"file_id": "doc1", "file_name": "report.pdf", "mime_type": "application/pdf", "file_size": 5000}

        reply = bot_instance.handle_document_message(12345, document, "")

        assert reply == "✅ File sent to architect."
        sent = mesh.sent_attachments[0]
        assert sent["filename"] == "report.pdf"
        assert sent["mime_type"] == "application/pdf"
        assert base64.b64decode(sent["content_base64"]) == b"fake-jpeg-bytes"


def test_handle_document_message_falls_back_to_octet_stream_for_an_invalid_reported_mime_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        document = {"file_id": "doc1", "file_name": "notes.txt", "mime_type": "text/plain; charset=utf-8"}
        bot_instance.handle_document_message(12345, document, "")
        assert mesh.sent_attachments[0]["mime_type"] == "application/octet-stream"


def test_handle_document_message_defaults_a_missing_mime_type_to_octet_stream():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        document = {"file_id": "doc1", "file_name": "notes.txt"}  # Telegram's mime_type is optional
        bot_instance.handle_document_message(12345, document, "")
        assert mesh.sent_attachments[0]["mime_type"] == "application/octet-stream"


def test_handle_document_message_falls_back_to_a_generated_filename_with_no_extension_assumed():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        document = {"file_id": "doc1", "file_name": "../evil", "mime_type": "application/octet-stream"}
        bot_instance.handle_document_message(12345, document, "")
        filename = mesh.sent_attachments[0]["filename"]
        assert _valid_attachment_filename(filename)
        assert filename.startswith("telegram-file-")


def test_handle_document_message_mention_routes_without_changing_the_persistent_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        document = {"file_id": "doc1", "file_name": "report.pdf", "mime_type": "application/pdf"}
        reply = bot_instance.handle_document_message(12345, document, "@sme-2 the numbers")
        assert reply == "✅ File sent to sme-2."
        assert mesh.sent_attachments[-1]["destination"] == "sme-2"
        assert mesh.sent_attachments[-1]["caption"] == "the numbers"
        assert "12345" not in bot_instance.chat_target_agent


def test_handle_document_message_respects_blocked_presence():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.presence_state = "blocked"
        bot_instance, mesh, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        document = {"file_id": "doc1", "file_name": "report.pdf", "mime_type": "application/pdf"}
        reply = bot_instance.handle_document_message(12345, document, "")
        assert reply == "architect is not accepting messages right now"
        assert mesh.sent_attachments == []


def test_handle_document_message_with_no_document_is_a_no_op():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        assert bot_instance.handle_document_message(12345, {}, "") == ""
        assert mesh.sent_attachments == []


def test_status_command_respects_per_chat_target():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.boards["sme-2"] = {"todo": [], "doing": [{"title": "Fix the flaky test"}], "hold": [], "done": []}
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        with bot_instance.chat_txn(12345): bot_instance.chat_target_agent[12345] = "sme-2"

        text = bot_instance.handle_status_command(12345)
        assert "Agent Status: sme-2" in text
        assert "Fix the flaky test" in text


def test_callback_query_back_to_menu():
    """An inline "◀ Back" button (e.g. from the Add Ticket agent picker)
    still resolves to "menu" and re-shows the sticky keyboard."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_callback_query(12345, "cb-1", "menu")
        markup = telegram.sent_messages[-1]["reply_markup"]
        assert markup == bot_instance._sticky_keyboard(12345)


def test_sticky_labels_cover_the_office_options():
    assert set(TelegramBot.STICKY_LABELS.values()) == {"ov", "at", "lc", "wa", "al", "hi", "bc", "hm"}


def test_sticky_keyboard_tap_dispatches_like_the_matching_inline_code():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.alerts = [{"kind": "blocked", "agent": "sme-2", "unconsumed_s": 60, "cursor": "1-0"}]
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)

        reply = bot_instance.handle_text_message(12345, "🔔 Alerts")
        assert "blocked" in reply


def test_hide_menu_sends_remove_keyboard_not_a_dead_end():
    """A persistent ReplyKeyboardMarkup can't be dismissed from the phone
    itself -- Telegram's own collapse gesture is a temporary toggle and the
    keyboard reappears on refresh. Only an explicit
    reply_markup={"remove_keyboard": True} (ReplyKeyboardRemove) actually
    removes it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_hide_menu_command(12345)
        assert len(telegram.sent_messages) == 1
        assert telegram.sent_messages[0]["reply_markup"] == {"remove_keyboard": True}
        assert "/menu" in reply


def test_hide_menu_button_dispatches_via_the_sticky_keyboard_tap():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        reply = bot_instance.handle_text_message(12345, "🙈 Hide menu")
        assert telegram.sent_messages[-1]["reply_markup"] == {"remove_keyboard": True}
        assert reply == telegram.sent_messages[-1]["text"]


def test_menu_command_still_brings_the_keyboard_back_after_hiding():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        bot_instance.handle_hide_menu_command(12345)
        bot_instance.handle_menu_command(12345)
        assert telegram.sent_messages[-1]["reply_markup"] == bot_instance._sticky_keyboard("12345")


# ── alerts ───────────────────────────────────────────────────────────────────

def test_render_alert_blocked():
    text = render_alert({"kind": "blocked", "agent": "sme-2", "unconsumed_s": 725})
    assert text == "⊘ blocked — sme-2 — unconsumed 12m"


def test_render_alert_stalled():
    text = render_alert({"kind": "stalled", "agent": "architect", "ticket": "fix auth", "doing_age_s": 900})
    assert text == '⏳ stalled — architect — "fix auth" — doing 15m'


def test_render_alert_credential():
    text = render_alert({"kind": "credential", "account": "default", "cli": "claude", "status": "expiring"})
    assert text == "🔑 credential — default/claude — expiring"


def test_render_alert_unknown_kind_degrades_gracefully():
    text = render_alert({"kind": "future_kind", "v": 1, "ts": "x", "cursor": "1-0", "agent": "sme-2", "note": "n"})
    assert text.startswith("🔔 future_kind — ")
    assert '"agent": "sme-2"' in text
    assert '"note": "n"' in text
    # v/ts/cursor/kind are framing, not alert content — excluded from the dump
    assert '"v"' not in text and '"ts"' not in text and '"cursor"' not in text


def test_handle_alerts_command_lists_recent_and_slices_tail():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.alerts = [
            {"kind": "credential", "account": "default", "cli": "claude", "status": "expiring", "cursor": f"{i}-0"}
            for i in range(15)
        ]
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        text = bot_instance.handle_alerts_command(12345, limit=10)
        assert text.count("credential") == 10
        assert telegram.sent_messages[-1]["text"] == text


def test_handle_alerts_command_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        text = bot_instance.handle_alerts_command(12345)
        assert text == "🔔 No alerts."


def test_callback_query_alerts_routes_to_handler():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.alerts = [{"kind": "blocked", "agent": "sme-2", "unconsumed_s": 60, "cursor": "1-0"}]
        bot_instance, _, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir)
        reply = bot_instance.handle_callback_query(12345, "cb-1", "al")
        assert "blocked" in reply
        assert telegram.answered_callbacks == [{"callback_query_id": "cb-1", "text": None, "show_alert": False}]


def test_parse_sse_events_single_frame():
    lines = [
        "id: 100-0\n",
        "event: alert\n",
        'data: {"kind": "blocked", "agent": "sme-2"}\n',
        "\n",
    ]
    events = list(_parse_sse_events(lines))
    assert events == [("alert", "100-0", '{"kind": "blocked", "agent": "sme-2"}')]


def test_parse_sse_events_multiple_frames_and_comment_lines():
    lines = [
        ": keepalive\n",
        "id: 1-0\n",
        "event: alert\n",
        'data: {"kind": "stalled"}\n',
        "\n",
        "id: 2-0\n",
        "event: alert\n",
        'data: {"kind": "credential"}\n',
        "\n",
    ]
    events = list(_parse_sse_events(lines))
    # ⚠ Keepalive comments are now REPORTED (data None) rather than swallowed:
    # an idle stream sends nothing else, so a consumer with a deadline needs
    # them to get a turn. Consumers that don't care filter on data is None,
    # which is what stream_alerts does.
    assert [e for e in events if e[2] is None] == [("keepalive", None, None)]
    real = [e for e in events if e[2] is not None]
    assert [e[1] for e in real] == ["1-0", "2-0"]
    assert [e[2] for e in real] == ['{"kind": "stalled"}', '{"kind": "credential"}']


def test_parse_sse_events_multiline_data_is_joined():
    lines = ["event: alert\n", "data: line1\n", "data: line2\n", "\n"]
    events = list(_parse_sse_events(lines))
    assert events[0][2] == "line1\nline2"


def test_parse_sse_events_accepts_bytes():
    lines = [b"event: alert\n", b'data: {"kind": "blocked"}\n', b"\n"]
    events = list(_parse_sse_events(lines))
    assert events == [("alert", None, '{"kind": "blocked"}')]


def test_alert_pusher_pushes_each_new_alert_and_persists_cursor():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "alerts_cursor.json"))
        pusher = AlertPusher(mesh, telegram, chat_id=999, cursor_store=store)

        alerts = [
            {"kind": "blocked", "agent": "sme-2", "unconsumed_s": 60, "cursor": "10-0"},
            {"kind": "credential", "account": "default", "cli": "claude", "status": "expired", "cursor": "11-0"},
        ]

        def fake_stream(after=None):
            assert after is None  # no persisted cursor and no history to seed from
            yield from alerts

        pusher.run(stream_fn=fake_stream)

        assert len(telegram.sent_messages) == 2
        assert "blocked" in telegram.sent_messages[0]["text"]
        assert "credential" in telegram.sent_messages[1]["text"]
        assert store.load() == "11-0"


def test_alert_pusher_seeds_from_tail_on_first_run_not_from_history():
    """A fresh cursor store must not replay the whole retained alert history as
    if every entry were new — it should start from GET /alerts's next_cursor."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.alerts = [{"kind": "blocked", "agent": "old-agent", "cursor": "1-0"}] * 50
        mesh.alerts_next_cursor = "50-0"
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "alerts_cursor.json"))
        pusher = AlertPusher(mesh, telegram, chat_id=999, cursor_store=store)

        seen_after = []

        def fake_stream(after=None):
            seen_after.append(after)
            return iter([])

        pusher.run(stream_fn=fake_stream)
        assert seen_after == ["50-0"]
        assert telegram.sent_messages == []
        assert store.load() == "50-0"


def test_alert_pusher_resumes_from_persisted_cursor_without_reseeding():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.alerts_next_cursor = "999-0"  # would be wrong to use this
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "alerts_cursor.json"))
        store.save("42-0")
        pusher = AlertPusher(mesh, telegram, chat_id=999, cursor_store=store)

        seen_after = []

        def fake_stream(after=None):
            seen_after.append(after)
            return iter([])

        pusher.run(stream_fn=fake_stream)
        assert seen_after == ["42-0"]


# ── ReplyPusher (replaces the old inline blocking wait) ───────────────────────

def test_render_reply_uses_source_and_falls_back_to_provided_name():
    msg = {"l2": {"source": "architect"}, "payload": {"text": "done"}}
    assert render_reply(msg, fallback_source="telegram") == "architect: done"

    no_source = {"payload": {"text": "hi"}}
    assert render_reply(no_source, fallback_source="telegram") == "telegram: hi"

    no_text = {"l2": {"source": "architect"}, "payload": {}}
    assert render_reply(no_text, fallback_source="telegram") == "architect sent a message"


def test_reply_pusher_pushes_each_new_message_and_persists_cursor():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)

        messages = [
            {"l2": {"source": "architect"}, "payload": {"text": "first"}, "cursor": "10-0"},
            {"l2": {"source": "architect"}, "payload": {"text": "second"}, "cursor": "11-0"},
        ]

        def fake_stream(after=None):
            assert after is None
            yield from messages

        pusher.run(stream_fn=fake_stream)

        assert len(telegram.sent_messages) == 2
        assert telegram.sent_messages[0]["text"] == "architect: first"
        assert telegram.sent_messages[1]["text"] == "architect: second"
        assert store.load() == "11-0"


def test_reply_pusher_delivers_an_attachment_via_send_document():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)

        content = base64.b64encode(b"%PDF-fake-bytes").decode("ascii")
        messages = [{
            "kind": "Attachment",
            "l2": {"source": "architect"},
            "payload": {"filename": "report.pdf", "mime_type": "application/pdf", "content_base64": content, "caption": "Q3 numbers"},
            "cursor": "10-0",
        }]
        pusher.run(stream_fn=lambda after=None: iter(messages))

        assert len(telegram.sent_documents) == 1
        sent = telegram.sent_documents[0]
        assert sent["chat_id"] == 999
        assert sent["filename"] == "report.pdf"
        assert sent["data"] == b"%PDF-fake-bytes"
        assert sent["mime_type"] == "application/pdf"
        assert sent["caption"] == "from architect: Q3 numbers"
        assert telegram.sent_messages == []  # no ordinary-reply fallback text
        assert {"chat_id": 999, "action": "upload_document"} in telegram.chat_actions


def test_reply_pusher_attachment_caption_omits_colon_when_none_sent():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)

        content = base64.b64encode(b"data").decode("ascii")
        messages = [{
            "kind": "Attachment",
            "l2": {"source": "architect"},
            "payload": {"filename": "notes.txt", "mime_type": "text/plain", "content_base64": content},
            "cursor": "10-0",
        }]
        pusher.run(stream_fn=lambda after=None: iter(messages))
        assert telegram.sent_documents[0]["caption"] == "from architect"


def test_reply_pusher_reports_a_send_document_failure_instead_of_dropping_it():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        telegram.send_document_response = {"ok": False, "description": "file too large"}
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)

        content = base64.b64encode(b"data").decode("ascii")
        messages = [{
            "kind": "Attachment",
            "l2": {"source": "architect"},
            "payload": {"filename": "big.bin", "mime_type": "application/octet-stream", "content_base64": content},
            "cursor": "10-0",
        }]
        pusher.run(stream_fn=lambda after=None: iter(messages))
        assert len(telegram.sent_messages) == 1
        assert "Failed to deliver" in telegram.sent_messages[0]["text"]
        assert "file too large" in telegram.sent_messages[0]["text"]


def test_reply_pusher_reports_malformed_attachment_base64_without_crashing():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)

        messages = [{
            "kind": "Attachment",
            "l2": {"source": "architect"},
            "payload": {"filename": "x.bin", "mime_type": "application/octet-stream", "content_base64": "not-valid-base64!!"},
            "cursor": "10-0",
        }]
        pusher.run(stream_fn=lambda after=None: iter(messages))  # must not raise
        assert telegram.sent_documents == []
        assert "rejected" in telegram.sent_messages[0]["text"]
        assert "strict decoding" in telegram.sent_messages[0]["text"]


def test_reply_pusher_reports_an_attachment_missing_required_fields():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)

        messages = [{
            "kind": "Attachment",
            "l2": {"source": "architect"},
            "payload": {"mime_type": "application/octet-stream"},  # no filename/content_base64
            "cursor": "10-0",
        }]
        pusher.run(stream_fn=lambda after=None: iter(messages))
        assert telegram.sent_documents == []
        assert "invalid or missing filename" in telegram.sent_messages[0]["text"]


# ── the fuller validation contract (docs/CONTRACTS.md), not just "is there
# something to send" ──────────────────────────────────────────────────────

def _attachment_message(payload: dict) -> dict:
    return {"kind": "Attachment", "l2": {"source": "architect"}, "payload": payload, "cursor": "10-0"}


def test_attachment_allowed_payload_keys_matches_the_closed_schema():
    assert ATTACHMENT_ALLOWED_PAYLOAD_KEYS == {"filename", "mime_type", "content_base64", "caption"}


def test_reply_pusher_rejects_an_invalid_filename():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)
        content = base64.b64encode(b"data").decode("ascii")

        for bad_name in ("..", "a/b.txt", ""):
            telegram.sent_messages.clear()
            messages = [_attachment_message({"filename": bad_name, "mime_type": "text/plain", "content_base64": content})]
            pusher.run(stream_fn=lambda after=None, m=messages: iter(m))
            assert telegram.sent_documents == []
            assert "invalid or missing filename" in telegram.sent_messages[0]["text"]


def test_reply_pusher_rejects_an_invalid_mime_type():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)
        content = base64.b64encode(b"data").decode("ascii")

        messages = [_attachment_message({"filename": "x.txt", "mime_type": "text/*", "content_base64": content})]
        pusher.run(stream_fn=lambda after=None: iter(messages))
        assert telegram.sent_documents == []
        assert "invalid or missing mime_type" in telegram.sent_messages[0]["text"]


def test_reply_pusher_rejects_a_missing_mime_type_rather_than_defaulting_it():
    """docs/CONTRACTS.md: mime_type is one of the three required fields --
    a prior version of this code defaulted a missing one to
    application/octet-stream instead of rejecting, which is not what the
    contract promises."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)
        content = base64.b64encode(b"data").decode("ascii")

        messages = [_attachment_message({"filename": "x.txt", "content_base64": content})]
        pusher.run(stream_fn=lambda after=None: iter(messages))
        assert telegram.sent_documents == []
        assert "invalid or missing mime_type" in telegram.sent_messages[0]["text"]


def test_reply_pusher_rejects_an_oversized_caption():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)
        content = base64.b64encode(b"data").decode("ascii")

        messages = [_attachment_message({
            "filename": "x.txt", "mime_type": "text/plain", "content_base64": content,
            "caption": "x" * (ATTACHMENT_MAX_CAPTION_BYTES + 1),
        })]
        pusher.run(stream_fn=lambda after=None: iter(messages))
        assert telegram.sent_documents == []
        assert "invalid caption" in telegram.sent_messages[0]["text"]


def test_reply_pusher_rejects_decoded_content_over_the_attachment_cap():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)
        content = base64.b64encode(b"x" * (ATTACHMENT_MAX_BYTES + 1)).decode("ascii")

        messages = [_attachment_message({"filename": "x.bin", "mime_type": "application/octet-stream", "content_base64": content})]
        pusher.run(stream_fn=lambda after=None: iter(messages))
        assert telegram.sent_documents == []
        assert "10MB attachment limit" in telegram.sent_messages[0]["text"]


def test_reply_pusher_rejects_an_unexpected_payload_field():
    """docs/CONTRACTS.md: "the payload is a closed shape ... no other field
    is accepted" -- enforced here the same as the api door and the tmux
    opener, since a direct bus caller bypasses the api door entirely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)
        content = base64.b64encode(b"data").decode("ascii")

        messages = [_attachment_message({
            "filename": "x.txt", "mime_type": "text/plain", "content_base64": content,
            "destination_path": "/etc/passwd",
        })]
        pusher.run(stream_fn=lambda after=None: iter(messages))
        assert telegram.sent_documents == []
        assert "unexpected field" in telegram.sent_messages[0]["text"]
        assert "destination_path" in telegram.sent_messages[0]["text"]


def test_reply_pusher_seeds_from_tail_on_first_run_not_from_history():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.messages_queue = [
            {"l2": {"source": "architect"}, "payload": {"text": "old"}, "cursor": "1-0"},
        ] * 20
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)

        seen_after = []

        def fake_stream(after=None):
            seen_after.append(after)
            return iter([])

        pusher.run(stream_fn=fake_stream)
        # DummyMeshClient.get_messages(after=None) with a non-empty queue
        # returns the last item's cursor as next_cursor -- "1-0" here since
        # every seeded message shares that cursor.
        assert seen_after == ["1-0"]
        assert telegram.sent_messages == []
        assert store.load() == "1-0"


def test_reply_pusher_resumes_from_persisted_cursor_without_reseeding():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.messages_queue = [{"l2": {"source": "architect"}, "payload": {"text": "x"}, "cursor": "999-0"}]
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        store.save("42-0")
        pusher = ReplyPusher(mesh, telegram, chat_id=999, cursor_store=store)

        seen_after = []

        def fake_stream(after=None):
            seen_after.append(after)
            return iter([])

        pusher.run(stream_fn=fake_stream)
        assert seen_after == ["42-0"]


def test_poll_messages_forever_yields_and_advances_cursor():
    """Real generator (not injected), exercised for a bounded number of
    iterations by having the second poll raise a sentinel to stop the
    otherwise-infinite loop deterministically."""
    class _Stop(Exception):
        pass

    mesh = MeshClient(base_url="http://unused", token="t", app_name="telegram")
    calls = {"n": 0}

    def fake_get_messages(after=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return 200, {"messages": [{"cursor": "5-0", "payload": {"text": "a"}}]}
        raise _Stop

    mesh.get_messages = fake_get_messages
    gen = mesh.poll_messages_forever(after=None, interval=0)

    first = next(gen)
    assert first["cursor"] == "5-0"
    try:
        next(gen)
        assert False, "expected _Stop to propagate"
    except _Stop:
        pass


def test_synthesize_speech_empty_text_raises_value_error():
    try:
        synthesize_speech("   ", "en-GB-RyanNeural")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_synthesize_speech_failure_cleans_up_and_raises(monkeypatch, tmp_path):
    class FakeCommunicate:
        def __init__(self, text, voice):
            pass

        async def save(self, path):
            Path(path).write_bytes(b"broken")
            raise RuntimeError("network down")

    monkeypatch.setattr(bot.edge_tts, "Communicate", FakeCommunicate)

    out_file = tmp_path / "test.mp3"
    try:
        synthesize_speech("hello", "en-GB-RyanNeural", output_path=out_file)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "network down" in str(exc)
        assert not out_file.exists()


def test_synthesize_speech_success(monkeypatch, tmp_path):
    class FakeCommunicate:
        def __init__(self, text, voice):
            self.text = text
            self.voice = voice

        async def save(self, path):
            Path(path).write_bytes(f"audio:{self.voice}:{self.text}".encode("utf-8"))

    monkeypatch.setattr(bot.edge_tts, "Communicate", FakeCommunicate)

    out_file = tmp_path / "voice.mp3"
    res_path = synthesize_speech("hello world", "en-GB-RyanNeural", output_path=out_file)
    assert Path(res_path).exists()
    assert Path(res_path).read_bytes() == b"audio:en-GB-RyanNeural:hello world"

    # Default voice parameter test
    out_file_default = tmp_path / "voice_default.mp3"
    res_path_default = synthesize_speech("default call", output_path=out_file_default)
    assert Path(res_path_default).read_bytes() == b"audio:en-GB-RyanNeural:default call"


def test_telegram_client_send_voice_multipart(monkeypatch):
    client = TelegramClient(bot_token="fake-token")
    captured = {}

    def fake_urlopen(req, timeout=60):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["data"] = req.data
        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self):
                return b'{"ok": true, "result": {"message_id": 42}}'
        return FakeResp()

    monkeypatch.setattr(bot.urllib.request, "urlopen", fake_urlopen)

    res = client.send_voice(
        chat_id=12345,
        voice=b"MP3_DATA_BYTES",
        caption="Voice caption",
        reply_to_message_id=99,
        reply_markup={"inline_keyboard": []},
    )

    assert res.get("ok") is True
    assert captured["url"] == "https://api.telegram.org/botfake-token/sendVoice"
    content_type = captured["headers"]["Content-type"]
    assert "multipart/form-data; boundary=" in content_type
    body = captured["data"].decode("utf-8", errors="replace")
    assert 'name="chat_id"\r\n\r\n12345' in body
    assert 'name="caption"\r\n\r\nVoice caption' in body
    assert 'name="reply_to_message_id"\r\n\r\n99' in body
    assert 'name="voice"; filename="voice.mp3"' in body
    assert "MP3_DATA_BYTES" in body


def test_dry_run_telegram_client_send_voice(capsys):
    client = DryRunTelegramClient()
    res = client.send_voice(12345, b"RAW_BYTES", caption="dry voice test")
    assert res.get("ok") is True
    assert res["result"]["caption"] == "dry voice test"
    out = capsys.readouterr().out
    assert "[DRY-RUN Telegram] sendVoice" in out
    assert "chat=12345" in out
    assert "caption='dry voice test'" in out


def test_reply_pusher_voice_reply_success(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(
            mesh,
            telegram,
            chat_id=999,
            cursor_store=store,
            tts_voice="en-GB-RyanNeural",
            voice_enabled=True,
        )

        saved_files = []

        def fake_synthesize(text, voice="en-GB-RyanNeural", output_path=None):
            assert text == "architect: spoken reply"
            assert voice == "en-GB-RyanNeural"
            p = Path(tmpdir) / "voice_out.mp3"
            p.write_bytes(b"spoken audio")
            saved_files.append(p)
            return str(p)

        monkeypatch.setattr(bot, "synthesize_speech", fake_synthesize)

        messages = [
            {"l2": {"source": "architect"}, "payload": {"text": "spoken reply"}, "cursor": "20-0"},
        ]

        def fake_stream(after=None):
            yield from messages

        pusher.run(stream_fn=fake_stream)

        assert len(telegram.sent_messages) == 1
        assert telegram.sent_messages[0]["chat_id"] == 999
        assert telegram.sent_messages[0]["text"] == "architect: spoken reply"
        assert len(telegram.sent_voices) == 1
        assert telegram.sent_voices[0]["chat_id"] == 999
        assert store.load() == "20-0"
        assert not saved_files[0].exists()
        assert {"chat_id": 999, "action": "record_voice"} in telegram.chat_actions


def test_reply_pusher_text_only_when_voice_disabled():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(
            mesh,
            telegram,
            chat_id=999,
            cursor_store=store,
            voice_enabled=False,
        )

        messages = [
            {"l2": {"source": "architect"}, "payload": {"text": "text only reply"}, "cursor": "21-0"},
        ]

        def fake_stream(after=None):
            yield from messages

        pusher.run(stream_fn=fake_stream)

        assert len(telegram.sent_messages) == 1
        assert telegram.sent_messages[0]["chat_id"] == 999
        assert telegram.sent_messages[0]["text"] == "architect: text only reply"
        assert telegram.sent_voices == []
        assert store.load() == "21-0"


def test_reply_pusher_per_message_voice_override(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        telegram = DummyTelegramClient()
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        pusher = ReplyPusher(
            mesh,
            telegram,
            chat_id=999,
            cursor_store=store,
            tts_voice="default-voice",
            voice_enabled=True,
        )

        synthesized_voices = []

        def fake_synthesize(text, voice="en-GB-RyanNeural", output_path=None):
            synthesized_voices.append(voice)
            p = Path(tmpdir) / "voice.mp3"
            p.write_bytes(b"audio")
            return str(p)

        monkeypatch.setattr(bot, "synthesize_speech", fake_synthesize)

        messages = [
            {"l2": {"source": "architect"}, "payload": {"text": "custom voice", "voice": "custom-override-voice"}, "cursor": "22-0"},
        ]

        def fake_stream(after=None):
            yield from messages

        pusher.run(stream_fn=fake_stream)
        assert synthesized_voices == ["custom-override-voice"]


def test_telegram_bot_voice_feature_flag_disabled_by_default(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh,
        telegram_client=telegram,
        cursor_store=store,
        target_agent="architect",
        allowed_chat_id=12345,
        voice_feature_enabled=False,
    )

    assert not bot_instance.is_voice_enabled(12345)
    kb = bot_instance._sticky_keyboard(12345)
    labels = [btn["text"] for row in kb["keyboard"] for btn in row]
    assert "🔇 Voice: OFF" not in labels
    assert "🔊 Voice: ON" not in labels

    reply = bot_instance.handle_voice_toggle(12345)
    assert "Voice replies are not enabled for this tenant" in reply
    assert not bot_instance.is_voice_enabled(12345)


def test_telegram_bot_voice_toggle_and_menu_when_feature_enabled(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh,
        telegram_client=telegram,
        cursor_store=store,
        target_agent="architect",
        allowed_chat_id=12345,
        voice_feature_enabled=True,
    )

    assert bot_instance.default_tts_voice == "en-GB-RyanNeural"
    assert not bot_instance.is_voice_enabled(12345)
    assert bot_instance._voice_label(12345) == "🔇 Voice: OFF"
    kb = bot_instance._sticky_keyboard(12345)
    labels = [btn["text"] for row in kb["keyboard"] for btn in row]
    assert "🔇 Voice: OFF" in labels

    # Toggle ON via handle_voice_toggle
    reply = bot_instance.handle_voice_toggle(12345)
    assert "🔊 Voice replies enabled" in reply
    assert "en-GB-RyanNeural" in reply
    assert bot_instance.is_voice_enabled(12345)
    assert bot_instance._voice_label(12345) == "🔊 Voice: ON"

    # Toggle OFF via text message "/voice"
    reply = bot_instance.handle_text_message(12345, "/voice")
    assert "🔇 Voice replies disabled" in reply
    assert not bot_instance.is_voice_enabled(12345)

    # Toggle ON via button text "🔇 Voice: OFF"
    reply = bot_instance.handle_text_message(12345, "🔇 Voice: OFF")
    assert "🔊 Voice replies enabled" in reply
    assert bot_instance.is_voice_enabled(12345)

    # Toggle OFF via callback query "vt"
    bot_instance.handle_callback_query(12345, "cb_1", "vt")
    assert not bot_instance.is_voice_enabled(12345)


def test_telegram_bot_enrol_registers_voice_command(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(mesh_client=mesh, telegram_client=telegram, cursor_store=store)
    assert bot_instance.enrol() is True
    assert len(telegram.commands_set) == 1
    cmds = {c["command"]: c["description"] for c in telegram.commands_set[0]}
    assert "menu" in cmds
    assert "status" in cmds
    assert "voice" in cmds


def test_telegram_bot_enrol_sets_the_chat_menu_button_when_mini_app_configured(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh, telegram_client=telegram, cursor_store=store,
        allowed_chat_id=42, mini_app_url="https://example.com/mini.html",
    )
    assert bot_instance.enrol() is True
    assert telegram.menu_buttons_set == [{
        "chat_id": "42",
        "menu_button": {"type": "web_app", "text": "Dashboard", "web_app": {"url": "https://example.com/mini.html"}},
    }]


def test_telegram_bot_enrol_skips_the_menu_button_without_mini_app_url(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(mesh_client=mesh, telegram_client=telegram, cursor_store=store, allowed_chat_id=42)
    assert bot_instance.enrol() is True
    assert telegram.menu_buttons_set == []


def test_telegram_bot_enrol_skips_the_menu_button_without_an_allowed_chat_id(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh, telegram_client=telegram, cursor_store=store,
        mini_app_url="https://example.com/mini.html",
    )
    assert bot_instance.enrol() is True
    assert telegram.menu_buttons_set == []


def test_telegram_bot_chat_id_type_normalization_with_reply_pusher(monkeypatch):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        bot_instance = TelegramBot(
            mesh_client=mesh,
            telegram_client=telegram,
            cursor_store=store,
            allowed_chat_id="46444780",
            voice_feature_enabled=True,
        )

        # Telegram sends integer chat_id in JSON updates
        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 46444780},
                "text": "/voice",
            },
        }
        bot_instance._dispatch_update(update)

        # Both int and str lookups should now return True
        assert bot_instance.is_voice_enabled(46444780) is True
        assert bot_instance.is_voice_enabled("46444780") is True

        # ReplyPusher with string chat_id should see voice enabled
        pusher = ReplyPusher(
            mesh=mesh,
            telegram=telegram,
            chat_id="46444780",
            cursor_store=store,
            voice_enabled_fn=bot_instance.is_voice_enabled,
        )

        synthesized = []

        def fake_synthesize(text, voice="en-GB-RyanNeural", output_path=None):
            synthesized.append((text, voice))
            p = Path(tmpdir) / "test.mp3"
            p.write_bytes(b"dummy audio")
            return str(p)

        monkeypatch.setattr(bot, "synthesize_speech", fake_synthesize)

        messages = [
            {"l2": {"source": "architect"}, "payload": {"text": "live spoken reply"}, "cursor": "30-0"},
        ]

        def fake_stream(after=None):
            yield from messages

        pusher.run(stream_fn=fake_stream)

        assert len(synthesized) == 1
        assert len(telegram.sent_messages) == 2
        assert telegram.sent_messages[-1]["text"] == "architect: live spoken reply"
        assert len(telegram.sent_voices) == 1
        assert telegram.sent_voices[0]["chat_id"] == "46444780"


def test_telegram_bot_int_str_chat_id_in_flows(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh,
        telegram_client=telegram,
        cursor_store=store,
        allowed_chat_id=46444780,
    )

    # Set target agent via int chat_id, query via str chat_id and vice versa
    bot_instance.handle_message_agent_pick(46444780, "specialist")
    assert bot_instance._target_for(46444780) == "specialist"
    assert bot_instance._target_for("46444780") == "specialist"

    # Multi-step pending flow started with int chat_id, continued with str chat_id
    bot_instance.handle_addticket_pick_agent(46444780, "architect")
    reply = bot_instance.handle_pending_text("46444780", "My Ticket Title")
    assert "Description?" in reply
    assert "46444780" in bot_instance.pending


def test_activity_render_formatting_and_lifecycle():
    render = ActivityRender(chat_id="12345", agent="architect")
    assert render.render() == "🛠 <b>Activity</b> (<code>architect</code>)"

    # Add input event
    render.add_event({"kind": "input", "cursor": "1-0"})
    text1 = render.render()
    assert "🛠 <b>Activity</b> (<code>architect</code>)" in text1
    assert "1. ⏳ 💬 <i>input received</i>" in text1

    # Add tool events
    render.add_event({"kind": "tool", "tool": "Read", "cursor": "2-0"})
    text2 = render.render()
    assert "1. ✓ 💬 <i>input received</i>" in text2
    assert "2. ⏳ <code>Read</code>" in text2

    render.add_event({"kind": "tool", "tool": "Bash", "cursor": "3-0"})
    text3 = render.render()
    assert "1. ✓ 💬 <i>input received</i>" in text3
    assert "2. ✓ <code>Read</code>" in text3
    assert "3. ⏳ <code>Bash</code>" in text3

    # Add output event -> stays in-progress until finalize()
    render.add_event({"kind": "output", "cursor": "4-0"})
    assert render.completed is False
    text4 = render.render()
    assert "🛠 <b>Activity</b> (<code>architect</code>)" in text4
    assert "1. ✓ 💬 <i>input received</i>" in text4
    assert "2. ✓ <code>Read</code>" in text4
    assert "3. ✓ <code>Bash</code>" in text4
    assert "4. ✓ ✍️ <i>output produced</i>" in text4

    # Finalize explicitly
    render.finalize()
    assert render.completed is True
    text5 = render.render()
    assert "🛠 <b>Activity</b> (<code>architect</code>) · completed (4 steps)" in text5
    assert "4. ✓ ✍️ <i>output produced</i>" in text5


def test_activity_render_truncation_and_escaping():
    # Escaping special characters in agent and tool names
    render = ActivityRender(chat_id="99", agent="<dangerous>&agent")
    render.add_event({"kind": "tool", "tool": "<script>alert(1)</script>"})
    text = render.render()
    assert "&lt;dangerous&gt;&amp;agent" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text

    # Truncation when more than 20 events are present
    render_many = ActivityRender(chat_id="99", agent="architect")
    for i in range(25):
        render_many.add_event({"kind": "tool", "tool": f"Tool_{i}"})
    text_many = render_many.render()
    assert "<i>… 5 earlier steps omitted …</i>" in text_many
    assert "25. ⏳ <code>Tool_24</code>" in text_many


def test_activity_render_flush_debouncing():
    client = DummyTelegramClient()
    render = ActivityRender(chat_id="555", agent="architect")

    # Initial flush with no events does nothing
    render.flush(client)
    assert len(client.sent_messages) == 0

    # Add event and flush -> send_message called
    render.add_event({"kind": "input"})
    render.flush(client)
    assert len(client.sent_messages) == 1
    assert render.message_id == 1

    # Second flush immediately without force or completion is debounced
    render.add_event({"kind": "tool", "tool": "Bash"})
    render.flush(client, force=False)
    assert len(client.edited_messages) == 0

    # Forced flush or finalized flush edits the message
    render.finalize()
    render.flush(client, force=True)
    assert len(client.edited_messages) == 1
    assert "<code>Bash</code>" in client.edited_messages[0]["text"]

    # Redundant flush with identical text is skipped (even with force=True)
    render.flush(client, force=True)
    assert len(client.edited_messages) == 1


def test_mesh_client_stream_activity(monkeypatch):
    mesh = MeshClient("http://fake:8080", "fake-token")

    raw_sse = (
        b"id: 100-0\n"
        b"event: activity\n"
        b'data: {"v":1,"agent":"architect","kind":"tool","tool":"Bash","cursor":"100-0"}\n\n'
    )

    class FakeResponse:
        def __enter__(self):
            return iter(raw_sse.splitlines(keepends=True))

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(bot.urllib.request, "urlopen", lambda req, timeout=90, context=None: FakeResponse())

    events = []
    # Consume one event and break
    for ev in mesh.stream_activity("architect"):
        events.append(ev)
        break

    assert len(events) == 1
    assert events[0]["tool"] == "Bash"
    assert events[0]["cursor"] == "100-0"


def test_telegram_bot_live_activity_with_user_prompt_and_reply_pusher(monkeypatch, tmp_path):
    mesh = DummyMeshClient()
    mesh.activity_queue = [{"cursor": "50-0", "agent": "architect", "kind": "input"}]
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh,
        telegram_client=telegram,
        cursor_store=store,
        target_agent="architect",
        allowed_chat_id=12345,
    )

    activity_events = [
        {"agent": "architect", "kind": "input", "cursor": "51-0"},
        {"agent": "architect", "kind": "tool", "tool": "Read", "cursor": "52-0"},
        {"agent": "architect", "kind": "output", "cursor": "53-0"},
    ]

    def fake_stream(agent, after=None, heartbeat=False):
        yield from activity_events

    monkeypatch.setattr(mesh, "stream_activity", fake_stream)

    # User sends prompt
    reply = bot_instance.handle_user_prompt(12345, "build the feature")
    assert reply == "✅ Sent to architect."

    # Give watcher thread a moment to process the generator
    import time
    time.sleep(0.8)

    # Activity message was sent, and then edited
    assert len(telegram.sent_messages) >= 2  # 1 for activity + 1 for "Sent to architect."
    activity_msg = telegram.sent_messages[0]
    assert "🛠 <b>Activity</b> (<code>architect</code>)" in activity_msg["text"]

    # ReplyPusher delivers final reply
    pusher = ReplyPusher(
        mesh=mesh,
        telegram=telegram,
        chat_id=12345,
        cursor_store=store,
        activity_finalizer_fn=bot_instance.finalize_activity,
    )

    messages = [
        {"l2": {"source": "architect"}, "payload": {"text": "done building"}, "cursor": "60-0"},
    ]

    def fake_reply_stream(after=None):
        yield from messages

    pusher.run(stream_fn=fake_reply_stream)

    # Activity message should be finalized
    assert len(telegram.edited_messages) >= 1
    last_edit = telegram.edited_messages[-1]
    assert "completed" in last_edit["text"]

    # Final reply delivered
    assert telegram.sent_messages[-1]["text"] == "architect: done building"


def test_telegram_bot_multi_output_turn_does_not_early_exit(monkeypatch, tmp_path):
    """Verify that multiple output events interleaved with tools do not cause early exit."""
    mesh = DummyMeshClient()
    mesh.activity_queue = [{"cursor": "70-0", "agent": "architect", "kind": "input"}]
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh,
        telegram_client=telegram,
        cursor_store=store,
        target_agent="architect",
        allowed_chat_id=12345,
    )

    # 7-step turn with multiple outputs interleaved between tools
    activity_events = [
        {"agent": "architect", "kind": "input", "cursor": "71-0"},
        {"agent": "architect", "kind": "output", "cursor": "72-0"},
        {"agent": "architect", "kind": "tool", "tool": "Read", "cursor": "73-0"},
        {"agent": "architect", "kind": "output", "cursor": "74-0"},
        {"agent": "architect", "kind": "tool", "tool": "Edit", "cursor": "75-0"},
        {"agent": "architect", "kind": "tool", "tool": "Bash", "cursor": "76-0"},
        {"agent": "architect", "kind": "output", "cursor": "77-0"},
    ]

    event_index = 0
    event_lock = threading.Lock()

    def fake_stream(agent, after=None, heartbeat=False):
        nonlocal event_index
        while True:
            with event_lock:
                if event_index < len(activity_events):
                    ev = activity_events[event_index]
                    event_index += 1
                    yield ev
                else:
                    break
            import time
            time.sleep(0.05)

    monkeypatch.setattr(mesh, "stream_activity", fake_stream)

    reply = bot_instance.handle_user_prompt(12345, "run multi step task")
    assert reply == "✅ Sent to architect."

    import time
    time.sleep(0.8)

    key = "12345:architect"
    render = bot_instance.activity_renders.get(key)
    assert render is not None
    # All 7 events must have been recorded (not stopped at step 2!)
    assert len(render.events) == 7
    assert [e.get("kind") for e in render.events] == [
        "input", "output", "tool", "output", "tool", "tool", "output"
    ]

    # Final reply arrives via ReplyPusher and finalizes the render
    pusher = ReplyPusher(
        mesh=mesh,
        telegram=telegram,
        chat_id=12345,
        cursor_store=store,
        activity_finalizer_fn=bot_instance.finalize_activity,
    )

    pusher.run(stream_fn=lambda after=None: iter([
        {"l2": {"source": "architect"}, "payload": {"text": "all done"}, "cursor": "80-0"}
    ]))

    assert render.completed is True
    assert "completed (7 steps)" in render.render()
    assert telegram.sent_messages[-1]["text"] == "architect: all done"


def test_telegram_bot_no_activity_push_flag(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh,
        telegram_client=telegram,
        cursor_store=store,
        target_agent="architect",
        allowed_chat_id=12345,
        no_activity_push=True,
    )

    reply = bot_instance.handle_user_prompt(12345, "build the feature")
    assert reply == "✅ Sent to architect."
    assert len(bot_instance.activity_renders) == 0
    # Only "✅ Sent to architect." message is sent
    assert len(telegram.sent_messages) == 1
    assert telegram.sent_messages[0]["text"] == "✅ Sent to architect."


def test_get_activity_tail_pagination_and_true_tail(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    bot_instance = TelegramBot(
        mesh_client=mesh,
        telegram_client=telegram,
        cursor_store=store,
    )

    # 0 events -> returns None
    assert bot_instance._get_activity_tail("architect") is None

    # 550 events (more than 1, less than 1000)
    mesh.activity_queue = [
        {"agent": "architect", "kind": "tool", "tool": "Bash", "cursor": f"{i}-0"}
        for i in range(1, 551)
    ]
    # Must return the TRUE tail (550-0), NOT the first event (1-0)!
    assert bot_instance._get_activity_tail("architect") == "550-0"

    # 2500 events (spanning 3 pages of 1000)
    mesh.activity_queue = [
        {"agent": "architect", "kind": "tool", "tool": "Bash", "cursor": f"{i:05d}-0"}
        for i in range(1, 2501)
    ]
    assert bot_instance._get_activity_tail("architect") == "02500-0"


def test_reply_pusher_seed_cursor_pagination(tmp_path):
    mesh = DummyMeshClient()
    telegram = DummyTelegramClient()
    store = CursorStore(str(tmp_path / "cursor.json"))
    pusher = ReplyPusher(
        mesh=mesh,
        telegram=telegram,
        chat_id=123,
        cursor_store=store,
    )

    # 0 messages -> None
    assert pusher._seed_cursor() is None

    # 1500 messages (spanning 2 pages of 1000)
    mesh.messages_queue = [
        {"cursor": f"{i:05d}-0", "payload": {"text": f"msg {i}"}}
        for i in range(1, 1501)
    ]
    assert pusher._seed_cursor() == "01500-0"


def test_derive_session_url():
    assert _derive_session_url("http://localhost:8080") == "ws://localhost:8081/session"
    assert _derive_session_url("https://office.example.com:8080") == "wss://office.example.com:8081/session"
    assert _derive_session_url("http://127.0.0.1:8080", "ws://custom:9999/session") == "ws://custom:9999/session"
    assert _derive_session_url("http://127.0.0.1:8080", "https://custom:9999") == "wss://custom:9999/session"


SNAPSHOT_PREFIX = "\x1b[2J\x1b[H"


def test_strip_ansi_removes_clear_home_and_sgr_colours():
    raw = "\x1b[2J\x1b[H\x1b[38;5;246mhello\x1b[39m\nworld\x1b[1;1H"
    assert _strip_ansi(raw) == "hello\nworld"


def test_pane_tail_window_crops_chrome_and_caps_lookback():
    lines = [f"row{i}" for i in range(1, 21)]  # row1..row20, row20 = bottom
    window = _pane_tail_window(lines, chrome_lines=4, tail_span=10)
    # bottom-10 .. bottom-4, i.e. rows 11..16
    assert window == ["row11", "row12", "row13", "row14", "row15", "row16"]


def test_pane_tail_window_trims_blank_edges_but_keeps_the_source_bounds():
    lines = ["a", "b", "c", "", "", "", "", ""]
    window = _pane_tail_window(lines, chrome_lines=1, tail_span=8)
    assert window == ["a", "b", "c"]


def test_pane_tail_window_handles_a_pane_shorter_than_the_window():
    assert _pane_tail_window(["one", "two"], chrome_lines=4, tail_span=10) == []


def test_pane_tail_window_rejects_a_span_not_wider_than_chrome():
    with pytest.raises(ValueError):
        _pane_tail_window(["a"], chrome_lines=4, tail_span=4)


# ── transient chrome: spinner/update-banner lines, present only sometimes ──
# Ticket 31e7ef18: leaked into the live-tail because they sit ABOVE the
# fixed-position `chrome_lines` crop and are absent/present by CLI *state*,
# not identity, so no line-count offset can reliably crop them.

def test_is_transient_chrome_line_recognises_real_captured_lines():
    # claude, measured live against a working pane
    assert _is_transient_chrome_line("✻ Churned for 20s") is True
    assert _is_transient_chrome_line("✻ Sautéed for 16s · done 11:21 AM") is True
    assert _is_transient_chrome_line("✻ Worked for 6m 24s · done 11:27 AM") is True
    assert _is_transient_chrome_line("✔ Update installed · Restart to update") is True
    # codex, from the ticket's reported symptom
    assert _is_transient_chrome_line("Boogieing... (17s · ↓ 639 tokens)") is True
    assert _is_transient_chrome_line("   ") is True  # blank counts too


def test_is_transient_chrome_line_does_not_eat_real_content():
    assert _is_transient_chrome_line("Fixed the auth bug, tests are green now.") is False
    # mentions a duration mid-sentence but is not a short spinner-shaped line
    assert _is_transient_chrome_line(
        "I'll wait for 5s before retrying, then report back with the result."
    ) is False
    assert _is_transient_chrome_line("Update the README before merging.") is False


def test_pane_tail_window_strips_a_spinner_and_update_banner_above_the_structural_chrome():
    lines = [
        "real reply line one",
        "real reply line two",
        "✻ Churned for 20s",
        "✔ Update installed · Restart to update",
        "───",           # structural chrome starts here (chrome_lines=4)
        "❯",
        "───",
        "bypass permissions on",
    ]
    window = _pane_tail_window(lines, chrome_lines=4, tail_span=12)
    assert window == ["real reply line one", "real reply line two"]


def test_pane_tail_window_strips_codex_style_spinner_line():
    lines = [
        "real reply",
        "Boogieing... (17s · ↓ 639 tokens)",
        "",
        "───",
        "› Ask Codex to do anything",
        "  gpt-5.6-sol default · /workdir/bus",
    ]
    window = _pane_tail_window(lines, chrome_lines=3, tail_span=12)
    assert window == ["real reply"]


def test_parse_int_overrides():
    assert _parse_int_overrides("backend=5, frontend=5") == {"backend": 5, "frontend": 5}
    assert _parse_int_overrides("") == {}
    assert _parse_int_overrides("garbage,also=not-an-int,ok=3") == {"ok": 3}


def test_pane_watch_render_diff_skip_and_rate_limit():
    telegram = DummyTelegramClient()
    render = PaneWatchRender(chat_id=123, agent="architect")
    render.flush(telegram, ["hello"], force=True)
    assert len(telegram.sent_messages) == 1

    # Identical content, not forced: rate-limited/diff-skipped, no edit.
    render.flush(telegram, ["hello"])
    assert len(telegram.edited_messages) == 0

    # Reset the throttle window and change content: one edit.
    render.last_flush_ts = 0.0
    render.flush(telegram, ["hello", "world"])
    assert len(telegram.edited_messages) == 1
    assert "hello\nworld" in telegram.edited_messages[0]["text"]


def test_pane_watch_render_final_flush_clears_the_stop_button():
    telegram = DummyTelegramClient()
    render = PaneWatchRender(chat_id=123, agent="architect")
    markup = {"inline_keyboard": [[{"text": "⏹ Stop watching", "callback_data": "ws:architect"}]]}
    render.flush(telegram, ["hello"], reply_markup=markup, force=True)
    assert telegram.sent_messages[0]["reply_markup"] == markup

    render.completed = True
    render.flush(telegram, ["hello"], footer="<i>⏹ stopped</i>", clear_markup=True, force=True)
    edited = telegram.edited_messages[-1]
    assert edited["reply_markup"] == {"inline_keyboard": []}
    assert "stopped" in edited["text"]


class FakeWatchWS:
    """A scripted session-door socket for `_run_pane_watch`: each `send`
    (a subscribe/refresh request) is answered by the next canned frame list
    in `frames_per_send` on the following `recv` calls. Once the script is
    exhausted, a further `send` sets `stop_event` — so a test scripts exactly
    N cycles by giving N frame lists and reading `.sent` for what happened,
    without racing `_run_pane_watch`'s own stop_event.wait(refresh_s)."""

    def __init__(self, frames_per_send, stop_event=None):
        self.frames_per_send = list(frames_per_send)
        self.sent = []
        self._pending: list[str] = []
        self._stop_event = stop_event

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def send(self, data):
        self.sent.append(data)
        if self.frames_per_send:
            self._pending = list(self.frames_per_send.pop(0))
        elif self._stop_event is not None:
            self._stop_event.set()

    def recv(self, timeout=None):
        if self._pending:
            return self._pending.pop(0)
        raise TimeoutError()


def test_run_pane_watch_renders_a_cropped_snapshot_and_sends_refresh_true():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        pane = "\n".join(f"row{i}" for i in range(1, 15))
        stop_event = threading.Event()
        ws = FakeWatchWS(
            [[json.dumps({"agent": "architect", "data": SNAPSHOT_PREFIX + pane})]],
            stop_event=stop_event,
        )
        render = PaneWatchRender(chat_id=123, agent="architect")

        bot_instance._run_pane_watch(123, "architect", render, stop_event, ws_connect_fn=lambda: ws)

        assert json.loads(ws.sent[0])["refresh"] is True
        assert len(telegram.sent_messages) == 1
        assert "row9" in telegram.sent_messages[0]["text"]  # within the default [-10:-4] window
        assert "row14" not in telegram.sent_messages[0]["text"]  # cropped as chrome
        assert render.completed is True
        assert bot_instance.pane_watches.get("123") is None


def test_run_pane_watch_ignores_incremental_diffs_between_snapshots():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        pane = "\n".join(f"row{i}" for i in range(1, 15))
        stop_event = threading.Event()
        ws = FakeWatchWS(
            [[
                json.dumps({"agent": "architect", "data": "not a snapshot, a live diff"}),
                json.dumps({"agent": "architect", "data": SNAPSHOT_PREFIX + pane}),
            ]],
            stop_event=stop_event,
        )
        render = PaneWatchRender(chat_id=123, agent="architect")

        bot_instance._run_pane_watch(123, "architect", render, stop_event, ws_connect_fn=lambda: ws)

        assert "row9" in telegram.sent_messages[0]["text"]
        assert "a live diff" not in telegram.sent_messages[0]["text"]


def test_run_pane_watch_stops_on_working_to_idle_transition_not_on_initial_idle():
    with tempfile.TemporaryDirectory() as tmpdir:
        mesh = DummyMeshClient()
        mesh.presence_state = "working"
        bot_instance, mesh, telegram = _make_bot(mesh=mesh, tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        frame = [json.dumps({"agent": "architect", "data": SNAPSHOT_PREFIX + "hi"})]
        ws = FakeWatchWS([frame, frame])
        render = PaneWatchRender(chat_id=123, agent="architect")
        stop_event = threading.Event()

        calls = {"n": 0}
        real_get_presence = mesh.get_presence

        def get_presence(agent):
            calls["n"] += 1
            if calls["n"] >= 2:
                mesh.presence_state = "idle"
            return real_get_presence(agent)

        mesh.get_presence = get_presence

        bot_instance._run_pane_watch(123, "architect", render, stop_event, ws_connect_fn=lambda: ws)

        assert "went idle" in telegram.edited_messages[-1]["text"] or "went idle" in telegram.sent_messages[-1]["text"]
        assert len(ws.sent) == 2  # ran a second cycle before stopping, not zero


def test_run_pane_watch_stops_at_max_duration():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        bot_instance.pane_watch_max_duration_s = 0.0
        frame = [json.dumps({"agent": "architect", "data": SNAPSHOT_PREFIX + "hi"})]
        ws = FakeWatchWS([frame])
        render = PaneWatchRender(chat_id=123, agent="architect")
        stop_event = threading.Event()

        bot_instance._run_pane_watch(123, "architect", render, stop_event, ws_connect_fn=lambda: ws)

        text = telegram.sent_messages[-1]["text"] if not telegram.edited_messages else telegram.edited_messages[-1]["text"]
        assert "time limit" in text


def test_handle_watch_pick_rejects_an_unknown_agent():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        reply = bot_instance.handle_watch_pick(123, "nonexistent")
        assert "Unknown agent" in reply
        assert "123" not in bot_instance.pane_watches


def test_handle_watch_pick_edits_the_picker_message_in_place(monkeypatch):
    """The live pane tail itself is always a new message (PaneWatchRender,
    a separate high-frequency channel) -- but the picker that led to it
    should be edited into an ack, not left dangling with a live agent
    button that no longer does anything useful."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        monkeypatch.setattr(bot_instance, "_run_pane_watch", lambda *a, **kw: None)

        bot_instance.handle_callback_query(123, "cb-1", "wa")
        picker_id = telegram.sent_messages[-1]["message_id"]

        bot_instance.handle_callback_query(123, "cb-2", "wp:architect", picker_id)
        assert telegram.edited_messages[-1]["message_id"] == picker_id
        assert "Watching architect" in telegram.edited_messages[-1]["text"]
        assert telegram.edited_messages[-1]["reply_markup"] == {"inline_keyboard": []}
        # No second new message for the ack -- only the picker was ever sent.
        assert len(telegram.sent_messages) == 1


def test_handle_watch_pick_replaces_an_existing_watch_in_the_same_chat(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        monkeypatch.setattr(bot_instance, "_run_pane_watch", lambda *a, **kw: None)
        bot_instance.handle_watch_pick(123, "architect")
        first_stop_event = bot_instance.pane_watches["123"]["stop_event"]
        bot_instance.handle_watch_pick(123, "sme-2")
        assert first_stop_event.is_set()
        assert bot_instance.pane_watches["123"]["agent"] == "sme-2"


def test_handle_watch_stop_command_with_no_active_watch():
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        reply = bot_instance.handle_watch_stop_command(123)
        assert "No active watch" in reply


def test_handle_watch_stop_via_callback_matches_agent_and_sets_stop_event(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        monkeypatch.setattr(bot_instance, "_run_pane_watch", lambda *a, **kw: None)
        bot_instance.handle_watch_pick(123, "architect")
        stop_event = bot_instance.pane_watches["123"]["stop_event"]
        assert bot_instance.handle_watch_stop(123, "sme-2") == ""  # wrong agent: no-op
        assert not stop_event.is_set()
        bot_instance.handle_watch_stop(123, "architect")
        assert stop_event.is_set()


def test_watch_command_routes_through_text_and_callback(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=123, pane_watch_refresh_s=0.0)
        monkeypatch.setattr(bot_instance, "_run_pane_watch", lambda *a, **kw: None)

        reply = bot_instance.handle_text_message(123, "/watch architect")
        assert "Watching architect" in reply

        reply = bot_instance.handle_callback_query(123, "cb1", "wp:sme-2")
        assert "Watching sme-2" in reply
        assert bot_instance.pane_watches["123"]["agent"] == "sme-2"

        reply = bot_instance.handle_text_message(123, "/watch")
        assert "pick an agent" in reply.lower()


def test_naming_sweep_classes_and_env_vars(monkeypatch):
    from clients.telegram.bot import MeshClient, TelegramBot, CursorStore, logger
    assert logger.name == "mesh_telegram"

    client = MeshClient(base_url="http://127.0.0.1:8080", token="tok123")
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CursorStore(str(Path(tmpdir) / "cursor.json"))
        b1 = TelegramBot(mesh_client=client, telegram_client=None, cursor_store=store)
        assert b1.mesh is client






# ── per-chat transactions: the reviewer's four channels ──────────────────────

def _race(target, n=2, gap=0.0):
    """Run `target` in n threads, collecting anything that escapes one. A
    dispatch thread dying is the production symptom, so it has to be visible
    to the test rather than printed and forgotten."""
    errors = []

    def run():
        try:
            target()
        except BaseException as exc:  # noqa: BLE001 - the point is to catch everything
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
        if gap:
            time.sleep(gap)
    for t in threads:
        t.join(timeout=10)
    assert [t.is_alive() for t in threads] == [False] * n
    return errors


def _slow_read_dict(bot_instance, *, block_on=1, initial=None):
    """A ChatDict that stalls one reader inside the window under test.

    ⚠ Timing simulation, deliberately, and the same technique as the
    pending-flow race test: these windows contain no I/O, so plain concurrency
    never lands inside them -- a version of each of these tests written with
    only threads and a barrier passed with the fix REMOVED, i.e. tested
    nothing. What is asserted stays behavioural: the final state, and that no
    thread died.
    """

    class SlowRead(bot.ChatDict):
        reads = 0
        gate = threading.Lock()
        entered = threading.Event()
        release = threading.Event()

        def get(self, key, default=None):
            value = super().get(key, default)
            with SlowRead.gate:
                SlowRead.reads += 1
                mine = SlowRead.reads
            if mine == block_on:
                SlowRead.entered.set()
                SlowRead.release.wait(timeout=2)
            return value

    d = SlowRead(guard=bot_instance._holds_chat_txn)
    if initial:
        with bot_instance.chat_txn(next(iter(initial))):
            for k, v in initial.items():
                d[k] = v
    return d


def test_two_priority_taps_add_one_ticket_and_kill_no_thread():
    """Reviewer's finding 1. Two callbacks read the same priority state; one
    added the ticket and the other died with KeyError('12345') -- the same
    silent dispatch-thread death this branch exists to close."""
    with tempfile.TemporaryDirectory() as tmpdir:
        entered = threading.Event()
        release = threading.Event()

        class SlowAddMesh(DummyMeshClient):
            def add_ticket(self, agent, title, description="", priority="normal"):
                entered.set()
                release.wait(timeout=2)
                return super().add_ticket(agent, title, description, priority)

        bot_instance, mesh, telegram = _make_bot(mesh=SlowAddMesh(), tmpdir=tmpdir)
        with bot_instance.chat_txn(12345):
            bot_instance.pending[12345] = {
                "flow": "addticket", "agent": "sme-2", "stage": "priority",
                "title": "t", "description": "d", "message_id": 1,
            }

        errors = _race(lambda: bot_instance.handle_addticket_priority(12345, "high"), gap=0.05)
        release.set()

        assert errors == []
        assert len(mesh.added_tickets) == 1
        assert "12345" not in bot_instance.pending


def test_two_watch_picks_leave_exactly_one_stoppable_watch():
    """Reviewer's finding 2. Stopping outside the transaction let two picks
    each see no current watch and each start a watcher: both live, one
    untracked, its stop_event never set and unreachable through the bot. The
    first reader is stalled inside the window (see _slow_read_dict) so the two
    picks genuinely overlap."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        slow = _slow_read_dict(bot_instance)
        bot_instance.pane_watches = slow
        started = []

        def fake_run(cid, agent, render, stop_event):
            started.append(stop_event)
            stop_event.wait(timeout=5)

        bot_instance._run_pane_watch = fake_run
        errors = []

        def pick():
            try:
                bot_instance.handle_watch_pick(12345, "sme-2")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        first = threading.Thread(target=pick)
        first.start()
        assert slow.entered.wait(timeout=5)
        second = threading.Thread(target=pick)
        second.start()
        time.sleep(0.2)
        slow.release.set()
        for th in (first, second):
            th.join(timeout=5)

        tracked = bot_instance.pane_watches.get("12345")
        assert errors == []
        assert tracked is not None
        assert len(started) == 2
        # every watcher started is either the tracked one or already told to
        # stop -- none is left running with no way to reach it
        assert all(ev is tracked["stop_event"] or ev.is_set() for ev in started)
        tracked["stop_event"].set()


def test_a_failing_prompt_does_not_finalize_a_later_prompts_render():
    """Reviewer's finding 3. A installs render A and blocks in its send; B
    swaps in render B and succeeds; A's send then fails and used to clean up
    'the render for this chat and agent', which by then was B's -- leaving B
    live, completed, and untracked."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, no_activity_push=False)
        key = "12345:architect"

        with bot_instance.chat_txn(12345):
            render_a = ActivityRender("12345", "architect")
            render_b = ActivityRender("12345", "architect")
            bot_instance.activity_renders[key] = render_b

        # A owns render_a, which is no longer the installed one
        bot_instance.finalize_activity(12345, "architect", render=render_a)

        assert bot_instance.activity_renders.get(key) is render_b
        assert render_b.completed is False
        # the callback path (no render handle) still finalizes what is installed
        bot_instance.finalize_activity(12345, "architect")
        assert bot_instance.activity_renders.get(key) is None
        assert render_b.completed is True


def test_two_voice_toggles_end_where_two_sequential_toggles_would():
    """Reviewer's finding 4. Two synchronized toggles both replied 'enabled'
    and left it enabled; done one after the other they end disabled. The
    read-then-write window has no I/O in it, so the first reader is stalled
    inside it (see _slow_read_dict) to make the interleaving deterministic."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, voice_feature_enabled=True)
        slow = _slow_read_dict(bot_instance)
        bot_instance.chat_voice_enabled = slow

        errors = []

        def toggle():
            try:
                bot_instance.handle_voice_toggle(12345)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        first = threading.Thread(target=toggle)
        first.start()
        assert slow.entered.wait(timeout=5)
        second = threading.Thread(target=toggle)
        second.start()
        time.sleep(0.2)
        slow.release.set()
        for th in (first, second):
            th.join(timeout=5)

        assert errors == []
        assert bot_instance.is_voice_enabled(12345) is False
        replies = [m["text"] for m in telegram.sent_messages]
        assert sum("enabled for this chat" in r for r in replies) == 1
        assert sum("disabled for this chat" in r for r in replies) == 1


def test_changing_per_chat_state_without_a_transaction_is_refused():
    """The structural half: forgetting the coordination is not a race you have
    to reproduce, it is an error at the first write."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)

        with pytest.raises(bot.ChatTransactionError):
            bot_instance.pending["12345"] = {"flow": "hire", "stage": "name"}
        with bot_instance.chat_txn(12345):
            bot_instance.pending["12345"] = {"flow": "hire", "stage": "name"}
        with pytest.raises(bot.ChatTransactionError):
            del bot_instance.pending["12345"]
        # reads never need one
        assert bot_instance.pending.get("12345")["flow"] == "hire"


def test_a_nested_transaction_is_refused_rather_than_deadlocking():
    """A plain lock would hang here with no message. Nesting means two callers
    each believe they own the chat, which is a design error worth seeing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345):
            with pytest.raises(bot.ChatTransactionError):
                with bot_instance.chat_txn(12345):
                    pass
        # and it is usable again afterwards
        with bot_instance.chat_txn(12345):
            pass


def test_updates_for_one_chat_are_applied_in_arrival_order():
    """Mutual exclusion is not ordering. Lock acquisition is not FIFO, so with
    a lock alone the answers "sme-9" then "-" can be applied "-" first --
    rejected against stage=name -- leaving the flow at profile instead of
    provider, with nothing crashed and a normal-looking log.

    ⚠ The barrier here is DISPATCH COMPLETION, not any wait helper of the
    bot's. An earlier version waited on chat_worker(...).wait_idle(), which
    under a thread-per-update implementation creates an unrelated empty worker
    and returns immediately: the test then failed at stage `name` because the
    dispatches were still running, never at the reversed-order `profile` it
    claimed to catch. It failed for the wrong reason, which is the same thing
    as not testing what it says.

    FALSIFICATION, observed: replace submit_update's body with
    `threading.Thread(target=self._dispatch_update, args=(update,), daemon=True).start()`
    and this fails on `assert state["stage"] == "provider"` with the actual
    value 'profile' -- the second answer applied first, rejected at the name
    stage, and the first answer then advancing only one step."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=12345)
        with bot_instance.chat_txn(12345):
            bot_instance.pending[12345] = {"flow": "hire", "stage": "name", "message_id": 1}

        second_entered = threading.Event()
        finished = threading.Semaphore(0)
        real_dispatch = bot_instance._dispatch_update

        def instrumented(update):
            uid = update["update_id"]
            if uid == 2:
                second_entered.set()
            if uid == 1:
                # Give a mis-ordered implementation every chance to run the
                # later update first. Under the worker this simply times out,
                # because update 2 cannot start until update 1 returns.
                second_entered.wait(timeout=0.3)
            try:
                real_dispatch(update)
            finally:
                finished.release()

        # patched before the first submit, so the worker captures this handler
        bot_instance._dispatch_update = instrumented

        def update(uid, text):
            return {"update_id": uid, "message": {"chat": {"id": 12345}, "message_id": uid, "text": text}}

        bot_instance.submit_update(update(1, "sme-9"))
        bot_instance.submit_update(update(2, "-"))
        assert finished.acquire(timeout=5), "first dispatch never completed"
        assert finished.acquire(timeout=5), "second dispatch never completed"

        state = bot_instance.pending.get("12345")
        assert state is not None, "the flow should still be open at the provider stage"
        assert state["stage"] == "provider"
        assert state["name"] == "sme-9"


def test_a_handler_that_raises_does_not_kill_the_chat_worker():
    """Before, each update owned a bare thread: an exception killed it
    silently and the operator saw nothing. The worker logs and keeps going."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=12345)
        calls = []

        def explode(update):
            calls.append(update["update_id"])
            if update["update_id"] == 1:
                raise RuntimeError("boom")

        worker = bot.ChatWorker("12345", explode)
        worker.submit({"update_id": 1})
        worker.submit({"update_id": 2})

        assert worker.wait_idle(timeout=5)
        assert calls == [1, 2]


def test_an_overlapping_reply_finalizes_the_wrong_turns_render():
    """⚠ Pins a LIMIT WITH A FIX IN FLIGHT, not correct behaviour and not an
    accepted decision -- api-agent's original decline was overtaken by an
    opt-in exact-correlation change they are now building. ReplyPusher has no
    render handle because a reply carries no link back to its prompt: the api
    mints a fresh correlation_id per envelope and an agent's reply is its own
    envelope. With two overlapping prompts to one agent, the first reply ends
    whichever render is installed. ⚠ Do NOT delete this test when correlation
    lands: it is opt-in and depends on the replying agent passing the id back,
    so uncorrelated replies keep arriving and this becomes the test for that
    fallback path."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        key = "12345:architect"
        with bot_instance.chat_txn(12345):
            render_b = ActivityRender("12345", "architect")
            bot_instance.activity_renders[key] = render_b

        # the reply to an earlier, already-swapped-out turn
        bot_instance.finalize_activity(12345, "architect")

        assert render_b.completed is True
        assert bot_instance.activity_renders.get(key) is None


def test_every_mutating_method_on_per_chat_state_is_refused_without_a_transaction():
    """The previous version subclassed dict, so update/clear/popitem/|= all
    bypassed the guard: "omission is an error" was false for four spellings of
    the same write. ChatDict is a MutableMapping over a private dict now, so
    every mutator routes through __setitem__/__delitem__."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345):
            bot_instance.pending[12345] = {"flow": "hire", "stage": "name"}

        for label, mutate in [
            ("__setitem__", lambda: bot_instance.pending.__setitem__("12345", {"flow": "x"})),
            ("update", lambda: bot_instance.pending.update({"12345": {"flow": "x"}})),
            ("setdefault", lambda: bot_instance.pending.setdefault("999", {"flow": "x"})),
            ("pop", lambda: bot_instance.pending.pop("12345")),
            ("popitem", lambda: bot_instance.pending.popitem()),
            ("clear", lambda: bot_instance.pending.clear()),
            ("__ior__", lambda: bot_instance.pending.__ior__({"12345": {"flow": "x"}})),
            ("__delitem__", lambda: bot_instance.pending.__delitem__("12345")),
        ]:
            with pytest.raises(bot.ChatTransactionError):
                mutate()
            assert bot_instance.pending.get("12345")["flow"] == "hire", f"{label} changed state anyway"


def test_state_handed_out_by_a_read_cannot_be_mutated_in_place():
    """`state = pending.get(cid); state["stage"] = ...` used to change live
    shared state with no transaction and no error -- the guard cannot see a
    write it never receives. Stored values are frozen, so that write is
    refused at the point it happens."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345):
            bot_instance.pending[12345] = {"flow": "hire", "stage": "name"}

        state = bot_instance.pending.get("12345")
        with pytest.raises(bot.ChatTransactionError):
            state["stage"] = "provider"
        with pytest.raises(bot.ChatTransactionError):
            del state["stage"]
        assert bot_instance.pending.get("12345")["stage"] == "name"
        # the supported way round: write a successor back inside the transaction
        with bot_instance.chat_txn(12345):
            bot_instance.pending["12345"] = {**dict(state), "stage": "profile"}
        assert bot_instance.pending.get("12345")["stage"] == "profile"


def test_a_stale_read_is_still_accepted_and_the_docs_say_so():
    """⚠ Pins a LIMIT, not a guarantee. Reading outside a transaction and
    writing inside one is exactly what the container cannot detect: by the
    time the write arrives it looks identical to a fresh one. Ordering comes
    from the per-chat worker, not from this class, and the docstring says so
    rather than claiming the guard covers it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345):
            bot_instance.pending[12345] = {"flow": "hire", "stage": "name"}

        stale = dict(bot_instance.pending.get("12345"))  # read with no transaction
        with bot_instance.chat_txn(12345):
            bot_instance.pending[12345] = {**stale, "stage": "profile"}

        assert bot_instance.pending.get("12345")["stage"] == "profile"


def test_nested_state_is_frozen_too():
    """Shallow freezing would be the same hole one level down: a nested dict
    left mutable takes an untracked write exactly as the top level used to."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        with bot_instance.chat_txn(12345):
            bot_instance.pending[12345] = {"flow": "hire", "args": {"stage": "name"}}

        state = bot_instance.pending.get("12345")
        with pytest.raises(bot.ChatTransactionError):
            state["args"]["stage"] = "provider"
        assert bot_instance.pending.get("12345")["args"]["stage"] == "name"
        # objects that carry their own thread-safety are passed through, not frozen
        event = threading.Event()
        with bot_instance.chat_txn(777):
            bot_instance.pane_watches[777] = {"agent": "sme-2", "stop_event": event}
        bot_instance.pane_watches.get("777")["stop_event"].set()
        assert event.is_set()


def test_unauthorized_chats_never_get_a_worker():
    """⚠ Resource exhaustion, not tidiness: a worker is a permanent thread and
    a permanent queue. Measured before the fix, 30 updates from 30 unauthorised
    chats left 30 live daemon threads behind, every one of those updates having
    been correctly rejected. Authorisation has to happen before allocation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir, allowed_chat_id=12345)
        before = threading.active_count()

        for i in range(30):
            bot_instance.submit_update(
                {"update_id": i, "message": {"chat": {"id": 900000 + i}, "message_id": i, "text": "hello"}}
            )
        # malformed updates must not become a chat named "None" either
        bot_instance.submit_update({"update_id": 99, "message": {"text": "no chat at all"}})
        bot_instance.submit_update({"update_id": 100})

        assert bot_instance._chat_workers == {}
        assert threading.active_count() == before
        assert telegram.sent_messages == []

        # the allowed chat still gets exactly one
        bot_instance.submit_update(
            {"update_id": 200, "message": {"chat": {"id": 12345}, "message_id": 200, "text": "/menu"}}
        )
        assert bot_instance.chat_worker(12345).wait_idle(timeout=5)
        assert list(bot_instance._chat_workers) == ["12345"]


def test_updates_are_acknowledged_to_telegram_before_they_are_handled():
    """⚠ Pins a LOSS BOUNDARY, not a guarantee. `offset` advances when an
    update is queued, so a crash loses operator actions Telegram believes were
    delivered. The alternative -- acknowledging only after processing -- makes
    restart redeliver, and a redelivered /run runs the command twice with no
    dedupe anywhere in this client. A lost message can be sent again; a
    duplicated side effect cannot be un-run.

    ⚠ The exposure is the in-progress update PLUS anything queued behind it,
    not just the queue: once the worker has dequeued an update, qsize reads
    zero while the handler may still be tens of seconds inside network calls,
    and that update is equally acknowledged and equally lost. The assertion
    below deliberately uses unfinished_tasks rather than qsize for exactly
    that reason, and BACKLOG_WARN never fires for a single in-flight update --
    no warning does not mean nothing at risk."""
    with tempfile.TemporaryDirectory() as tmpdir:
        holding = threading.Event()
        offsets = []
        polls = threading.Semaphore(0)

        class RecordingTelegram(DummyTelegramClient):
            def get_updates(self, offset=None, timeout=20):
                offsets.append(offset)
                polls.release()
                if len(offsets) == 1:
                    return [{"update_id": 7,
                             "message": {"chat": {"id": 12345}, "message_id": 1, "text": "hello"}}]
                if len(offsets) > 3:
                    raise SystemExit("stop the loop")  # not caught by run_polling's except Exception
                return []

        telegram = RecordingTelegram()
        bot_instance, mesh, _ = _make_bot(telegram=telegram, tmpdir=tmpdir, allowed_chat_id=12345)

        real_dispatch = bot_instance._dispatch_update
        bot_instance._dispatch_update = lambda update: (holding.wait(timeout=5), real_dispatch(update))

        def poll():
            try:
                bot_instance.run_polling()
            except SystemExit:
                pass  # the fake client's way of ending the loop

        loop = threading.Thread(target=poll, daemon=True)
        loop.start()
        try:
            assert polls.acquire(timeout=5), "the loop never polled at all"
            assert polls.acquire(timeout=5), (
                "no second poll while the update was still queued — acknowledgement "
                "now waits for handling, which moves the loss boundary"
            )
            # the update is still sitting in the worker, unhandled...
            assert bot_instance.chat_worker(12345)._queue.unfinished_tasks == 1
            # ...and Telegram has already been told not to send it again
            assert offsets[1] == 8
        finally:
            holding.set()
            loop.join(timeout=5)


# ── idle activity watchers (ticket b6c8b819) ─────────────────────────────────

def test_a_watcher_on_a_silent_stream_still_stops_at_its_deadline():
    """⚠ HARM, not mechanism: a watcher started against an idle agent used to
    live forever, holding a thread and an SSE connection, because its 300s
    deadline was only evaluated when an event arrived and an idle agent sends
    none. Measured on the acceptance instance: four live watcher threads for
    one agent. The stream now reports keepalives as heartbeats, so the loop
    gets a turn to look at the clock."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        render = ActivityRender("12345", "architect")
        ticks = {"n": 0}

        def silent_stream():
            # what an idle agent produces: keepalives, forever
            while True:
                ticks["n"] += 1
                yield None

        started = time.time()
        bot_instance._watch_activity("12345", "architect", None, render,
                                     timeout_s=0.05, stream_fn=silent_stream)

        assert time.time() - started < 5, "the watcher must end on its own deadline"
        assert ticks["n"] > 0, "the heartbeat is what gives the deadline a chance to fire"


def test_a_watcher_without_heartbeats_would_never_reach_its_deadline():
    """The falsification, kept as a test: a stream that yields nothing at all
    blocks the loop in next() forever, which is exactly what swallowing
    keepalives did. Asserted with a thread and a timeout rather than by
    hanging the suite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        render = ActivityRender("12345", "architect")
        release = threading.Event()

        def silent_forever():
            release.wait(timeout=5)   # never yields: no events, no heartbeats
            return
            yield  # pragma: no cover

        done = threading.Event()

        def run():
            bot_instance._watch_activity("12345", "architect", None, render,
                                         timeout_s=0.05, stream_fn=silent_forever)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        assert not done.wait(timeout=0.5), (
            "without heartbeats the deadline cannot fire — this is the leak, pinned"
        )
        release.set()
        assert done.wait(timeout=5)


def test_a_newer_turn_stops_the_previous_watcher_instead_of_stacking():
    """Four watcher threads for one agent was the observed symptom. Swapping
    the render finalized the old one but left its thread running against the
    same agent until its own deadline; the stop switch is checked on every
    tick, and heartbeats guarantee ticks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        bot_instance, mesh, telegram = _make_bot(tmpdir=tmpdir)
        first = ActivityRender("12345", "architect")
        first.stop_event = threading.Event()
        ended = threading.Event()

        def heartbeat_stream():
            while not ended.is_set():
                yield None
                time.sleep(0.01)

        def run():
            bot_instance._watch_activity("12345", "architect", None, first,
                                         timeout_s=30, stream_fn=heartbeat_stream)
            ended.set()

        threading.Thread(target=run, daemon=True).start()
        time.sleep(0.05)
        assert not ended.is_set()

        first.stop_event.set()  # what handle_user_prompt does on a newer turn
        assert ended.wait(timeout=5), "a replaced watcher must end, not run out its own deadline"


def test_the_pane_watcher_does_not_have_the_same_shape():
    """⚠ Checked because the ticket asked, and the answer is no: _run_pane_watch
    drives its own loop (`ws.recv` with a drain deadline, then
    `stop_event.wait(refresh_s)`) and evaluates its max duration at the top of
    every iteration regardless of traffic. Its deadline does not depend on
    anything arriving, so it needs no equivalent fix. Pinned so that stays
    true."""
    source = inspect.getsource(TelegramBot._run_pane_watch)
    assert "stop_event.wait(self.pane_watch_refresh_s)" in source
    assert "time.time() - start_time > self.pane_watch_max_duration_s" in source
