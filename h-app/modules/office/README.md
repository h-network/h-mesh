# office

The `office` CLI and its receiving port, together. `office` builds and sends
envelopes (hire/retire/pause/resume, send, board, peers, etc); this module's
own port receives the hire/retire/pause/resume envelopes back and calls the
settled `lib.agentlifecycle` API. Same domain, both sides, one module.

| file | what it holds |
|---|---|
| `cli.py` | the sending half -- argparse dispatch and every command that builds an envelope or reads/mutates Redis state the caller owns directly |
| `dispatcher.py` | the unified `h-mesh COMMAND` routing layer; delegates to this CLI and the existing service entry points without replacing them |
| `pricing.py` | model token pricing and cost calculation, used only by `usage` |
| `port.py` | the receiving half -- one-envelope lifecycle dispatch plus its runnable kick entrypoint |

Not runnable via any packaging yet -- h-mesh has no console-scripts story for
any module, so `office` is reached the same way tests reach it: put `h-app/`
on `sys.path` and call `modules.office.cli.main`.

`profiles` is not a command yet: it needs an `available_profiles()`
(configured-accounts) read that has no h-mesh equivalent anywhere in `core`
or `lib`. `hire --profile` still exists and still reaches the payload, but
without the client-side "unknown account" check the old system had, for the
same reason. `hire --provider NAME` likewise passes an explicit provider to
the lifecycle payload, where the existing StartAgent validation applies.
`hire` starts a fresh CLI session by default, including when an agent name has
local session history; use `hire --resume` to restore that history explicitly.
The first tmux hire becomes lead when the registry is empty; later hires
preserve the incumbent. Registry membership and the lead selection are
published atomically before tmuxhost creates panes and their guides.

`office run NAME [--cwd PATH] [-- COMMAND ...]` opens a shell or raw command
in the existing office tmux session, separately from `hire`. Omitting the
command starts `bash -il`; use `--` before an explicit command and its literal
arguments (`office run monitor -- top`). The working directory defaults to
the caller's current directory. This local operation uses `TMUX_SESSION`
(or `TENANT`) and the explicitly isolated tmux socket/tmpdir; it sends no
envelope and enrolls no participant.

Run it from the operator's shell outside the office. The shared tmux isolation
guard remains in force: invocation from inside the office refuses an explicit
socket that matches the caller's ambient `$TMUX` server. This command does not
provide an override for that refusal.

The physical window is named `__run__NAME`, which cannot be an envelope
address. It receives no agent identity, custody hooks, or generated guides.
Direct sends and broadcasts cannot target it, and tmux's message-paste helper
refuses that namespace. Its process runs once: exited output and exit status
stay in a dead pane for inspection, with no agent restart, resume, presence,
or stall tracking. The reconciler preserves these windows even with an empty
agent roster. Close it explicitly, for example
`tmux -S "$TMUX_SOCKET" kill-window -t "$TMUX_SESSION:__run__monitor"`;
closing it does not recreate it. Reusing an existing run name fails instead
of replacing or restarting the process. Ordinary manual typing in a run shell
remains available through tmux. Do not rename it into an agent window.

Board transitions run in one isolated Lua script and preflight both Redis key
types before mutation; `take` also checks destination emptiness before its
first write. This keeps the one-doing-ticket invariant under concurrent
`office take` calls and prevents WRONGTYPE from leaving a removed-only ticket.
A malformed todo/held entry is moved without rewriting into the stored
`invalid` list instead of being discarded or permanently blocking later
tickets; the h-mesh CLI renders that list without interpreting its contents.
`office hold --reason TEXT [ID]` requires
and stores the blocking reason; with an explicit ID it can park a queued ticket
without taking it or displacing the active ticket. The one-open-task limit
applies to `doing`, not `hold`: parked tickets remain visible in `office list`
with their reasons and are still subject to hold-duration alerts.

That visibility depends on the reader as well as the stored record. h-mesh
stores `hold_reason` and its CLI renders it, but a reader running an older CLI
can show the ticket's held state, title, and age without showing the reason.
A guarantee that depends on what the reader is running is not a guarantee
about what every intended reader can observe. In a mixed-version office,
communicate the reason through a channel every intended reader can consume
until their CLI deployment supports it; otherwise the work is visibly parked
but its explanation is effectively hidden.

