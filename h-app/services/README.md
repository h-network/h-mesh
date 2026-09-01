# services

Entrypoints for anything that runs continuously and never exits on its own --
a daemon, a monitoring loop, a reconciler. If your module needs a script that
keeps running (not something invoked once per request, and not a one-off
tool), its entrypoint goes here.

Each module still owns the actual logic. A file here is a thin launcher --
import that module's own daemon/service class and call its run loop, nothing
more. This is a shared, discoverable place to start things individually or as
a bundle; it does not centralize or couple the logic itself.

One exception: core's own daemon (the switch) stays in `core/`, since core
*is* the switch's module -- there's no separate library/daemon split to make
there the way another module might have its own mechanism library and a
separate reconciler daemon as two different things.

Nothing per-request (e.g. `agentlifecycle`) and nothing CLI-invoked (e.g.
`office`) belongs here.

A launcher does one thing besides starting its module: it calls
`core.logging.configure_logging()` first, which sets the stdlib logging
threshold from `H_MESH_LOG_LEVEL` (see `core/README.md`, "Two logging systems,
one file"). A new launcher should do the same — the process that starts is the
one entitled to pick the level, and without it stdlib logging drops everything
below WARNING with no way to turn it up. `telegram_bot.py` is the one that
does not, because `clients.telegram.bot` already configures itself at import
from the same variable.

| file | service |
|---|---|
| `api.py` | Loads API settings and runs the module's FastAPI application with uvicorn. |
| `tmux_reconciler.py` | Loads tmux configuration and runs `TmuxReconciler.run_forever()`. |
| `telegram_bot.py` | `clients.telegram.bot.TelegramBot` polling client |
| `web_console.py` | `clients.web.server.WebConsoleServer` HTTP/WS proxy server |
