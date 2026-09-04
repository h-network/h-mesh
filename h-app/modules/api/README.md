# api

The REST endpoint and mailbox port used by external clients. HTTP callers send
envelopes, inspect fixed tenant state providers, and poll or stream replies.
Inbound envelopes for a participant whose registry `port_type` is `api` are
drained by `deliver_api` into that participant's Redis Stream inbox.

| file | what it holds |
|---|---|
| `server.py` | `ApiSettings`, FastAPI application factory, authentication, REST routes, SSE responses, and generated REST documentation |
| `port.py` | `deliver_api` ingress handler, mailbox retention bound, and the `python -m modules.api.port AGENT` delivery entrypoint the switch spawns per kick |
| `__main__.py` | package execution shim to the thin service launcher |

The continuously running entrypoint is `h-app/services/api.py`. Uvicorn owns
the server loop. The SSE generators are connection-lived polling responses,
not independent daemons.

Delivery itself is a separate, one-shot subprocess per kick, same shape as
every other port module: `python -m modules.api.port AGENT` reads `POD`,
`TENANT`, and optionally `REDIS_URL`, takes the shared delivery lock, checks
the pause marker, and calls `deliver_api` once. This is what
`core.service.transmission` actually spawns for any destination whose
registry `port_type` is `api` -- if this file has no `main()`/`__main__`
guard, that subprocess runs, does nothing, and exits 0, so every api-bound
envelope (Telegram replies included) sits in ingress forever with no error
anywhere. `tests/test_api.py`'s `RealApiPortSubprocessTests` runs this
exact subprocess against a real Redis to catch that class of regression --
a unit test of `deliver_api()` alone, or a mock asserting the right argv was
Popen'd, cannot.

The external route contract intentionally preserves the predecessor's API so
Telegram and web clients remain ordinary API consumers. Internally it uses h-mesh names:
`core.channels`, `core.envelope`, `core.keys`, and `core.registry`.

`GET /agents/{agent}` keeps two independently owned facts separate. The
`presence.state` value is activity-derived (`working`, `idle`, or `unknown`).
`delivery_unverified` is either `null` or the retained `since` and `stream_id`
for a delivery whose result could not be verified. That marker is not proof
that the agent currently cannot accept a prompt, so clients must not use it as
an admission gate. They may show it as a warning; the attempted send's own
result is the evidence for whether new work was admitted.

`GET /agents/{agent}/contexts` lists the hot-tier memory contexts a
`claude_sdk` agent (see `modules/claude_sdk/README.md`) currently has live
turns for -- `{"agent": "...", "contexts": [...]}`. 404 for an unknown agent
or one whose `port_type` isn't `claude_sdk` (no memory contexts to have). A
direct `lib/chat_memory.py` read, not a round trip through the agent's own
`ListContexts` envelope kind -- both doors read the same underlying store,
this one just doesn't need the target agent's ingress to be drained first.

## SSE idle keepalive

`/agents/{agent}/activity/stream` and `/alerts/stream` (both routed through
`_stream_response`) send a bare `: keepalive\n\n` SSE comment line whenever
the stream has gone `SSE_KEEPALIVE_INTERVAL_S` (3s) without a real event.
Comment lines are ignored by `EventSource` and by
`clients/telegram/bot.py`'s parser (any line starting with `:` is skipped)
-- their only job is to put a byte on the wire so an idle connection isn't
byte-silent to whatever sits between the client and this process.

This exists because a fully idle stream previously sent nothing at all, and
on a real install the connection was observed dropping and reconnecting
every 5-7 seconds -- burying unrelated log lines under a churn of
`disconnected, retrying in 1s` warnings. `uvicorn`'s `timeout_keep_alive`
(default 5s) was suspected but does not explain it: that timer only starts
after a response completes (`on_response_complete` in uvicorn's H11
protocol), and a live reproduction of the exact reported topology (bare
`uvicorn.run()`, no reverse proxy, telegram bot on loopback, run on an
isolated VM) held an idle stream open past 30s with zero disconnects, both
via curl and via `clients/telegram/bot.py`'s actual `urllib` client path.

**What actually severs the connection at 5-7s on that install is
unidentified.** This fix is deliberately independent of that cause, not a
diagnosis of it: the periodic keepalive resolves the symptom regardless of
which layer enforces an idle cutoff, since the connection is never
actually idle long enough to reach it. If the reconnect spam persists on a
real install after this ships, the cause is still an open question.

Separately: the idle-poll loop in `_stream_response` calls
`_read_stream_entries` (one Redis `XRANGE`) every 100ms per open connection
regardless of whether anything is queued -- measured at exactly ~10
`XRANGE`/s per idle stream against a real Redis. This is not addressed
here; see the commit that added this section for the measurement and why
it was left alone.

## Reply correlation (`in_reply_to`)

A mailbox message read from `GET /agents/{agent}/messages` (or its SSE
stream) may carry a top-level `in_reply_to` field: the `stream_id` of the
envelope it answers. **Absent means genuinely absent, not `null` and not
`""`** -- a reply that isn't correlated simply doesn't have the key, so
`"in_reply_to" in message` is the right presence check, not a truthiness
check on its value.

There are exactly three states, not two, and the third is permanent, not
transitional:

- **correlated** -- the replying agent passed a real, previously-delivered
  id (`office send --reply-to STREAM_ID`, or automatically for OpenShell
  agents, whose reply is generated mechanically from the envelope that
  triggered it). `in_reply_to` is present and trustworthy.
- **uncorrelated (the permanent fallback)** -- the replying agent didn't opt
  in, can't opt in (an older CLI, a route that doesn't go through `office
  send`), or is talking on a route this feature doesn't cover. `in_reply_to`
  is absent. This is not a bug and not going away: correlation is opt-in by
  design (see the design note below), so every client that reads mailbox
  messages must keep working for this case indefinitely, not treat it as a
  gap to be closed later.
- **malformed or unverifiable, dropped before storage** -- a claimed
  `in_reply_to` that isn't a well-formed 32-character lowercase hex id, or
  is well-formed but was never actually delivered to the agent claiming to
  answer it *by this specific API client* (provenance is bound to
  `(replying agent, originating client, stream_id)`, not just `(agent,
  stream_id)` -- an id Telegram delivered to an agent must not validate
  when that agent replies to a different API client naming the same id),
  is stripped by `modules/api/port.py`'s `deliver_api` before the envelope
  is ever written to a mailbox. This state is never visible on the wire --
  from a client's perspective it is indistinguishable from uncorrelated.
  It exists so a confidently wrong pointer can never reach a client; see
  `lib/reply_correlation.py` for the validation itself, and its own
  module docstring for why this key is deliberately not the same one
  `modules/tmux/port.py`'s `mark_delivery_pending` writes for watchdog.

Design note, in the words of the client this shipped for: **correlated when
the replying agent passes it, accepted behaviour when not.** An earlier
design considered inferring correlation automatically from delivery order
(first-delivered, first-replied); it was rejected because that's exactly
backwards in the overlapping-turn case that motivated this feature, and a
fix that only narrows a bug without resolving the case that prompted it is
worse than an honestly absent field. `office send --reply-to` is exact
because the replying agent (or OpenShell's automatic reply path) states
which envelope it means, rather than something else guessing.
