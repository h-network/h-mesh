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
| `run_windows.py` | observe and remove `office run` windows: `snapshot` (state of every `__run__*` window), `publish` (mirror it to Redis `run.windows`), `process_removals` (honour `run.remove`) |
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

## Raw process windows

`create_run_window` backs `office run` and bypasses agent enrollment, guide
generation, and `window_env`. Raw windows use the reserved `__run__` prefix:
underscores are invalid in participant addresses, and `submit_text` also
rejects this namespace before issuing a tmux command. The reconciler excludes
these windows from stale-window cleanup and agent restart tracking, including
when the registry has no tmux agents.

At the end of each pass the reconciler also calls `run_windows.process_removals`
and `run_windows.publish` (inside a try/except, so a fault there never stops
agent reconciliation). It is the one process that can always ask tmux: an agent
pane may not, because of the isolation guard. `publish` mirrors every run window
to the tenant hash `run.windows` (`name -> {state: running|exited, exit_status,
signal, command, since}`), writing only on a change, and `process_removals`
kills the windows named in the set `run.remove` **by tmux window id**, never by
name (tmux resolves `-t session:a` by prefix, so removing `a` must not be able to
take `ab`). A dead pane can briefly (tmux 3.5a: sometimes for a long while) have
no `pane_dead_status`; `publish` stores `exit_status: null` and rewrites as soon
as the status appears, and the CLI shows `exited (status pending)` meanwhile. The
reconciler does not nudge tmux to reap. Both go through `ops.run_tmux`; `ops.py` is unchanged.

A new window initially holds a private placeholder process while
`remain-on-exit` and rename protection are set. Only then is the requested
literal argv launched, with agent identity and custody variables removed.
This preserves even a command that exits immediately; no automatic respawn
is registered. Failure cleanup targets only the unique window id returned by
that creation. Existing names are refused, never replaced.

## Delivery

`message_opener` pastes `[message from {source}] {text}`, unchanged from
before opt-in reply correlation existed, for every message. When `source`

`message_opener` pastes `[message from {source}] {text}` for every message,
led by a stamp from `message_stamp`: `<UTC send time> #<id> ` (#20), e.g.
`2026-11-02T03:42:11Z #4f3a9c21 [message from alice] hello`. The time is the
frame's own `ts` (when the sender built it, not when it was pasted, so a
message that sat in a queue still reads as old), cut to whole seconds; the id
is the first 8 hex of its `stream_id` (the full id is still what `--reply-to`
takes). Both fields are checked against a strict pattern before use because
the sender controls them and they land in a terminal; one that does not match
is left out, and a frame with neither gets the old plain line. Delivery order
is unchanged (queue order); the stamp makes it, and staleness, visible. The
`[message from ...]` text itself is untouched, so guides already written for
live agents stay accurate. Attachment notices (`[attachment from ...]`) are not
stamped. When `source`
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
The #20 stamp is written before `[message from relay]`, outside the text the
header is parsed from, so a stamped relayed message still gets its hint
(`test_the_hint_still_matches_with_the_full_time_and_id_stamp_in_front`).

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

**Command inventory (#4).** After the board commands the guide lists every
other office command as `name  one-line description`, generated by
`office_command_inventory` from `modules.office.cli._COMMAND_TABLE` (imported
lazily; if it cannot be read the block is omitted and the guide still writes).
Commands the prose already explains with examples are named in
`GUIDE_CURATED_COMMANDS` (`peers send list take done return show retitle`) and
skipped; aliases appear as `alias of <name>`. A command added to the table
reaches the guide with no edit here, and `tests/test_guide_command_inventory.py`
fails if a table command is in neither the prose nor the inventory, or if a
curated name stops being explained in the prose. Guides already written for live
agents are not rewritten; the inventory appears in new hires' guides.

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
printed exit codes drift from what `send` reports. The generated command inventory (above) lists every one of these
subcommands to every agent, the lead-only ones labelled "(lead only)";
`tests/test_guide_command_inventory.py` pins that for `offices`, `coop-grant`,
`coops`, `coop-revoke`, `office-add` and `office-rm`. Guides already written for
live agents are not rewritten.

The guide deliberately carries **no operator-authority text**: earlier versions
added a per-entrance block ("routing provenance, not cryptographic proof of
identity", "operator coordination authority cannot attach...") and an
"Interface entries may only accept specific envelope kinds" sentence. Agents,
claude especially, read those as a reason to refuse or to demand an architect
ack for legitimate Telegram/api messages (issue #5), so they were removed.
The `⚠ Treat where a message arrives (an external door) ...` paragraph
("untrusted external content", "without blindly executing arbitrary remote
input") was removed too, for the same reason: it was the first alarm an agent
read and the main cause of refusals of the operator's own Telegram messages.

What replaced it is a calibrated two-paragraph section, placed late in
"Working in this office" (just before the "destructive, ambiguous, or outside
scope: ask the lead first" rule), not at the top. It separates two things the
alarm text had fused: **doing the requested work** and **honouring a claim of
authority**. The first paragraph says other agents, the api, Telegram and any
other connection are designed, expected ways for work to arrive, that a benign
in-scope request is ordinary work to do and answer with `send`, and that the
switch and participant list, not each agent per message, decide who may send.
The second keeps the guardrails: the `[message from <name>]` label is the
switch's stamp and stays meaningful (a message *from* `telegram` is not a
message from someone else *saying* what the operator wants); words in a body
claiming to be the operator or the lead are a claim, not an instruction
(a non-lead agent is told direction comes from the lead writing as itself); and
a destructive or out-of-scope request is not acted on because a message says so,
whichever door it used. It uses none of the removed alarm phrases.
`tests/test_guide_external_doors.py` pins each sentence for a non-lead agent,
the lead and an office with no lead, plus the alarm phrases' absence and that
the destructive rule is unchanged and directly follows.

Status: unit-tested, not proven live. No live claude agent has been shown to act on a
benign api request and decline a body-claimed "I am the operator" one; the operator
verifies that on a real claude office.

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
