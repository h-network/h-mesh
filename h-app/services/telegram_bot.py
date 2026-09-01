"""Thin launcher for the Telegram bot client daemon. The actual logic lives in
clients.telegram.bot -- this file only wires environment into it and runs the
client.

⚠ Must mirror clients.telegram.bot's own main() daemon-mode sequence
exactly (enrol, then ReplyPusher/AlertPusher threads, then run_polling) --
not just call something on `bot` and hope. This file used to call the
nonexistent `bot.run()`, which crashed instantly with AttributeError on
every real start (caught live by a user, never by anything here -- see
h-app/tests/test_telegram_bot_launcher.py, added because nothing executed
this module's main() at all before that).
"""

import os
import sys
import threading

from clients.telegram.bot import (
    AlertPusher,
    CursorStore,
    DryRunTelegramClient,
    MeshClient,
    ReplyPusher,
    TelegramBot,
    TelegramClient,
    _derive_session_url,
    _sibling_path,
    DEFAULT_CURSOR_FILE,
)
from services.daemon_identity import require_daemon_identity


def main() -> None:
    require_daemon_identity()
    api_url = os.environ.get("H_MESH_API_URL", "http://127.0.0.1:8080")
    api_token = os.environ.get("H_MESH_API_TOKEN", os.environ.get("API_TOKEN", ""))
    session_url = os.environ.get("H_MESH_SESSION_URL", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    target_agent = os.environ.get("TELEGRAM_TARGET_AGENT", "architect")
    cursor_file = os.environ.get("TELEGRAM_CURSOR_FILE", DEFAULT_CURSOR_FILE)
    mini_app_url = os.environ.get("TELEGRAM_MINI_APP_URL", "")
    tts_voice = os.environ.get("TTS_VOICE", "") or None
    no_alert_push = os.environ.get("NO_ALERT_PUSH") == "1"
    alerts_cursor_file = os.environ.get("ALERTS_CURSOR_FILE", "") or _sibling_path(cursor_file, "alerts")

    if not api_token:
        print("Error: API token required (API_TOKEN or H_MESH_API_TOKEN env var)", file=sys.stderr)
        sys.exit(1)

    mesh_client = MeshClient(
        base_url=api_url,
        token=api_token,
        app_name="telegram",
    )

    telegram = TelegramClient(bot_token) if bot_token else DryRunTelegramClient()
    cursor_store = CursorStore(filepath=cursor_file)

    bot = TelegramBot(
        mesh_client=mesh_client,
        telegram_client=telegram,
        cursor_store=cursor_store,
        target_agent=target_agent,
        allowed_chat_id=chat_id,
        session_url=session_url or _derive_session_url(api_url),
        mini_app_url=mini_app_url,
        # ⚠ Not passed explicitly: TelegramBot's own __init__ already falls
        # back to reading TELEGRAM_VOICE/TTS_VOICE/NO_ACTIVITY_PUSH from the
        # environment when these are left at their default (None/False +
        # unset), so wiring them here again would just duplicate that
        # fallback, not add anything a fresh env var didn't already cover.
    )

    if not bot_token:
        print("Running telegram client in dry-run mode (no TELEGRAM_BOT_TOKEN set).", file=sys.stderr)

    # ⚠ Called once, unconditionally, before anything below runs -- not
    # per-branch. The API daemon and this one can start within the same
    # instant with no readiness wait, so enrolment can lose that race;
    # TelegramBot.enrol() retries with backoff to cover it (see its own
    # docstring). It never raises on failure, only logs and returns False,
    # so this is safe to call even if the API isn't reachable yet.
    bot.enrol()

    if chat_id:
        # cursor_store is entirely ReplyPusher's now -- see ReplyPusher's
        # own docstring for why replies are pushed independently rather
        # than handle_user_prompt blocking inline.
        reply_pusher = ReplyPusher(
            mesh=mesh_client,
            telegram=telegram,
            chat_id=chat_id,
            cursor_store=cursor_store,
            tts_voice=tts_voice,
            voice_enabled_fn=bot.is_voice_enabled,
            activity_finalizer_fn=bot.finalize_activity,
        )
        threading.Thread(target=reply_pusher.run, daemon=True, name="reply-pusher").start()
        if not no_alert_push:
            alert_pusher = AlertPusher(
                mesh=mesh_client, telegram=telegram, chat_id=chat_id,
                cursor_store=CursorStore(filepath=alerts_cursor_file),
            )
            threading.Thread(target=alert_pusher.run, daemon=True, name="alert-pusher").start()
    else:
        print("TELEGRAM_CHAT_ID not set; live reply/alert push disabled (the menu still works on demand).",
              file=sys.stderr)

    bot.run_polling()


if __name__ == "__main__":
    main()
