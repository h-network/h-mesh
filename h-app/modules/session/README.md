# session

The live tmux control-mode viewer and WebSocket service for viewing and driving
tenant terminals.

This module runs a long-running WebSocket daemon (uvicorn) that attaches a single
`tmux -C` control-mode client to the tenant's tmux session and multiplexes
terminal updates out to multiple connected viewers. Viewers can subscribe to
one or more agents in read-only or read-write mode and send keystrokes back into
specific agent panes.

The daemon's *entrypoint* lives in `h-app/services/session.py` (a thin launcher),
while the actual application and control logic stays here in `app.py` and
`control.py`.

| file | what it holds |
|---|---|
| `control.py` | `ControlModeClient` -- single `tmux -C` connection shared across viewers, `%output` unescaping, pane discovery, screen snapshots, keystroke forwarding (`send-keys -H`) |
| `app.py` | FastAPI application, WebSocket `/session` endpoint, authentication (`Bearer` header or `?token=` query param), subscription management, structured connection logging |

## %output unescaping

⚠ `%output` in tmux control mode does not carry raw bytes. tmux escapes every
non-printable character as a backslash and three OCTAL digits (e.g. ESC as `\033`
and literal backslash as `\\`). Publishing that unchanged means a terminal renders
`\033[?25l` as text instead of hiding the cursor (which was an actual production
incident where operators saw screenfuls of raw escape sequences). `_unescape_control()`
in `control.py` converts these back to raw bytes before publishing to subscribers.

## Isolation & safety

⚠ `require_isolated_tmux()` refuses to connect to an ambient tmux server unless
`TMUX_SOCKET` or `TMUX_TMPDIR` is set.

A non-loopback `SESSION_BIND` requires TLS certificates (`SESSION_TLS_CERT` /
`SESSION_TLS_KEY` or `API_TLS_CERT` / `API_TLS_KEY`) unless `H_MESH_ALLOW_PLAINTEXT=1`
is explicitly set in the environment.
