"""Thin launcher for the Telegram bot client daemon. The actual logic lives in
clients.telegram.bot -- this file only wires environment into it and runs the
client.
"""

import os
import sys

from clients.telegram.bot import (
    CursorStore,
    DryRunTelegramClient,
    MeshClient,
    TelegramBot,
    TelegramClient,
    _derive_session_url,
    DEFAULT_CURSOR_FILE,
)


def main() -> None:
    api_url = os.environ.get("H_MESH_API_URL", "http://127.0.0.1:8080")
    api_token = os.environ.get("H_MESH_API_TOKEN", os.environ.get("API_TOKEN", ""))
    session_url = os.environ.get("H_MESH_SESSION_URL", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    target_agent = os.environ.get("TELEGRAM_TARGET_AGENT", "architect")
    cursor_file = os.environ.get("TELEGRAM_CURSOR_FILE", DEFAULT_CURSOR_FILE)
    mini_app_url = os.environ.get("TELEGRAM_MINI_APP_URL", "")

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
    )

    if not bot_token:
        print("Running telegram client in dry-run mode (no TELEGRAM_BOT_TOKEN set).", file=sys.stderr)

    bot.run()


if __name__ == "__main__":
    main()