Use `office return [ID]` to put work back in `todo`; `cancel` remains a terminal,
auditable state and `delete` is the explicit permanent-removal operation.
`office done --outcome {completed,passed,failed} [ID]` requires and records the
result in the ticket. The h-mesh CLI renders that stored verdict in `office
list`; an older reader can list the completed ticket without displaying its
outcome, just as it can omit `hold_reason`.
An interactive legacy invocation of plain `office done` prompts for that
outcome; a non-interactive invocation fails with the exact replacement syntax.
Already-hired agents deliberately keep their existing guides indefinitely,
including custom content and the old plain-`done` example; upgrades do not
rewrite workspace instructions. New hires receive the updated guide. The
use-time prompt/error is the compatibility bridge that makes this divergence
safe without silently destroying customized context.
`office show ID [-a AGENT]` prints a complete ticket from an enrolled board
without changing its state, timestamps, queue position, or stored bytes. This
is the inspection path; taking a ticket is reserved for actually starting it.
`office retitle --title TEXT [ID]` corrects a ticket on the caller's own open
board (`todo`, `doing`, or `hold`) without changing its identity, timestamps,
state, or queue position. It does not edit another agent's board or closed
work. Both audit channels record the old and replacement title.
`office reopen ID [--todo]` brings one of the caller's own `done` or `cancelled`
tickets back, so a short re-review reuses the ticket instead of a new one. By
default it goes to `doing`, with a fresh `started_ts`, and refuses (changing
nothing) if the caller already has a `doing` ticket -- the one-doing invariant is
enforced by the same preflighted Lua transition as `take`. `--todo` queues it
instead. `done_ts` and `outcome` are cleared, but the earlier verdict is kept as
a note (`reopened; was done (failed)`), so reopening never erases what was
decided. Another agent's board cannot be reopened.
`office note [-i ID] TEXT` (or `--stdin`/`--file`) appends `{ts, by, text}` to a
`todo`, `doing` or `hold` ticket (default: the `doing` one) so tried/found
context lives on the ticket, readable with `show` and counted as `notes:N` in
`list`. The append is an exact-entry `LSET`, so a concurrent transition makes it
fail with "try again" rather than lose either change, and unknown fields on the
stored ticket are preserved. A note is limited to 2,000 characters and a ticket
to 50 notes; past either limit the command refuses rather than truncating or
dropping history. Notes travel with the ticket through take/hold/return/done.
Notes are the owner's: there is no way to annotate another agent's board.
`office add` allocates a 32-lowercase-hex ticket id before sending its
asynchronous envelope, includes that id in the payload, and prints it—not the
transport stream id—after the envelope is admitted. This is the allocated
identity the board will use if downstream creation succeeds, not proof that a
ticket was created. Transport and custody logs establish later delivery states.

One piece of work keeps one ticket id across boards. `office add -a A,B,...`
allocates the id once and sends the same id to every listed board (all
destinations are checked before anything is sent; duplicates in the list are
collapsed), and prints it once. `office add --id ID` reuses an existing 32-hex
id instead of allocating one, to give the same work to another board later; it
is refused if that board already holds the id in any state, and a send that
fails midway prints the boards already served and the `--id` to re-run with.
`office show ID -a all` is the office-wide lookup: it reads every enrolled
board (read-only; unreadable entries are skipped, never quarantined) and prints
one JSON line `{"agent", "state", "ticket"}` per match, so a `rel:<id>` or a
quoted id resolves without guessing `-a`. Plain `office show ID [-a AGENT]` is
unchanged, as are tickets already stored and the `-r/--related` option (related
ids are still stored unvalidated). The id is already the first column of
`office list` and what `add` prints; there is no notification on `add`.

