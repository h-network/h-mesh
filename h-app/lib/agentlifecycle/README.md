# agentlifecycle

A library, not a port. Start/stop/pause/resume logic for an agent/participant:
writes registry/policy/HMAC-key desired state, and delegates the actual
mechanism (a tmux window, for now) to callbacks the caller supplies. Supports
`tmux` and `api` port types today; openshell support was removed until that
module actually exists in h-mesh.

`StartAgent` accepts `lead: true` for tmux agents. It publishes the registry
membership, optional window cause, and tenant lead selection atomically so a
reconciler cannot create the new pane with a non-lead guide. Here “atomically”
means no-write-or-all-write, not merely that Redis isolates a Lua script: every
type-sensitive command is preflighted before the first mutation, because a
runtime error in `EVAL` does not roll back earlier writes. This deliberately
reassigns leadership if another lead is configured: hire the replacement with
`lead: true` before stopping the old lead to avoid any leaderless interval.
`StopAgent` uses the same preflight rule, then compare-and-deletes the lead
selection only when the retired agent still owns it, so retiring the former
lead cannot undo that transfer.

| file | what it holds |
|---|---|
| `lifecycle.py` | `start_agent`/`stop_agent`/`pause_agent`/`resume_agent`, callback-driven, no port of its own |
