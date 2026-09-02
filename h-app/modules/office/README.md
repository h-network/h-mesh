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

The receiving port delegates to the settled `lib.agentlifecycle` API.
`stop_agent` removes the retired instance's ingress queue and paused marker as
well as its registry membership and delivery lock, so a later hire that reuses
the name cannot inherit queued messages or paused state. Registry removal also
atomically clears the tenant lead key when (and only when) it still names the
retired agent.
