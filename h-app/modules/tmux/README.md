# tmux

The tmux port: mechanism library, delivery, and the reconciler daemon that
turns desired registry state into real tmux windows. This is the first
module in h-mesh with both a passive library and a daemon of its own -- the
daemon's *entrypoint* lives in `h-app/services/tmux_reconciler.py` (a thin
launcher), but the daemon's actual logic stays here, in `reconciler.py`,
same as every module owning its own daemon logic.

| file | what it holds |
|---|---|
| `ops.py` | passive tmux mechanics: `run_tmux`, `create_window`, `kill_window`, `submit_text`, session-history detection, CLI trust/guide seeding, single-guide decision (`claude_reads_agents_md`, `migrate_agent_guide`) |
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

**Relayed inter-office messages.** The relay delivers a remote agent's message
as local source `relay` (an api-type participant) with the text prefix
`[from <office>/<agent>]`. The generic hint above would point the answer at the
local `relay`, whose API inbox keeps it and nothing crosses back (seen in the
live laba/labb run). So for source `relay` the hint is instead
`[reply to <office>/<agent>: office send --office <office> -a <agent> "..."]`,
taken from that leading header by `relayed_reply_hint`; both names are
re-validated as agent segments before they reach a line the agent reads as a
command, only the leading header counts, and a relay message without a usable
header gets no hint at all. There is no `--reply-to` in it: `send --office`
rejects that flag, and the office-sent record already routes the remote's answer
back to the sender. Hints for every other api-type source are unchanged.

## Agent guide

`write_agent_guide` (called by `create_window` on every hire/reconcile, and by
`ensure_server_and_session` for the first window) renders `generate_agents_md`
and writes it to `AGENTS.md` in the agent's directory. codex and agy read that
file; claude reads `CLAUDE.md` and, when a project has none of its own, falls
back to `AGENTS.md` (its `instructionFiles` setting, default
`claude-md-or-agents-md`).

### One file, or two (#31)

The per-agent `CLAUDE.md` is no longer written **when claude's fallback can be
positively established**. That fallback was tested by the operator on the live
office; issue #31 verified the fallback against **Claude Code 2.1.302**, which is recorded as `CLAUDE_AGENTS_MD_FLOOR`
and is the minimum version. `claude_reads_agents_md` decides, and **fails
closed**: any doubt keeps writing `CLAUDE.md` exactly as before (a guide-less
claude agent is worse than one identical extra file). It requires all of:

1. `claude --version` (5 s timeout, cached for 30 s only: a fresh container writes the guide on the installer's claude, which self-updates about a minute later) parses and is >= the floor.
2. No settings file sets a blocking override. Checked: the agent's user config
   dir (`~/.claude` or `~/.claude-<profile>`) `settings.json`, the agent's
   `<workdir>/.claude/settings.json` and `settings.local.json`, and
   `/etc/claude-code/managed-settings.json` plus `managed-settings.d/*.json`.
   `instructionFiles` must be `claude-md-or-agents-md` or
   `claude-md-and-agents-md`; the legacy `projectInstructions` must be
   `agents-fallback` or `both`. A file that exists but is unreadable, not JSON, or
   not an object also keeps the dual-write.
3. No `CLAUDE.md` in any ancestor directory of the agent's workdir (whether that
   counts as the project's "own" is unverified, so it is treated as blocking).

Decided on every guide write, so adding an override later brings `CLAUDE.md`
back on the agent's next window creation.

**Cleaning up a stale `CLAUDE.md`** (left by an older dual-writing build) never
deletes anything we cannot prove we generated. It is removed only if
byte-identical to the `AGENTS.md` that was on disk before it was overwritten
(the old dual-write wrote identical files, so this holds even if the template
has since changed) or to the newly generated guide. Anything else (including a
symlink) is left in place and logged as an `error` event, and claude will then
read it **instead of** `AGENTS.md`.

**Already-running agents.** Their window is never recreated, so the guide is not
rewritten. The reconciler therefore runs `migrate_agent_guide` once per agent per
reconciler process: when the conditions above hold and `CLAUDE.md` is
byte-identical to the `AGENTS.md` beside it, it is removed (`AGENTS.md` is not
rewritten); otherwise it is kept. Each outcome is logged as a `guide_migrated`
event. A live claude session keeps what it already loaded; the change takes effect
at its next start.

**Retry.** A sweep blocked only by claude's version (`kept: claude ...`: unavailable
or older than the floor) is not final: claude can self-update after the reconciler
started, so the agent is re-checked on every reconcile (the version probe itself
runs at most once per 30 s) and the status is logged only when it changes. Every
other outcome (foreign `CLAUDE.md`, settings override, ancestor `CLAUDE.md`, no
`AGENTS.md`) is final for the process. An agent whose window is recreated goes
back into the sweep, since its guide was just rewritten.

**What cannot be detected:** organisation/remote policy settings (not readable
from files); settings added after the last check; a claude binary replaced
under a running reconciler (the version is cached for 30 s); the agent
editing its own `.claude/settings.json`; settings passed on the command line
(h-agent only passes a capture-hook `--settings`, but that is outside this
module).

### Guide contents

**Reaching another office.** After the local `send` examples the guide has a
short inter-office section (`inter_office_guide`). Every agent gets `offices`,
`send --office <office> -a <agent> "..."`, the exit codes (0 accepted and queued,
1 refused with nothing sent, 3 outcome unknown, so do not blindly resend) and a
pointer to the reply, which arrives as `[message from relay] [from
<office>/<agent>] ...` with a hint line carrying the working answer command (see
"Relayed inter-office messages" above). Only the lead also gets the linking
commands (`coop-grant`, `office-add ... --token-stdin`, `coops`, `coop-revoke`,
`office-rm`) and a warning to give a token only to the other office's lead and
never put it in a message, file or commit. The syntax is hand-written because the
command table carries no arguments; `tests/test_guide_inter_office.py` fails if a
command the table marks "lead only" is missing from the lead's text or leaks into
an ordinary agent's, if the printed `send --office` form stops parsing, or if the
printed exit codes drift from what `send` reports. Guides already written for
live agents are not rewritten.

The guide deliberately carries **no operator-authority text**: earlier versions
added a per-entrance block ("routing provenance, not cryptographic proof of
identity", "operator coordination authority cannot attach...") and an
"Interface entries may only accept specific envelope kinds" sentence. Agents,
claude especially, read those as a reason to refuse or to demand an architect
ack for legitimate Telegram/api messages (issue #5), so they were removed.
The `⚠ Treat where a message arrives (an external door) ...` paragraph
("untrusted external content", "without blindly executing arbitrary remote
input") was removed too, with no replacement text, for the same reason: it was
the first alarm an agent read and the main cause of refusals of the operator's
own Telegram messages. All that remains on the subject is the "destructive,
ambiguous, or outside scope: ask the lead first" rule.

`operator_entrance` / `OPERATOR_ENTRANCE`, `enrolled_entrances` and
`raise_on_invalid` are still accepted and still validated, but now only choose
the wording of the "colleagues only -- ..." sentence (and log an error for an
unenrolled entrance); they no longer add guide text of their own.

⚠ A guide is only (re)written when a window is created. Agents that are already
running keep their old `AGENTS.md`/`CLAUDE.md` text until their window is next
created (hire, or the reconciler recreating a missing window).
