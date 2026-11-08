# core

The wire format, addressing, and the switch: envelope encoding, Redis key
construction, the registry (who's on the tenant), policy (import/export
ACL), the two delivery channels (send/receive), and the switch daemon that
forwards a message by address without reading its payload.

Everything else is a module that plugs into this.

| file | what it holds |
|---|---|
| `envelope.py` | wire frame: `build`/`parse`/`encode`, header splicing (`stamp_source`, `advance_hop`), address resolution |
| `keys.py` | `prefix()`, `delivery_lock_key()` and the named per-resource helpers (`receive_*_key`, `incarnation_key`, inter-office `offices_key` family, `run_windows_key`/`run_remove_key`) — the sole Redis key constructors, segment validation |
| `config.py` | local state directory resolution (`H_MESH_STATE_DIR`, default `~/.h-mesh`) |
| `channels.py` | `send()`/`receive()`, unreplied tracking (api *clients* only -- inter-office participants never open an entry), `DeadLetter`, and best-effort failure feedback to tmux senders |
| `queues.py` | `admit_ingress()` — shared atomic ingress-admission Lua op |
| `registry.py` | read-only registry access (`members`/`member_types`/`is_member`/`port_type`) and `is_interoffice()` (is this api participant a relay/coop speaker for another office) |
| `policy.py` | import/export tag ACL (`allows`/`require_allowed`) |
| `dispatch.py` | generic port_type -> handler registry and dispatch with renewable per-agent delivery leases, invoked only by whatever the switch's kick actually calls -- the switch itself never imports this |
| `logging.py` | contract-shaped JSON event logging and durable stdout mirroring, plus `configure_logging()` — the stdlib logging threshold for entry points |
| `retention.py` | count-based trimming for completed-task and dead-letter queues |
| `ingress_health.py` | `IngressStallMonitor` — payload-free detection of an ingress whose consumer stopped draining |

| `receipts.py` | `trace()` — read-only lookup of one stream_id across the custody lists (queued / delivering / delivered / failed / unresolved) |
| `windowlog.py` | agent-window custody-log validation, tailing, offset tracking, and bounded truncation |
| `service.py` | the `Switch` class — BLPOP/forward/kick loop, is what "the switch" is |

The tenant participant hash is the wire-visible `registry` resource. Delivery
itself remains outside core: pass a `kick(agent, port_type, envelope)` callback to
`Switch` when an edge module should be notified after ingress admission. With
no callback, forwarding still completes and core records terminal `kick_skipped`.
The production switch uses `transmission` to start
`python -m modules.<port_type>.port <agent>`; `port_type` is currently assumed
to equal its module directory name, and the child reads the frame from ingress.
Kicked ports do not inherit the switch's structured stdout: their custody
records return over a dedicated validated pipe, while arbitrary stdout/stderr
and crash tracebacks go to `ports.log` beside the daemon logs.
For tmux, office, and openshell ports, one kick drains ingress until empty,
atomically claiming one envelope at a time into the participant's durable
`processing` list. Processing means the effect has not started and is safe for
a successor to recover. Immediately before calling an opener, custody moves to
`opening`; that state is never replayed because the external outcome may be
unknown. Success moves it to a per-agent `opened` receipt list capped at 1,000;
explicit rejection moves it to `dead`; exceptions and abandoned `opening`
claims move to the tenant-level `unresolved` evidence list. `office unresolved`
reads that evidence without consuming or retrying it. There is deliberately no
automatic expiry and currently no resolution verb: future exact-identity,
audited retry/dead/resolve actions require per-kind policy rather than silently
choosing loss or duplication today.
Retirement classifies the same phases without erasing custody: queued
`ingress` and claimed `processing` envelopes are known not to have begun, so
they move to the tenant-level `undeliverable` evidence list; `opening` moves to
`unresolved`; bounded `opened` receipts remain intact. `office undeliverable`
is read-only. Like `unresolved`, it has no replay/delete verb and no expiry;
those missing operator actions are a deliberate gap, not an implied cleanup.
Operators therefore have three truthful terminal surfaces: `dead` for an
explicit rejection, `unresolved` when an effect may have happened, and
`undeliverable` when the destination retired before the effect began. Keeping
them separate avoids changing dead's raw-envelope contract or calling a known
non-effect unknown.
Kicks are availability hints, not
tokens paired one-to-one with envelopes: an older entry left by a missed or
crashed kick therefore cannot consume the only attempt for the request behind
it, while an opener crash leaves every not-yet-claimed envelope queued.
Broadcast fan-out reads one registry snapshot containing both participant names
and port types, admits every recipient atomically, then starts each recipient's
delivery port. Each resolvable recipient therefore records `kick_started` or
`kick_unknown`. A recipient with no usable type, or a switch with no callback,
records terminal `kick_skipped`; it is not a promise of later retry. Resolvable
members still start delivery when another member is unresolvable, making a
partial outcome explicit instead of discarding the whole broadcast.
After fan-out, the switch also sends the initiating participant one consolidated
fact-only notice naming skipped recipients: it says no delivery attempt was
started, not that later receipt is impossible. This makes the partial outcome
visible at the initiating office/API surface without contradicting an existing
port that may independently drain an already-admitted queue.
The drain is intentionally unbounded at the current queue scale: ingress has a
configured cap, and one worker holding the per-agent delivery lease is safer
than spawning competing consumers. If that worker is killed between entries,
the switch's maintenance pass observes non-empty ingress after the renewable
lease expires and records `kick_restarted` while starting a replacement. It
does not rely on an unrelated future message to supply the missing kick.

