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

  The claim is enforced structurally, not by trusting a naming convention --
  and not by closing off fields one at a time either. Two earlier rounds
  each closed one leak and opened another: first `outcome`/`title`/
  `old_title` (closed by not exposing those parameters), then an arbitrary
  caller-supplied `event` string and free `reason` prose (reviewer's next
  counterexample: `event="lead_alert_delivered"` next to a truthful
  `evidence="admitted"` -- a delivery claim sitting in the field naming the
  record, which nothing was checking). The actual fix: if a caller can pass
  a string that reaches the record, the caller owns the vocabulary, not
  this module. So `_log_lead_alert` accepts no caller-composed string at
  all -- only a closed `kind` key into `Watchdog._LEAD_ALERT_TEMPLATES`,
  which is the *sole* source of `event`, `evidence`, and the `reason`
  template together, as one hardcoded entry. A caller supplies only
  structured values (a lead name, a closed error category, a queue depth)
  to fill that template's named placeholders; it can never set `event` or
  `evidence` independently, so the two cannot disagree. An unrecognized or
  omitted `kind` is a `KeyError`/`TypeError` at the call, immediately --
  never a malformed or contradictory record reaching the log.

  This is checked two ways, not one, per the lesson that a guarantee
  asserted at a boundary wider than the mechanism enforcing it is not a
  guarantee: a static AST check over `_notify_lead`'s own source confirms
  every log call in it targets `_log_lead_alert` (not `log_record`
  directly) *and* passes a `kind` whose literal value is a real key in
  `_LEAD_ALERT_TEMPLATES` -- not just that the call is named right, which
  is what an earlier version of this check verified and reviewer found
  insufficient (a call missing `kind` entirely still named
  `_log_lead_alert` and passed that check, and would only have failed the
  first time it actually executed). Falsified by hand before being
  trusted: a real call site was temporarily edited to drop its `kind`
  argument, the AST check was confirmed to fail on it, and the call site
  was restored.

  A fourth leak, found after the above three: closing the *sentence
  frame* (`event`/`evidence`/`reason`-wording) is not the same as closing
  its *content*. The `unknown` template used to interpolate a
  caller-supplied `detail=str(exc)` -- a fixed template around free text
  is still free text reaching the record, and an exception's message is
  remote-influenced by construction (a Redis/proxy error can embed a
  connection string, a key name, or arbitrary backend text nothing here
  can bound). Every placeholder in every template now has to answer one
  provenance question before it's allowed to exist: *could this value be
  influenced by anything outside this process?* `{lead}` passes (a
  registered agent name, only ever set by an authenticated `office` admin
  action, already carried by the same record's `destination` field
  regardless); `{depth}`/`{ingress_max}` pass (plain ints, never text).
  `{detail}` didn't, and is gone -- replaced by `{category}`, drawn from
  `_admission_error_category`, a small CLOSED mapping this module
  hardcodes by `isinstance` (never `str(exc)`, and never even
  `type(exc).__name__`, since that vocabulary belongs to whichever
  library raised it, not to this module). An exception type nobody has
  enumerated there yet falls back to `"unexpected_error"` rather than
  widening what gets logged to cover it.

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
- **`_EMIT_USAGE_LUA` preflights every key it may write, before the first
  mutation.** A Redis `EVAL` is isolated from other clients but NOT
  transactional with itself -- a runtime error partway through does not
  undo `redis.call()` side effects the same script already applied.
  Confirmed on real Redis (FakeRedis cannot reproduce mid-script `WRONGTYPE`
  faithfully): with `attributed_key` holding the wrong type, the pre-fix
  script emitted the usage record and set its dedup marker, then failed on
  the attribution `SADD` -- a real usage record survives, silently missing
  its delivery correlation, with the caller swallowing the error entirely.
  The fix checks `TYPE` on `stream_key`/`seen_key`/`attributed_key` before
  any mutation and returns an error reply if any is wrong, so the script now
  does nothing at all or everything -- never partially. The eval-failure
  path (preflight rejection or otherwise) also now logs `usage_emit_failed`
  instead of silently returning; a systemic problem here (e.g. something
  elsewhere writing the wrong type to one of these keys) used to be able to
  stop all usage tracking for an agent forever with no trace of why.
