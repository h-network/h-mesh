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
`office add` allocates a 32-lowercase-hex ticket id before sending its
asynchronous envelope, includes that id in the payload, and prints it—not the
transport stream id—after the envelope is admitted. This is the allocated
identity the board will use if downstream creation succeeds, not proof that a
ticket was created. Transport and custody logs establish later delivery states.

`office delete ID` is deliberately scoped to the caller's own board. Delegated
creation is not authority to destroy another agent's queued or in-flight work,
even when the caller originally raised it. A miss therefore names that scope
and says that `delete` cannot withdraw a task assigned to another agent instead
of implying that an otherwise valid cross-board id does not exist. Coordinate
with the assignee to return, cancel, or delete work in their custody.
For review work, use `passed` or `failed`. Hold a QUESTIONS review while its
answers block the verdict; use `completed` only when its questions are
non-blocking. Returned work joins the back of a nonempty `todo` queue.

The receiving port delegates to the settled `lib.agentlifecycle` API.

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
