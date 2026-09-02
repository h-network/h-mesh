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

## State ownership at stop/re-hire

A Redis key containing an agent name does not by itself say whether the state
belongs to one lifecycle instance, to the durable logical name, or to another
module's evidence. The stop boundary uses the following explicit ownership
map; an unlisted key is a defect, not implied permission to inherit it.

| state | classification and stop policy |
|---|---|
| registry field; owned tenant `lead` | Instance membership. Removed or compare-deleted in the stop script. |
| `processing`, `opening`, `opened`, `ingress`, `paused`, `delivering` | Instance-owned receive and delivery state. Deleted in the same script. Tenant `unresolved` is the durable exception below. |
| `launch`, `profile`, `provider`, `resume`, `skip-permissions`, `claude-tools`, `tags`, `hmac-keys`, `window.cause` | Instance-owned launch, permission, policy, credential, and creation state. Deleted in the same script. A successor must state its own optional configuration; omission means the default, never predecessor inheritance. |
| tenant `unresolved` | Durable custody evidence. Preserved across retirement and name reuse; each record carries the destination name and exact raw envelope. |
| `dead`, API `inbox`, `egress` | Durable message custody or outcome evidence, preserved to avoid silent deletion. Their current name-keying can expose predecessor inbox content or queued sends to a successor; core/API owns the identity-aware resolution and must not replace this with lifecycle `DEL`. |
| `activity`, `delivery.markers`, `pending.verify`, `usage.attributed`, `usage.requests`, `delivered.s<stream-id>` | Durable observation, verification, usage-dedup, or reply-provenance evidence. Preserved. Watchdog/tmux/reply-correlation own the remaining name-reuse attribution question: successor activity can currently confirm a predecessor `pending.verify`, and `delivered.*` can validate predecessor provenance until its owner-defined TTL expires. CLI request-id uniqueness across incarnations is not yet established for usage dedup. |
| `tasks.todo`, `tasks.doing`, `tasks.hold`, `tasks.done`, `tasks.invalid` | Intentionally inherited logical-name work. A re-hired agent resumes the board assigned to that name; lifecycle does not delete it. |
| `presence`, `blocked`, `unreplied`, `acks`, and per-agent `*.alerted` watchdog suppression keys | Instance-derived status that a successor should not inherit, but owned and interpreted by watchdog/core conversation tracking. Preserved activity can briefly make a successor appear `working`; `blocked` is false until successor activity self-heals it; client-keyed unreplied/ack suppression can hide a new warning. Those owners need a retirement/reset boundary; lifecycle does not silently delete their state in this branch. (`acks` has no writer today, but is still classified before one is added.) |
| `activity.offset` | Intentionally preserved observer cursor over the likewise-preserved activity stream. Resetting it would replay predecessor history as new activity. |

Tenant-wide keys such as `alerts`, `usage`, `credential.alerted`, and
`window.log.offset` are not per-name successor state. Actual window teardown
also remains an external callback after the Redis transition, not a Redis key
mutation.

| file | what it holds |
|---|---|
| `lifecycle.py` | `start_agent`/`stop_agent`/`pause_agent`/`resume_agent`, callback-driven, no port of its own |