Core logging uses the `H_MESH_WRITER`, `H_MESH_CUSTODY_FILE`,
`H_MESH_LOG_FILE`, `H_MESH_LOG_QUIET`, and
`H_MESH_LOG_FILE_AGENT_ONLY` environment variables. Switch registry refresh is
configured with `REGISTRY_POLL_SECONDS`.

## Inter-office keys

`keys.py` names the tenant-level keys used for office-to-office messaging.
None carries an agent segment, so retiring an agent never deletes them.

| helper | resource | type / content |
|---|---|---|
| `offices_key` | `offices` | HASH name -> JSON `{url, participant, added_by, added_ts}`; no secrets |
| `office_tokens_key` | `offices.tokens` | HASH name -> token string (sender side) |
| `office_cursor_key` | `offices.cursor` | HASH name -> last SSE event id handled by the reply relay |
| `office_sent_key` | `xsent.s<stream_id>` | STRING JSON `{sender, office, agent, ts}`, 24h TTL |
| `coop_tokens_key` | `coop-tokens` | HASH token-id -> JSON `{participant, sha256, kinds, created_ts, revoked}` (receiving side; only the sha256 is stored) |

⚠ `office_sent_key` writes the `s` prefix itself: a 32-hex stream_id with no
a-f character is all digits, which `validate_segment` rejects. It also refuses
an empty id or one containing `.`, because the id comes from a remote office's
response and `prefix()` would otherwise split it into extra resource segments.

## Dead-consumer detection (ingress stalls)

`admit_ingress` accepts frames until `INGRESS_MAX` whether or not anything is
draining the queue, so a consumer that died used to be visible only as senders
being rejected. Ports (not clients) drain ingress, and a port can fail in three
ways: crash on every kick, hang while its renewing lease stays held, or have no
resolvable handler. `IngressStallMonitor` runs in the switch's maintenance pass
and watches *progress*: if the same frame stays at the head of a non-empty,
unpaused ingress for `INGRESS_STALL_SECONDS` (default 60), it writes
`ingress.stalled` (HASH agent -> JSON `{since, depth, head_stream_id, lease,
port_type}`), logs one `ingress_stalled` record (reason names the lease state,
`count` is the depth, `waited` the seconds) and shows it as `ingress_stalled` in
`GET /agents/{agent}`. When the head changes or the queue empties the entry is
deleted and `ingress_recovered` is logged. A paused agent is never flagged.

The head is identified by the stream_id in the fixed v4 header -- the same bytes
the switch already reads to forward -- so no payload is parsed. Detection only
surfaces: recovery remains the existing `kick_restarted` re-kick plus operators,
and a switch restart restarts the clock. Not covered: an `api` participant's
*client* (e.g. the Telegram bot) that stops reading its `inbox` stream -- the api
port keeps draining ingress into `inbox` (capped at 1,000), so the queue never
stalls; that needs a client-side heartbeat and is a separate design.

## Delivery trace

`receipts.trace()` reports where one `stream_id` is by scanning egress, ingress,
`processing`, `opening`, `opened`, `dead`, the api `inbox`, and the tenant
`unresolved`/`undeliverable` evidence in a single Lua call that compares only the
fixed-header stream_id, so no payload is read or transferred. It is the engine
behind `office delivery`. "Delivered" is a custody fact, never a read receipt, and
a frame that has aged out of the 1,000-entry `opened` list is reported `unknown`,
not guessed. The api port drains ingress and then writes the mailbox, so a
frame can be briefly in neither during that hand-off.

## Two logging systems, one file

`logging.py` holds both, and they are not interchangeable:

- **Custody records** (`log_record`/`emit`/`mirror`) — one JSON object per
  line on **stdout**, durably mirrored, parsed by conservation checks. The
  variables above configure these. No level, no threshold: a record is either
  written or it is a hole in the evidence.
