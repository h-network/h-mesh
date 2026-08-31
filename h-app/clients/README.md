# clients

External-facing clients that connect to an h-mesh tenant over HTTP/REST and WebSocket.
**Neither is part of the core framework** — they are external participants that communicate over standard protocol doors.

| client | what it provides |
|---|---|
| `telegram/` | Unattended Telegram bot client: talk to agents, receive replies/alerts/tool runs, attach files/photos, manage tickets |
| `web/` | Standalone zero-build browser console SPA and Telegram Mini App dashboard with authenticated HTTP/SSE/WebSocket proxy |

## Running the Clients

### 1. Telegram Client
```bash
python3 h-app/services/telegram_bot.py
# Or with explicit flags:
python3 h-app/clients/telegram/bot.py --api-url http://127.0.0.1:8080 --api-token "$API_TOKEN" --chat-id "$TELEGRAM_CHAT_ID"
```

### 2. Web Console
```bash
python3 h-app/services/web_console.py
# Or with explicit flags:
python3 h-app/clients/web/server.py --api http://127.0.0.1:8080 --token "$API_TOKEN" --secret "$OPERATOR_SECRET"
```

Both clients can be tested in dry-run/demo mode without live credentials:
- Telegram: omit `TELEGRAM_BOT_TOKEN` to use the dry-run console driver.
- Web Console: run with `--demo` to explore the UI using offline simulated office fixtures.
