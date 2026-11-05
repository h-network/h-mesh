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

`ingress_stalled` is `null` while the destination's ingress is empty or draining.
When the switch's stall monitor sees the same frame at the head of a non-empty,
unpaused ingress for `INGRESS_STALL_SECONDS` (default 60) it becomes an object
`{since, depth, head_stream_id, lease, port_type}`: nothing is consuming that
queue. `lease` is `held` when a port holds the delivery lease but is not
draining (hung) and `absent` when nothing runs or each kick dies (crash loop,
missing handler). It is evidence, not a verdict on the agent, and clears when
the queue drains. See `core/README.md`.

`GET /agents/{agent}/contexts` lists the hot-tier memory contexts a
`claude_sdk` agent (see `modules/claude_sdk/README.md`) currently has live
turns for -- `{"agent": "...", "contexts": [...]}`. 404 for an unknown agent
or one whose `port_type` isn't `claude_sdk` (no memory contexts to have). A
direct `lib/chat_memory.py` read, not a round trip through the agent's own
`ListContexts` envelope kind -- both doors read the same underlying store,
this one just doesn't need the target agent's ingress to be drained first.

## Authentication: office-wide token and cooperation tokens

Two kinds of bearer token are accepted (`authorize` in `server.py`):

- **`API_TOKEN`** (office-wide): full power over every route, including
  `Command`, `StartAgent` and any `as`. Whoever holds it can paste into any
  terminal and enrol or stop agents -- hand it only to clients you would give a
  shell. Not meant for another office.
- **Cooperation token** (`coop.<token-id>.<secret>`, minted by
  `lib/coop_tokens.py`): lets ONE remote office act as ONE enrolled `api`
  participant of this office. `token-id` is 16 hex characters (public: listing
  and revocation), `secret` is 43 url-safe characters. The receiving office
  stores only the SHA-256 of the secret in the `coop-tokens` hash
  (`token-id -> {participant, sha256, kinds, created_ts, revoked}`); the full
  token is shown once at grant time.

A cooperation token may do exactly this, everything else is `403`:

| allowed | rule |
|---|---|
| `POST /agents/{agent}/envelopes` | `kind` must be `Message` (the `{"text": ...}` shorthand included); `as` is forced to the bound participant -- omitted is filled in, a different value is `403`; destination must be a concrete agent: not `all`, `host`, `api`, nor any `api`-type participant's mailbox or the lifecycle `host` port |
| `GET /agents/{participant}/messages`, `.../messages/stream` | only the bound participant's own mailbox (`after=` and `Last-Event-ID` resume as usual); any other mailbox is `404` |
| `GET /whoami` | what the presented token is bound to (see below) |

Not reachable with a cooperation token: `/agents`, `/agents/{agent}`, board,
activity, contexts, `/alerts`, `/restdoc`, `/docs`, `/redoc`, `/openapi.json`,
`/health`, and any non-`Message` kind (`Command`, `StartAgent`, `StopAgent`,
`PauseAgent`, `ResumeAgent`, `Attachment`, ...).

### `GET /whoami`

Lets a caller check a token (and the participant it is meant for) before the
first send. Accepted for a cooperation token and for the office-wide
`API_TOKEN`. `200`:

```json
{"office": "<pod>/<tenant>", "participant": "coop-a", "kinds": ["Message"], "api": 1}
```

`participant` is the participant bound to the presented token (`null` for the
office-wide token, which is bound to none; its `kinds` is `["*"]`). `api` is the
version of this response. A refusal is `401` with a JSON body that has the usual
human `detail` and a top-level machine-readable `reason`:

| `reason` | meaning |
|---|---|
| `unknown_token` | missing, malformed, unknown id or wrong secret (not distinguished) |
| `revoked` | the secret is right but the token was revoked; only the holder of the real secret is told this |
| `participant_not_enrolled` | the token is valid but its participant is not enrolled (any more) as an `api` participant |

`reason` appears only on `/whoami`; every other route keeps a bare, reason-less
`401`. The token is never echoed in any response.

Cooperation tokens replace the HMAC `kid`/`sig` check for their principal: a
published API does not demand a signature from them, because the token already
binds the caller to its participant. They are read from Redis on every request,
so `revoke` takes effect on the next request, and an open SSE stream is closed
(`event: error`, `token revoked`) within about a second of revocation. A storage
failure while verifying fails closed (`401`). A malformed, unknown, wrong or
revoked token is an indistinguishable `401` on every route except `/whoami`.

