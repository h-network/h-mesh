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
| `activity.py` | `ActivityTailer` -- tails each agent's CLI session files (claude/codex/agy) into a privacy-reduced per-agent `activity` stream, and emits token usage records |
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

## Board truthfulness

The watchdog exists to report reality, so a false positive here is worse
than in most modules -- the whole point is trust. A few decisions, made
deliberately rather than by accident (2026-09-02):

- **Log events claim only what's actually known: ALLOCATED / ADMITTED /
  CREATED.** `_notify_lead`'s admission-succeeded log is `lead_alert_admitted`,
  not `lead_alert_sent` -- at that point only ADMITTED (durably queued onto
  the lead's ingress) is known, `deliver_tmux` hasn't been called yet, let
  alone confirmed. Deliberately no second, stronger-sounding event follows a
  successful `deliver_tmux` call either: `deliver_tmux` -> `core.channels.
  receive()` catches `DeadLetter` (window missing, unknown kind, opener
  failure) *inside itself* and returns normally either way, so "no exception
  raised" cannot honestly distinguish a real delivery from an internal
  dead-letter -- there is no claim available beyond ADMITTED at this call
  site. What actually happened during delivery is already recorded by
  `channels.receive()` itself (`received`/`dead_lettered`/`opened`, under
  `module="tmux"`) -- read those, not an inferred "it probably worked."
  `DeliveryVerifier`'s `delivery_unverified` reason text already modeled this
  discipline correctly before it had a name: "not confirmed... cannot
  distinguish loss from a landed paste."

- **`blocked` self-heals.** `DeliveryVerifier` used to only ever CLEAR
  `blocked` in response to a NEW delivery marker verifying -- an agent
  nobody messaged again after one unverified paste stayed `blocked` in
  `office status` forever, regardless of its own ongoing activity. It now
  re-checks every poll: activity after the recorded `since` is exactly the
  evidence a marker-based verification would have accepted anyway, so it is
  granted without requiring a new delivery first.
- **`todo`-duration skips an agent with something in `doing`.** One agent
  works one ticket at a time; anything queued behind it in `todo` cannot be
  started yet no matter how old it gets. That is queueing, not neglect. An
  agent with nothing in `doing` is genuinely free, and an old `todo` entry
  for *that* agent still means what the check exists to catch.
- **`hold`-duration carries `hold_reason`** (the field `office hold
  --reason` now requires; gracefully absent on older entries) into the
  alert text. It still fires on the same schedule -- a stated reason
  explains why a wait *started*, not that it is still justified, and the
  goal is fewer *false* alerts, not fewer alerts. Silencing a held ticket
  because it has a reason would make the watchdog quieter by being blind,
  which is the one failure mode explicitly rejected here.
- **`stalled` alerts carry `last_activity_kind`** (and `last_activity_tool`
  for a tool call) as honest extra context, never as a suppression signal.
  The three-signal stall check cannot tell "genuinely stuck" from "idle
  because it's waiting on a reply" -- from the lead, from a client, from
  anything. A real fix needs a real signal that does not exist yet: peer-
  to-peer unreplied tracking in `core.channels.send()`, symmetric to the
  api-to-tmux `unreplied` it already writes. Until that lands, the alert
  still fires and lets the reader weigh the ambiguity themselves, rather
  than the watchdog guessing wrong in the direction of silence.
- **`_check_ack_loop`'s `acks` hash has no writer yet** in h-mesh's
  `core.channels.send()` (confirmed by reading it directly) -- that family
  is dormant, not broken. Documented in its own docstring so a quiet ack-
  loop check is not mistaken for "no ack-looping happening."

## Imports from core

`core.registry` (`is_member`/`members`/`port_type`), `core.keys` (`prefix`),
`core.policy` (`require_allowed`), `core.envelope` (`EnvelopeError`/`build`/
`encode`), `core.logging` (`log_record`/`mirror`), `core.queues`
(`admit_ingress`), and `modules.tmux` (`run_tmux`, `deliver_tmux`). No new
third-party dependency: `redis` is already ambient in h-mesh
(`core/service.py`, `modules/tmux/reconciler.py`).
