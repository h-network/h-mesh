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
- `require_isolated_tmux()` refuses execution if no isolated socket/tmpdir is set, or if an explicit socket matches the ambient `$TMUX` socket.
- `run_tmux()` and `ControlModeClient.start()` pass explicit `-S <socket>` if specified, and strip ambient `TMUX`/`TMUX_PANE` from the subprocess environment so tmux natively computes its socket path under `TMUX_TMPDIR` without preferring outer sessions.
Never run anything in this module against an unset or ambient tmux server.

`message_opener` pastes `[message from {source}] {text}`, unchanged from
before opt-in reply correlation existed, for every message. When `source`
is an api-type participant, a second line follows:
`[reply to {source}: office send -a {source} --reply-to {stream_id} "..."]`
-- the id lives only there, not on the primary line, because
`in_reply_to` is only ever validated when the reply's destination is
api-type (see `modules/api/README.md`'s "Reply correlation" section), which
is exactly the condition that already gated this hint line before the id
existed. `record_delivered()` (`lib/reply_correlation.py`) runs on every
delivery regardless of source type, so a later `--reply-to` can be
validated against real provenance.