Limits to know: this is policy, not storage security -- every agent of an office
shares one Redis with no ACL, so a hostile co-resident agent could write
`coop-tokens`; hashing protects a leaked dump, not that. Use TLS between
offices (the default the API enforces for non-loopback binds); plaintext is
only for a lab test bed via the existing `H_MESH_ALLOW_PLAINTEXT=1` opt-in.
Message-only means no attachments across offices in this version.

## Action audit (#27)

A bounded record of what an agent *did* (shell and tool commands), as opposed to
the switch's log of what it *said*. h-agent's per-CLI tool hooks submit the
records; this door stores and serves them. Design: `docs/design/action-audit.md`.

| route | |
|---|---|
| `POST /agents/{agent}/audit` | store one record; `202 {"id": "<cursor>"}`. `agent` must be enrolled (else `404`); a body over **8 KiB** is `413`; an invalid record is `422` with the reason |
| `GET /agents/{agent}/audit?after=<cursor>&limit=N` | records oldest first, `{"agent", "audit": [...], "next_cursor"}`; `limit` 1-1000 (default 100), same cursor convention as `/activity` |

Both need the office `API_TOKEN` (reads included). A cooperation token is `403`
on both. When the API runs with no `API_TOKEN` at all, like every other route it
is open.

**Record** (JSON object, unknown fields are `422`):

| field | rule |
|---|---|
| `tool` | required; tool name (`Bash`, `Edit`, ...), 1-64 chars of letters, digits and `. _ : @ -` |
| `args` | the command or tool input: a string, or JSON (stored as canonical JSON text). **Masked here**, truncated to 2048 bytes, `args_truncated` says so |
| `exit`, `out_bytes` | integer result code (or `null`) and size of the output. The output itself is never stored |
| `ts` | the caller's ISO-8601 time. Asserted, not proven |
| `cli`, `session`, `call_id` | free labels (same character rule); `call_id` pairs a `pre` and a `post` record |
| `phase` | `pre` or `post` (a `pre` with no `post` shows a command that never returned) |
| `v` | optional, must be `1` |

The server adds `agent` (from the URL), `recv_ts` (this process's clock, the one
to trust), `args_sha256` (SHA-256 of the *masked, untruncated* args), and
`prev_hash: null`: reserved for a later version that chains records, never
settable by a caller. A stored record is a `record` field in the Redis stream
`pod:<p>:tenant:<t>:agent:<name>:audit`, capped at **5000 entries per agent**
(approximate `MAXLEN`); the key is not removed when the agent is retired.
`H_MESH_AUDIT_FILE=/path/audit.jsonl` additionally appends every accepted record
(plus its `id`) as one JSON line; unset means off, a write failure never fails
the request, and rotating or deleting the file is the operator's job.

**Secret masking happens on the server, on every record,** whatever h-agent
already did (h-agent should mask too). The full text is masked *before* it is
digested or truncated, so a secret cannot survive by being cut in half and the
digest cannot be used to guess one. Masked: `Authorization`/`Bearer`/`Basic`
values, `--token`/`--password`/`--secret`/`--api-key`-style flag values,
`NAME=value` where NAME contains `token`, `secret`, `password`, `key` or
`credential`, JSON `"password": "..."`-style members, `X-...-Key/Token` headers,
`user:password@` in URLs, cooperation tokens (`coop.<id>.<secret>`), PEM blocks
(also cut-off ones), and common key shapes (`sk-`, `ghp_`, `github_pat_`,
`xox?-`, `AKIA...`, JWTs). It is **best-effort pattern masking**: a secret passed
as a bare positional argument has no shape to recognise and is stored as sent.

### What the action audit cannot guarantee

All agents run as one OS user with the same Redis and the same `API_TOKEN`.
Therefore, plainly:

- An agent can **forge** a record for any agent (it has the token and Redis).
  `agent` and `session` are asserted, not proven.
- An agent can **skip** auditing: unset/edit its CLI hook config, run commands
  outside the hooked tool, or use a CLI without hooks.
- An agent can **delete or rewrite** the stream and the mirror file.
- Capture covers hooked CLI tool calls only; operator terminal work is covered
  only if h-agent also wraps that shell.
- `ts` is client-supplied; only `recv_ts` is the server's.

So this is an accountability and diagnostics log for honest mistakes and
reconstruction, **not tamper-evident evidence**. Real guarantees need the
deferred separation: a writer under a different OS user (unix socket, append-only
file owned by it), then a per-agent hash chain so deletion is detectable. Out of
scope now; the record format (`v`, `call_id`) leaves room for `prev_hash`.

Reading is not an access boundary either: `office audit` reads Redis directly
and any agent can; the API route additionally needs `API_TOKEN`.

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
