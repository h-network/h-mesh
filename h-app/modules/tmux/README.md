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

⚠ `require_isolated_tmux()` in `ops.py` refuses to touch the ambient tmux
server unless `TMUX_SOCKET`/`TMUX_TMPDIR` is explicitly set -- this exists
because driving the wrong server has destroyed a live office before. Never
run anything in this module against an unset/ambient tmux server, including
for manual testing.

This module has not been run against a real tmux server yet -- only
imported and syntax-checked. Real verification (an isolated `TMUX_TMPDIR`,
an actual reconcile pass, an actual delivery) is still open work.