`office add --authority TEXT` sets the ticket's authoritative directive once, at
creation: what the creator is instructing or approving, up to 2000 characters.
The assignee reads it in `office take` / `office show` as
`"authority": {"text", "by", "ts"}` and reconciles from the ticket rather than
from forwarded chat. Only the text travels in the envelope: `by` is the
switch-stamped L2 source of the AddTicket and `ts` is the ticket's creation
time, both written by `lib.board_interaction.add_ticket`, so a payload cannot
claim another author (any `by`/`ts` it carries is discarded; a missing,
blank, non-string or over-length text leaves the ticket without the field).
The field is carried through take, hold, return and done like the other
ticket fields, and an older reader simply omits it. It is deliberately not
editable afterwards: to supersede a directive, add a new ticket with
`--related <old-id>` and cancel or delete the old one. A quoted approval
chain, expiry or per-directive history are not modelled.

`office delete ID` is deliberately scoped to the caller's own board. Delegated
creation is not authority to destroy another agent's queued or in-flight work,
even when the caller originally raised it. A miss therefore names that scope
and says that `delete` cannot withdraw a task assigned to another agent instead
of implying that an otherwise valid cross-board id does not exist. Coordinate
with the assignee to return, cancel, or delete work in their custody.

`office purge AGENT --cli {claude,codex,agy} [--profile ACCOUNT] --confirm AGENT`
permanently deletes a *retired* agent's on-disk CLI session history, the
deliberate complement to `hire --fresh` (which only ignores it) and to `let-go`
(which leaves it so a later `--resume` works). It is irreversible, so:

- It refuses while `AGENT` is still in the registry (let it go first: a live
  session may rewrite its own history), and refuses if the registry cannot be
  read. `--confirm` must repeat the exact name; names are validated as one
  agent segment, so no glob, prefix or path can reach it.
- `--cli` and `--profile` are required inputs, not looked up: retirement
  deletes the agent's `launch` and `profile` keys, so afterwards nothing records
  which CLI or account it ran. Run it once per CLI/account that agent used.
- It acts on the calling host's `$HOME` and the agent's own working directory
  (`get_agent_workdir`), mirroring `has_session_history` location for location
  (`modules.tmux.ops.purge_session_history`): claude deletes the
  `~/.claude[-ACCOUNT]/projects/<cwd>` directory; codex deletes only the
  `rollout-*.jsonl` files under `~/.codex[-ACCOUNT]/sessions` whose
  `session_meta` cwd is the agent's; agy rewrites the shared
  `~/.gemini/antigravity-cli/history.jsonl` without that workspace's lines
  (other lines kept byte for byte) via temp file, fsync and atomic replace.
  Symlinks are never followed.
- agy's file is shared and agy takes none of our locks. The rewrite is retried
  if the file changed while it was being built, and fails with nothing removed
  after five tries. A line appended in the microseconds between the final check
  and the replace can still be lost; closing that needs agy's cooperation.
- Not covered: `claude_sdk` agents' memory contexts (`lib/chat_memory.py`), a
  different store and a separate concern.

It prints how many sessions (claude, codex) or history lines (agy) it removed.
For review work, use `passed` or `failed`. Hold a QUESTIONS review while its
answers block the verdict; use `completed` only when its questions are
non-blocking. Returned work joins the back of a nonempty `todo` queue.

The receiving port delegates to the settled `lib.agentlifecycle` API.

