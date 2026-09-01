# tmux

The tmux port: mechanism library, delivery, and the reconciler daemon that
turns desired registry state into real tmux windows. This is the first
module in h-mesh with both a passive library and a daemon of its own -- the
daemon's *entrypoint* lives in `h-app/services/tmux_reconciler.py` (a thin
launcher), but the daemon's actual logic stays here, in `reconciler.py`,
same as every module owning its own daemon logic.

| file | what it holds |
|---|---|
| `ops.py` | passive tmux mechanics: `run_tmux`, `create_window`, `kill_window`, `submit_text`, session-history detection, CLI trust/guide seeding |
| `port.py` | `deliver_tmux` entrypoint and terminal delivery handlers (`message_opener`, `command_opener`, `attachment_opener`), delivery-verification marking |
| `reconciler.py` | `TmuxReconciler` -- the daemon logic: compares desired tmux membership against real windows, creates/removes them |

⚠ `require_isolated_tmux()` and `run_tmux()` in `ops.py` enforce strict isolation:
- `resolve_tmux_socket()` resolves the target socket in priority order (explicit argument, `TMUX_SOCKET`, or `$TMUX_TMPDIR/default`).
- `run_tmux()` always passes `-S <socket>` explicitly so that ambient `$TMUX` in the environment cannot override `TMUX_TMPDIR`.
- `require_isolated_tmux()` refuses execution if no isolated socket/tmpdir is set, or if the resolved target matches the ambient `$TMUX` socket.
Never run anything in this module against an unset or ambient tmux server.
