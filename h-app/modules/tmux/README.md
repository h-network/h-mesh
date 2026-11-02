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

`start_agent_command(cli, resume=False, model=None)` adds optional vendor
model passthrough while preserving the existing argv when no model is chosen.
Claude and agy use `--model <id>`; Codex uses `-c` with a quoted model string.
The reconciler resolves `AGENT_MODELS` before `DEFAULT_MODEL` from its merged
tenant environment for both the initial session window and later windows.
An agent with a local provider receives no vendor-model argument: the provider
model stays authoritative. The settings affect launches, not a CLI already
running; restart the reconciler to reload changed tenant configuration.

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

The "ticket done" rule is a one-line notice, not a report: when a ticket is
done the agent sends the lead ONE line saying so, naming the ticket (title or
id), then runs `h-mesh-office done`. No summary or recap of changes; the commit
and the ticket already carry the detail. (It replaced "message the lead a
summary before or alongside `done`".) The suite-counts rule ("report both the
collected count and the passed count") is unchanged.

The "That's colleagues only -- {entrance} ... `peers -i` lists those too"
paragraph was removed as well (no more entrance gatekeeping text, no
replacement): a message already arrives as `[message from <name>]` and the guide
says to reply to that name, which covers every entrance. As a result
`operator_entrance` / `OPERATOR_ENTRANCE`, `enrolled_entrances` and
`raise_on_invalid` **no longer influence the guide text at all**. They are
deliberately left in place, and still do their validation side effects only: an
operator entrance that is not enrolled raises `ValueError` in
`generate_agents_md` (which `write_agent_guide` logs as an `error` event and then
regenerates), and an unverifiable one logs a `warning`.

⚠ A guide is only (re)written when a window is created. Agents that are already
running keep their old `AGENTS.md`/`CLAUDE.md` text until their window is next
created (hire, or the reconciler recreating a missing window).

## Session history

`has_session_history` detects an agent's prior on-disk CLI session (claude:
`~/.claude[-ACCOUNT]/projects/<cwd>/*.jsonl`; codex: `rollout-*.jsonl` whose
`session_meta` cwd matches; agy: lines of the shared `history.jsonl` whose
`workspace` matches). `purge_session_history` is its deleting twin, used by
`office purge`; see `modules/office/README.md` for the safety contract (it does
not know about liveness, so the caller must refuse an enrolled agent). It
validates the account name, requires an absolute normalised cwd, never follows
symlinks, and rewrites agy's shared file atomically instead of deleting it.