`office delivery STREAM_ID [--wait SECONDS]` answers "where is the message I
just sent?" for the id `office send` prints. It prints one JSON line,
`{"stream_id", "state", "locations": [{"agent", "stage"}], "confirmation"?}`, and
changes nothing. `state` is `queued` (in egress, ingress or claimed but not
started), `delivering` (the effect may have begun), `delivered`, `failed`
(rejected, or the destination retired), `unresolved` (the effect may have
happened but its outcome was not recorded), `partial` (a broadcast whose copies
differ) or `unknown` (not in any custody list: never sent, or aged out of the
1,000-entry `opened` receipt list). `--wait` polls every half second until
`delivered`, `failed` or `unresolved`, or the timeout. ⚠ `delivered` means the
text was pasted and submitted to the terminal, or stored in an api participant's
mailbox -- it does **not** mean it was read; no component records that.
`confirmation` is only what the watchdog still retains for a pasted delivery:
`pending` (awaiting the agent's next activity), `unverified` (none seen), or
`null`. The trace lives in `core/receipts.py` and reads the existing custody
lists by the stream_id in the frame header; it adds no state and does not change
custody semantics.

Delivery evidence has three read paths because it has three different facts:
`dead` contains raw envelopes explicitly rejected by a recipient;
`office unresolved [--agent AGENT]` shows envelopes whose external effect may
have happened; and `office undeliverable [--agent AGENT]` shows envelopes known
not to have begun because their destination retired. The latter two are
tenant-level, read-only evidence. Neither has replay, delete, or expiry today;
those missing exact-identity resolution verbs are a deliberate product gap,
not permission to discard the records automatically.
`stop_agent` transfers the retired instance's queued receive custody to the
phase-appropriate tenant evidence list, then removes its per-name receive keys
and paused marker with its registry membership and delivery lock. A later hire
that reuses the name therefore cannot inherit either messages or coordination
state, while every transferred envelope remains named by exact identity.
Registry removal also
atomically clears the tenant lead key when (and only when) it still names the
retired agent; all type checks occur before the first removal, so a runtime
error cannot leave only half of that combined transition applied.

modules/api keeps its own separate delivery path outside the
ingress/processing/opening model above, so its retirement exposure needed a
different answer, not the same one applied twice. `office retired-inbox
[--agent AGENT]` shows an api-type agent's already-delivered mailbox content
still unread when its destination retired -- conserved into tenant-level,
read-only evidence rather than left in place (where a same-named successor's
own client could read it) or bare-deleted (destroying it outright). Same
posture as `unresolved`/`undeliverable`: no replay, delete, or expiry verb.
Separately, `lib.reply_correlation`'s delivered.s* provenance keys are now
bound to the agent's current INCARNATION, not its bare name: a fresh
incarnation id is minted atomically with registry membership on a genuinely
new hire (self-healing once for any agent already registered before this
shipped) and deleted at retirement, so a same-named successor's reply cannot
validate against provenance the retired predecessor established -- the id
changes underneath a reused name, not a cleanup step someone has to
remember to run.

`office send -a AGENT --reply-to STREAM_ID TEXT` opts a reply into exact
correlation: `STREAM_ID` is the id shown in the `[reply to X: office send -a
X --reply-to <id> "..."]` hint line a tmux agent gets when the message it's
answering came from an api-type source (see `modules/tmux/README.md`).
`--reply-to` is format-checked locally for fast feedback (rejects anything
that isn't a 32-character lowercase hex id before sending at all), but that
is a courtesy, not the trust boundary -- the recipient's door
(`modules/api/port.py`'s `deliver_api`) independently validates the claim
against real delivery provenance and silently drops it if it doesn't hold,
same as if `--reply-to` had never been passed. See `lib/reply_correlation.py`
and `modules/api/README.md` for the full mechanism.

`office send -a AGENT --context CONTEXT TEXT` names the hot-tier memory
context (`lib/chat_memory.py`, `modules/claude_sdk/port.py`) this message
belongs to, for a `claude_sdk` destination: the same `CONTEXT` on a later
send recalls this exchange, a different one (or omitting `--context`
entirely) doesn't. `--context` is format-checked locally the same way
`--reply-to` is (`core.keys.validate_segment`), and is simply ignored by any
destination whose port_type isn't `claude_sdk` -- every existing opener
already reads only the payload fields it recognizes and drops the rest.
`office contexts -a AGENT` lists a `claude_sdk` agent's currently-live
contexts, reading `lib/chat_memory.py`'s `ChatMemory` directly (same "own a
Redis client, read state directly" shape `office status`/`office peers -v`
already use, not a round trip through an envelope).

`office doc` attaches a document once and refers to it by hash, so a
versioned checklist or spec is not re-pasted into every message (and the
reader cannot review the wrong version). `office doc put PATH [--name NAME]`
stores the file's bytes under their SHA-256 in tenant-scoped Redis hashes
(`docs`, `docs.meta`, `docs.names`; `lib/doc_store.py`) and prints
`doc:<first 12 hex>`; put that reference in a message or ticket. Identical
bytes dedupe to the same hash and keep the first upload's metadata.
`office doc get REF [-o PATH]` writes the bytes to stdout or `PATH`,
`office doc info REF` shows hash, filename, MIME type, size, author and
upload time, and `office doc ls` lists everything. `REF` is `doc:<hash>`, any
unique hash prefix of at least 8 hex characters, or a `--name`: re-putting a
new version with the same name moves the name to it, while every older version
stays reachable by its hash. Names may not look like a hash prefix. The size
cap is `send-file`'s (10 MiB). This is direct Redis state like `office status`;
nothing crosses the switch. There is no delete or expiry yet, so stored
documents persist until the tenant's Redis state is cleared.

Operational compatibility warning: the legacy `office` CLI deployed where
this module was developed silently loses messages submitted with `office send
--stdin`. It still reports success and prints the submitted byte count, but the
recipient never receives the message; four review verdicts were lost before
the discrepancy was detected. Do not treat that byte count as recipient-side
delivery evidence. On that deployment, use `--file /dev/stdin` or a real file
instead. The defect did not reproduce through h-mesh: a real PTY/Redis check
followed one stdin envelope by identity through recipient opening, and the
committed regression asserts the recipient opens the exact reported identity
and body. This does not make a general promise about another CLI implementation
or deployment.

`office status` reports `working`, `idle`, or `unknown` from the watchdog's
activity-derived presence record. A separate `blocked` hash means only that a
past delivery could not be verified; it does not establish that the agent is
currently unable to work, so status renders it as `delivery unverified`
context—with its age, or an explicit unknown age—instead of replacing presence
with a `blocked` state. DeliveryVerifier clears the marker when a later
input/output/tool activity timestamp is strictly newer than its `since`, or
when a later delivery verifies. A quiet agent can retain it indefinitely:
there is no TTL or manual resolution today, so the age is essential context,
not a promise that the uncertainty will self-clear. With no presence evidence
the state remains `unknown`, never guessed available from the board or guessed
blocked from delivery uncertainty.

`office usage [-a AGENT] [--since ISO] [--json]` totals the tenant's token
usage, per agent, CLI and model, and prices it with `pricing.py`. It reads the
`usage` stream that the watchdog's `ActivityTailer` fills from claude and codex
session files; `office` itself measures nothing. A model with no pricing entry
is listed as `unpriced` and left out of the USD total.

agy agents are a known limitation, not a failure. agy's `history.jsonl` records
only what was submitted and carries no token counts, and its richer per-turn
data (`~/.gemini/antigravity-cli/conversations/*.db`) is opaque protobuf with no
readable schema, so the watchdog has no usage source for agy and emits no
usage records for it (`modules/watchdog/activity.py`; asserted by
`test_agy_emits_no_usage_records`). Every enrolled agent whose `launch` is `agy`
therefore gets a placeholder row with model `not collected`, dashes for the
counts, and `unpriced` for the cost (`"collected": false` in `--json`). That row
means "usage cannot be measured", not "usage was zero". It adds nothing to the
total, so the total understates real spend whenever an agy agent is running.
Collecting agy usage would need a new data source in the watchdog; no
collection code exists in `office`.

## Inter-office (proof of concept)

One office can send a message to an agent in another office. The CLI itself
POSTs to the other office's api door; a separate relay daemon handles replies
only (it is not part of this module). Message kind only, no attachments.

**Sending.** `office send --office OFFICE -a AGENT TEXT` (or `--stdin` /
`--file`) looks OFFICE up in the address book and sends
`POST <url>/agents/AGENT/envelopes` with `Authorization: Bearer
coop.<token-id>.<secret>` and body `{"text": ..., "as": "<participant>"}`. On a
`202` it prints `sent to OFFICE/AGENT: N bytes (<stream_id>)`, where
`stream_id` is the remote office's. **202 means queued on the remote office's
egress, never delivered.** `--office` cannot be combined with `--reply-to` or
`--context`; `all`, `api` and `host` are refused before any I/O. Nothing goes
onto the local bus.

| exit | meaning |
|---|---|
| 0 | accepted (202) |
| 1 | refused, provably nothing sent: bad arguments, unknown office, no token, connection refused / DNS failure, HTTP 4xx (401 token wrong or revoked, 403 policy, 404 unknown agent, 422 invalid) |
| 2 | usage error (argparse) |
| 3 | outcome unknown: timeout, connection reset, HTTP 5xx or a redirect, a 2xx that is not a 202 with a valid `stream_id`. The message may have been accepted; do not blindly retry |

Redirects are never followed (urllib would replay the Authorization header).
Error output never contains the token, including text echoed by the server.

**Reply routing.** After a 202 the CLI writes `xsent.s<stream_id>` =
`{sender, sender_incarnation, correlation_id, office, agent, ts}` with a 24h TTL; the relay
uses it to hand a reply back to the sender. `sender_incarnation` is read
before the POST so a same-named successor never inherits a reply (absent means
the relay treats the reply as uncorrelated). If this write fails the message
was still sent: the CLI exits 0, warns on stderr, and that one message's reply
cannot be routed back. A remote agent's reply is only correlated when it
answers with `--reply-to <stream_id>`, which its pasted message shows because
the cooperation participant is an api-type source.
The stored `correlation_id` comes from that accepted outbound envelope's
202 response, and the reply relay reuses it for local admission after the
same office/agent/incarnation validation. A missing or invalid response
correlation is stored as null without changing the already-accepted send's
outcome; legacy/null correlations preserve reply routing but cannot join the
original conversation trace. The remote reply's own correlation is not used
as a substitute.

**Address book (sending side).** Redis, via `core.keys.prefix`:

| resource | holds |
|---|---|
| `offices` | hash `name -> {url, participant, added_by, added_ts}`, no secrets |
| `offices.tokens` | hash `name -> token`, read only by `send --office` and the relay |
| `xsent.s<stream_id>` | the reply-routing record above |

- `office office-add NAME URL [--participant P] --token-stdin [--allow-plaintext] [--no-verify]`
  (lead only). The token is read from stdin and there is no `--token` option,
  so it never appears in process arguments. It also enrols the local api
  participant `relay` with a `StartAgent` envelope if it is absent, and refuses
  if `relay` already exists as a non-api agent. It starts no process.
  **Link check:** before saving, `office-add` calls `GET URL/whoami` with the
  token (5 s timeout, redirects never followed). The remote answers
  `{office, participant, kinds, api: 1}`; a 401 carries a top-level `reason`
  (`revoked`, `unknown_token`, `participant_not_enrolled`). On any failure it
  prints one line saying what is wrong and what to do, ends with
  `nothing saved`, and exits 1: token revoked/unknown/participant gone (ask the
  remote lead for a fresh `coop-grant`), participant mismatch (`the remote
  office says this token belongs to participant 'X' ... use --participant X`),
  unreachable, a url that is not an api door, an older remote with no
  `/whoami`, an unknown `api` version. When `--participant` is omitted it is
  taken from the remote's answer. `--no-verify` skips the call for offline use
  (then `--participant` is required, and a wrong participant or token only
  shows on the first send; the command says `NOT verified`).
- `office offices` lists names, urls and participants. It reads only the
  `offices` hash, so it cannot print a token, and makes no network call.
  `office offices --check` also probes every entry with its saved token and
  appends `link: ok`, `link: participant mismatch`, `link: token problem`
  (revoked / unknown / participant gone / no token saved), `link: cannot
  verify` (remote has no `/whoami`) or `link: unreachable`, each with the
  reason. It checks all entries and exits 1 if any is not ok.
- `send --office` failures name the cause: 401 (token revoked / unknown /
  participant gone, per the remote's `reason`), 403 (the grant does not allow
  that destination), 404 (no such agent at the remote).
- `office office-rm NAME` (lead only) removes the record and the token.
- TLS is the supported default. `http://` to a non-loopback host is refused
  unless `--allow-plaintext` (or `H_MESH_ALLOW_PLAINTEXT=1`) is given, for lab
  test beds; the token then travels in the clear and `office-add` warns.

**Not clients.** `coop-grant` and `office-add` also record the participant (and `relay`) in the `interoffice-participants` hash. Their inbound messages never open an `unreplied` entry, so the watchdog does not report them as "unanswered"; offices set up earlier are recognised from their coop-token records and the reserved `relay` name. The registry port_type stays `api`.

**Granting (receiving side).** `office coop-grant PARTICIPANT` (lead only)
enrols PARTICIPANT as an api participant (`StartAgent`, same mechanism as
`hire`), mints a Message-only token with `lib.coop_tokens.grant` and prints it
once; it is never stored in clear. Give it to the other office's lead for
`office-add`. `office coop-revoke TOKEN-ID` (lead only; the 16-hex id, a full
token is refused) revokes it. `office coops` lists id, participant, kinds and
state, whitelisting what it prints. Enforcement of what a token may do
(Message kind only, forced `as`, own mailbox only, immediate revocation) lives
in the api door, not here.

**"Lead only" is advisory.** The CLI compares `AGENT_NAME` with the tenant
`lead` key and fails closed when there is no lead. `AGENT_NAME` is an
unauthenticated environment variable and any process with Redis access can
write the same keys directly, so this stops mistakes and non-lead agents, not a
hostile local process. The same applies to the tokens hash: hiding tokens from
`offices` prevents accidental disclosure, not reading Redis.

## Board event notices (#18)

Two board events now also send an ordinary `Message` envelope (written through
the same `send()` path as any message, so it shows in the mesh log), instead of
leaving the other party to poll `office list` or write a summary by hand:

- `office add -a TARGET ...` sends the `AddTicket`, then a notice to TARGET:
  who assigned which ticket (short id and title) and how to see/start it. The
  notice is queued behind the `AddTicket` on the same path, so the ticket is
  normally on the board before the notice is read. **One notice per target per
  30 seconds** (`notice.add` key, `SET NX EX`): a bulk add of N tickets
  announces only the first and says so, and the rest are on the board. Never
  sent to oneself.
- `office done` sends a notice to the ticket's `created_by` with the outcome,
  only when that is another agent still enrolled. A self-created ticket or a
  retired/unknown creator sends nothing and is not an error. `cancel`, `return`
  and `hold` stay silent.
- `--no-notify` on `add` and on `done` suppresses the notice (scripted bulk
  work, or when the creator was already told).
- Notices end with "Notice only, no reply needed" and nothing in `add`/`done`
  reads the inbox, so they cannot start an acknowledgment loop; the agent guide
  already says acknowledgments are not answered.
- The board change is committed first. If the notice cannot be sent, `add`
  and `done` still succeed and print a warning on stderr.

The guide's "send the lead ONE line when a ticket is done" rule is separate:
when the lead created the ticket, it now receives this notice as well as that
line. Whether to drop the manual line is the guide owner's call, not made here.

## Shell-mangled text (#16)

`office` cannot see what a shell removed: inside a double-quoted argument
`` `name` ``, `$(cmd)` and `$VAR` are expanded before `office` starts, usually
to nothing, and the shorter text is accepted like any other. The byte count
printed on success is the count of what *arrived*, so it cannot reveal the loss.
What `office` can do, and now does for text given as a command-line argument
(`send`, `broadcast`, and `add`'s `-t` and `-d`):

- **Warn on the hole** the removal leaves: two or more spaces between words
  (`the  setting`, from ``the `x` setting``) print a warning on stderr that
  points at `--stdin` / `--file`. It never blocks the send and does not change
  the output or exit code. It is a heuristic: it does not see a word dropped at
  a line edge or next to punctuation, it fires on aligned columns, and two
  spaces after `.`, `!` or `?` and leading/trailing spaces are ignored.
- **Refuse blank text** (`""`, whitespace only): this is what a body that was
  entirely a substitution becomes, and sending an empty message helps no one.
  `--stdin` already refused empty input.
- **Never check `--stdin` / `--file`** text; it is never shell-parsed, which is
  why it is the way to send anything containing backticks, `$` or quotes.

`office add` previously took its description only from `-d`. It now also
accepts `--description-stdin` and `--description-file PATH` (exactly one of the
three), so a ticket description with code in it no longer needs shell quoting.

`office clone-to-all <repo-url> [-a AGENT,...] [--dry-run]` clones a repository
into each selected tmux agent's workspace (`<workdir_root>/<agent>/<repo>`).
When invoked as `root` (for example via `sudo h-mesh-office clone-to-all` or
`docker exec -u 0`), it hands ownership of the cloned tree and any newly
created workspace parent back to the unprivileged workspace user
(`lib.paths.ensure_workdir_ownership`, using `os.lchown` without following
symlinks) so agents do not hit git's `detected dubious ownership` check or
permission errors when creating `.venv` or editing files in-repo (#28).
