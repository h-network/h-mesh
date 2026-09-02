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
Use `hire NAME --lead` to hire a lead or transfer leadership to a tmux agent;
the registry row and lead selection are published atomically, before tmuxhost
creates the pane and its lead-specific `AGENTS.md`. Retiring the current lead
clears the selection. Hire a replacement with `--lead` before retiring the old
lead when the office should always have one; retiring a former lead cannot
clear a leadership transfer that has already happened.

Board transitions keep the one-doing-ticket invariant atomically, including
concurrent `office take` calls. A malformed todo/held entry is moved without
rewriting into the visible `invalid` list instead of being discarded or
permanently blocking later tickets. `office hold --reason TEXT [ID]` requires
and stores the blocking reason; `office list` shows it on held tickets. Use
`office return [ID]` to put work back in `todo`; `cancel` remains a terminal,
auditable state and `delete` is the explicit permanent-removal operation.
`office done --outcome {completed,passed,failed} [ID]` requires and records the
result, so completed review work retains its verdict in `office list`.
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
`office add` allocates the ticket id before sending its asynchronous envelope,
includes that id in the ticket payload, and prints the ticket id—not the
transport stream id—so the returned value is the identity later stored on the
recipient's board.
For review work, use `passed` or `failed`. Hold a QUESTIONS review while its
answers block the verdict; use `completed` only when its questions are
non-blocking. Returned work joins the back of a nonempty `todo` queue.

The receiving port delegates to the settled `lib.agentlifecycle` API.
`stop_agent` removes the retired instance's ingress queue and paused marker as
well as its registry membership and delivery lock, so a later hire that reuses
the name cannot inherit queued messages or paused state. Registry removal also
atomically clears the tenant lead key when (and only when) it still names the
retired agent.
