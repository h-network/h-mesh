# watchdog

Agent observability: stalls, blocked deliveries, credential expiry, and board
hygiene (stuck `doing`/`todo`/`hold` tickets, unanswered client messages,
ack-loops).

**Observe-only, by design.** `service.Watchdog`'s docstring says it plainly:
report tenant stalls and blocked deliveries without repairing either. Nothing
in this module restarts an agent, retries a delivery, or edits a ticket. If a
change here starts doing any of that, it has drifted from the boundary this
module was built to.

| file | what it holds |
|---|---|
| `activity.py` | `ActivityTailer` -- tails each agent's CLI session file (claude/codex/agy) into a privacy-reduced per-agent `activity` stream, and emits token usage records |
| `presence.py` | `PresenceSampler` -- derives `working`/`idle`/`unknown` per agent from recency of that activity stream |
| `verification.py` | `DeliveryVerifier` -- judges aged `pending.verify` markers (written by `modules.tmux.port.mark_delivery_pending`) against later activity, and sets/clears the `blocked` hash |
| `service.py` | `Watchdog` -- polls stalls, blocked deliveries, credential expiry, and the doing/todo/hold/unreplied/ack-loop alert family; `main()` runs all four (three observers plus the alerting poll) as one daemon loop |

The daemon's *entrypoint* lives in `h-app/services/watchdog.py` (a thin
launcher, same shape as `services/tmux_reconciler.py`), but the daemon's
actual logic stays here, same convention `modules/tmux` already established.

## Alert delivery

Two channels, deliberately different:

- **`alerts` stream** (`_alert`): stalls, blocked deliveries, credential
  status changes. Passive -- a stream a human or client may or may not be
  watching.
- **Lead's pane directly** (`_notify_lead`): doing/todo/hold/unreplied/
  ack-loop nags. The watchdog is not a roster member with an egress queue
  anyone drains, so it cannot use `core.channels.send`. Instead it builds the
  same v4 envelope `send` would, admits it onto the lead's ingress with the
  same `core.queues.admit_ingress` bound every other forward uses, and
  delivers it with `modules.tmux.deliver_tmux` -- the same in-process call the
  switch's own `kick` callback uses after a normal forward. See
  `Watchdog._notify_lead`'s docstring for the full reasoning, including why a
  full lead ingress drops the alert instead of dead-lettering or retrying it.

## Imports from core

`core.registry` (`is_member`/`members`/`port_type`), `core.keys` (`prefix`),
`core.policy` (`require_allowed`), `core.envelope` (`EnvelopeError`/`build`/
`encode`), `core.logging` (`log_record`/`mirror`), `core.queues`
(`admit_ingress`), and `modules.tmux` (`run_tmux`, `deliver_tmux`). No new
third-party dependency: `redis` is already ambient in h-mesh
(`core/service.py`, `modules/tmux/reconciler.py`).
