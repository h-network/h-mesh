# office

The `office` CLI and its receiving port, together. `office` builds and sends
envelopes (hire/letGo/pause/resume, send, board, peers, etc); this module's
own port receives the hire/retire/pause/resume envelopes back and calls into
agentlifecycle to act on them. Same domain, both sides, one owner --
`agentlifecycle` will live here, not in `lib/`, once it moves, since this is
its only real caller.

| file | what it holds |
|---|---|
| `cli.py` | the sending half -- argparse dispatch and every command that builds an envelope or reads/mutates Redis state the caller owns directly |
| `pricing.py` | model token pricing and cost calculation, used only by `usage` |

Not runnable via any packaging yet -- h-mesh has no console-scripts story for
any module, so `office` is reached the same way tests reach it: put `h-app/`
on `sys.path` and call `modules.office.cli.main`.

`profiles` is not a command yet: it needs an `available_profiles()`
(configured-accounts) read that has no h-mesh equivalent anywhere in `core`
or `lib`. `hire --profile` still exists and still reaches the payload, but
without the client-side "unknown account" check the old system had, for the
same reason.

`port.py` (the receiving half) and the `agentlifecycle` move are not built
yet. `lib/agentlifecycle/lifecycle.py`'s own `stop_agent` has an open
`# TODO: purging an agent's other per-agent state on stop needs to be
implemented` that needs resolving before the receiving port ships -- letGo
would otherwise leave more residue behind here than the previous system did.