- **`_error()` classifies instead of quoting.** Every one of its 14 call
  sites (poll()'s ten per-check catches, run_observers's per-job catch, and
  main()'s three outer per-phase catches) used to log
  `f"{type(exc).__name__}: {exc}"` -- BOTH the exception's own message and
  its class name, durably. Both halves are remote-influenced by
  construction: a Redis/subprocess/proxy error's message can embed
  arbitrary backend text, and a dynamically constructed exception type
  (`type(sentinel, (NameError,), {})`) can put attacker-chosen text in its
  own `__class__.__name__` too -- so neither half was safe to trust as-is.
  `_error_category` (module level, not a `Watchdog` method -- see its own
  comment for why) replaces both with one of a small, closed set of
  literals this module owns:
  - `internal-error (ExactTypeName)` -- only when the exception's EXACT
    type (not merely `isinstance`) is one of `_INTERNAL_ERROR_TYPES`, a
    closed tuple of builtins that are near-certainly OUR coding mistakes
    and essentially never remote-caused (`NameError`, `AttributeError`,
    `TypeError`, `KeyError`, `IndexError`, `UnboundLocalError`,
    `ZeroDivisionError`, `AssertionError`, `ImportError`). Deliberately
    excludes `ValueError`/`RuntimeError` even though both are builtins:
    libraries raise them constantly for malformed/remote-caused input (a
    JSON decode failure is a `ValueError` subclass), so treating either as
    "ours" would send an operator hunting a defect in this module's code
    for someone else's bad data.
  - `internal-error (derived)` -- `isinstance` matched one of the above but
    the exact type didn't. `isinstance` decides the BRANCH (a subclass of
    our own bug type is still our bug); exact identity decides the NAME
    (a subclass's own `__class__.__name__` cannot be trusted the way a
    literal type from this module's own tuple can).
  - `redis-error` / `connection-error` / `timeout` / `os-error` -- known-
    safe categories for common infra failures, a separate closed table
    from `_admission_error_category`'s (that one is specific to one
    Lua-eval admission path; this one covers a much broader surface --
    Redis, subprocess/tmux, envelope building, JSON parsing -- so sharing
    one table would either leak admission's narrow categories into
    unrelated jobs or dilute its precise ones).
  - `external-error` -- the fallback for anything not explicitly
    recognized, including `ValueError`/`RuntimeError` on purpose.

  Diagnostic value after this fix, stated explicitly rather than left
  implied: `job` (unchanged, already on every record) says WHERE; the
  category says roughly WHAT KIND. That pair is deliberately all that's
  promised -- no free text, no exception args, no traceback. This is a
  logging-only distinction: it does not change retry or scheduling --
  every `_error()` call site's caller reruns on its own next scheduled
  cycle regardless of category, the same as before this fix.

  `_error()` itself is now wrapped in a bare `try/except: pass` around its
  whole body -- it is the LAST layer in this chain (all 14 call sites,
  including `main()`'s own outermost per-phase catches, report a failure
  *through* it, and nothing downstream catches a failure of the failure-
  reporter itself), and a rule adopted the same day this function was
  touched: the last layer in a chain must be allowed to fail silently, or
  it becomes the new hole. `_error_category` is proven total by its own
  test (`test_error_category_never_raises_on_hostile_input` -- a hostile
  `__eq__`/`__hash__`, `None`, a plain non-exception object), not just
  argued to be one; the guard around `_error`'s body is defense against
  something outside that function's control instead (a full disk, closed
  stdout), proven the same way by forcing `json.dumps` itself to fail.

## Imports from core

`core.registry` (`is_member`/`members`/`port_type`), `core.keys` (`prefix`),
`core.policy` (`require_allowed`), `core.envelope` (`EnvelopeError`/`build`/
`encode`), `core.logging` (`log_record`/`mirror`), `core.queues`
(`admit_ingress`), and `modules.tmux` (`run_tmux`, `deliver_tmux`). No new
third-party dependency: `redis` is already ambient in h-mesh
(`core/service.py`, `modules/tmux/reconciler.py`).