- **Diagnostics** (`logging.getLogger(...)`, e.g. `dispatch.py`'s two
  `logger.error` calls when a registered handler no longer resolves) — human
  prose on **stderr**, with a threshold set by `H_MESH_LOG_LEVEL`.

`H_MESH_LOG_LEVEL` (default `INFO`) takes level **names** only — `DEBUG`,
`INFO`, `WARNING`, `ERROR`, `CRITICAL`, with `WARN`/`FATAL` as the stdlib
aliases — case- and whitespace-insensitive. A number is not a name:
`H_MESH_LOG_LEVEL=10` is an unrecognised value, not DEBUG. Anything
unrecognised falls back to `INFO` and logs a `WARNING` saying so, rather than
failing a delivery process over a typo in a tenant's env. It is the same knob,
spelled the same way, as `clients/telegram/README.md` documents for the
Telegram client — that client keeps its own copy of the resolver because it
imports nothing from `core`, so the two move together or not at all.

⚠ **`configure_logging()` belongs in an entry point, never at import of a
library module.** `core/dispatch.py` deliberately does not configure anything:
it is imported by processes core does not own, including the test suite, and a
library that sets the root logger's level decides verbosity for all of them.
Without a configuring entry point, stdlib logging's lastResort handler prints
WARNING and above as a bare message — no timestamp, no logger name — and
silently drops everything below it.

Every entry point that starts a process of ours calls it as its first
statement:

| entry point | process |
|---|---|
| `core/service.py` `main()` | the switch (`h-mesh switch`) |
| `modules/{api,office,openshell,tmux}/port.py` `main()` | one-shot delivery per kick — covers both `python -m modules.<type>.port` as the switch spawns it and `h-mesh <type>-port`, which imports the module and calls the same `main()` |
| `services/tmux_reconciler.py` `main()` | the tmux reconciler |
| `services/api.py` `main()` | the REST API (see the uvicorn note below) |
| `services/session.py` `main()` | the session WebSocket door |
| `services/web_console.py` `main()` | the web console / Mini App gateway |
| `tools/smoke_tmux.py` `main()` | the tmux smoke tool, the one caller that reaches `dispatch`'s handler resolution today |

`services/telegram_bot.py` is the deliberate exception: it imports
`clients.telegram.bot`, which configures itself at import from the same
variable, and a second `basicConfig` there would be a no-op that reads like
the real thing. `clients/web/server.py` is configured by its
`services/web_console.py` launcher and not by itself — running that server
directly gets stdlib's unconfigured default, the price of `clients/` importing
nothing from `core`.

⚠ In `services/api.py` the call goes **before** `uvicorn.run`. Uvicorn's own
`dictConfig` sets `disable_existing_loggers: False` and never touches the root
logger, so the threshold survives it — but it does not reach uvicorn's own
`uvicorn.*` loggers, which take their level from uvicorn's `log_level`
argument rather than from root.

The threshold is the verbosity knob and nothing else: a real failure keeps its
own severity. Note that `basicConfig` sets the **root** level, so `DEBUG` also
turns up third-party libraries (redis included) in that process.
The forwarding loop survives transient Redis failures: connection resets and
timeouts, `LOADING`, `BUSY` (a long script running on the server), `READONLY`/
`TRYAGAIN`/`MASTERDOWN` during failover. It records an `error` event whose reason
names a closed category (`connection`, `timeout`, `loading`, `busy`, `readonly`,
`tryagain`, `masterdown`), sleeps with a doubling backoff (0.2s up to a 5s cap,
reset by the next successful pass), and lets redis-py reconnect. Anything else
-- `WRONGTYPE` from a corrupt key, a script error, bad credentials, a code bug --
stays fatal on purpose: retrying those would hide a defect behind a live-looking
daemon. `transient_redis_category()` in `service.py` is the single classifier.

A frame is popped from egress before it is admitted, so a Redis fault after the
pop decides its fate. The switch splits those faults by whether the command
could have run:

- **Provably rejected before execution** -- `busy`, `loading`, `readonly`,
  `tryagain`, `masterdown`. The write did not happen, so the same frame is
  retried in memory with the same backoff for up to `FRAME_RETRY_ATTEMPTS` (8,
  about 16s). If it is still rejected, the frame is moved to the sender's `dead`
  list with a `dead_lettered` event whose reason names the category and attempt
  count -- never dropped silently. If even that dead-letter write stays
  rejected, a `frame LOST` event carrying the stream_id is logged before the
  error propagates.
- **Ambiguous** -- `connection`, `timeout` on a write. The command may have
  executed, so core's at-most-once holds: no blind retry (it could duplicate).
  The switch records `forward_unknown` with the closed category, counts it in
  `Switch.unknown_outcomes`, and the loop survives; the frame is not re-queued.
- **Reads** (registry lookups) are idempotent, so any transient category is
  retried; repeating a read cannot duplicate anything.

