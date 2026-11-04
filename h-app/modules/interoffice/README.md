# Inter-office reply relay

`services.interoffice_relay` runs in the sending office. Its idle supervisor is
part of `h-mesh start`, setup and upgrade, even before any remote office is
configured. It opens one SSE worker for each valid recorded office; it sends no
outbound requests itself. The office CLI performs the remote POST.

The relay uses the cooperation bearer token from Redis to read
`GET <url>/agents/<participant>/messages/stream?after=<cursor>`. TLS is the
supported default. For the lab only, `H_MESH_ALLOW_PLAINTEXT=1` explicitly permits
HTTP. Redirects and URLs containing credentials, queries or fragments are
refused; tokens never enter process arguments, log messages or tenant env files.

All Redis keys are local to `POD`/`TENANT` through `core.keys.prefix`:

| Resource | Contents |
|---|---|
| `offices` | Hash: office name to JSON `{url, participant, added_by, added_ts}` |
| `offices.tokens` | Hash: office name to its secret cooperation token |
| `offices.cursor` | Hash: office name to the last committed SSE entry ID |
| `relay-seen.o<digest>` | ZSET: newest 4,000 remote stream IDs, ordered by padded numeric SSE cursor |
| `relay-seen-index.o<digest>` | Hash: remote stream ID to its ranked ledger member, for replay lookup |
| `relay-seen-ready.o<digest>` | One-time legacy-backfill completion flag |
| `interoffice-participants` | Hash: local api participant to `relay` or `coop`; written by `office-add` (the relay) and `coop-grant`. Marks senders that speak for another office so `core.channels` never opens an "unanswered message" entry for them (see `core/registry.py: is_interoffice`) |
| `xsent.s<stream_id>` | Sender CLI receipt: `{sender, sender_incarnation, correlation_id, office, agent, ts}`, TTL 24 hours |

Only the local lead can manage cooperation addresses and credentials through
the office commands. `office-add` enrolls the local api-type participant
`relay`; a participant with that name and another type is an error. The relay
waits for this enrollment before admitting messages. It does not create or
change address-book entries or token values.

An SSE entry ID is a Redis `milliseconds-sequence` cursor. An envelope
`stream_id` is a separate 32-character hex ID; `in_reply_to` names that envelope
ID. A correlated reply waits up to two seconds for the sending CLI to publish
the POST receipt, checks the recorded office, remote agent and sender
incarnation, and routes to that enrolled local sender. Missing, expired or
mismatched provenance falls back to the current local lead. Unsolicited
messages without `in_reply_to` go straight to the lead. With no enrolled lead,
the event stays pending and its cursor does not advance.

The receipt's `correlation_id` is the accepted outbound envelope's ID from
the remote POST's 202 response. For a validated sender mapping the relay
passes that ID to local `core.channels.send`, so the outbound request and
locally admitted reply share one correlation across the office boundary.
The remote reply's own correlation ID is not trusted or copied. Missing,
expired or mismatched mappings (including stale sender incarnations) use a
fresh local correlation when falling back to the lead. Legacy receipts
without a valid 32-character lowercase hex correlation still route to their
validated sender, but use a fresh correlation; these old IDs cannot be
reconstructed from a stream ID or SSE cursor.

This does not change stream or reply identifiers: the POST receipt remains
keyed by the remote outbound `stream_id`, remote `in_reply_to` still selects
that receipt, and normal local admission creates a new local `stream_id`.
As before, the relay does not copy the remote `in_reply_to` into the local
envelope. The relay's admission ledger suppresses a replay of an already admitted
remote stream ID, including in the enqueue/checkpoint crash window.

Admission uses `core.channels.send`, with local source `relay`, kind `Message`,
and text prefixed `[from <office>/<agent>]`. Remote names remain untrusted text;
they never become local source identities. The tmux port reads that
prefix to print the working reply command
(`office send --office <office> -a <agent> "..."`) instead of a reply hint at
the local `relay`; see `modules/tmux/README.md`. No remote `Command`, lifecycle or
attachment kind is admitted. Unsupported kinds are explicitly rejected and
checkpointed so one bad entry cannot block later messages. Malformed SSE data,
policy denials and storage failures leave the cursor unchanged for retry.

The cursor is persisted **after local admission**, not after the destination
opens the message. Each Message must have a valid 32-character lowercase hex
remote `stream_id`. Normal `core.channels.send` performs the policy checks and
builds the local envelope. A relay-local Redis adapter changes only its single
queue append: a bounded Lua operation appends that envelope and records its
remote ID together. Core send, registry and policy implementations are not
copied or changed. A failed or pending admission records nothing. If Redis
commits but the response or later cursor checkpoint is lost, replay finds the
committed ID and advances the cursor without a second local append. Concurrent
workers also append only once. SSE remains at-least-once; this closes the relay's
duplicate-admission window and does not redefine downstream opening semantics.

The ledger is scoped to the local office plus **alias, normalized endpoint and
remote participant**, hashed into the resource suffix. Tokens are excluded:
rotating a token or resetting a cursor must not replay replies already delivered
by that cooperation. Another alias, endpoint or participant has its own ledger.
Replays are checked even when the lead is absent or the original sender retired.

Retention follows the API's **count** contract, not a guessed time period.
`modules/api/port.py:MAILBOX_MAXLEN` is 1,000, with approximate stream trimming
and **no age expiry**. `store.SEEN_LEDGER_LIMIT` is 4,000 (4x that limit), and a
regression test imports the API constant and requires the factor to remain at
least four. Both the ranked ZSET and its ID index are trimmed to this bound in
the same atomic operation as admission; neither expires by time. Cursors are
padded and lexically ranked, so full uint64 milliseconds/sequence values never
lose precision through floating-point scores. A repeated ID at a newer cursor
refreshes its place without appending again. The guarantee depends on the
remote mailbox retaining the newest roughly 1,000 entries by count. If API
retention grows or changes to time-based retention, update the ledger contract
and its guard test too. Remote replies trimmed before observation are lost.

An upgraded pre-ledger office can already have a committed cursor. On the first
connection for that cooperation, the relay reads the retained stream from the
oldest entry and **only marks entries at or below that committed cursor** as
seen, without appending them. Newer entries still take normal admission. This
backfill is bounded by the ledger size and idempotent across interrupted reads;
a durable completion flag prevents repeating it. It completes upon reaching the
committed boundary, encountering a newer entry, or the API's tail keepalive.
A partial disconnect does not claim completion, and changing/resetting the
committed cursor invalidates the old backfill. Fresh cooperations with neither
cursor nor ledger admit retained messages normally: historical local delivery
cannot be inferred without evidence.

New cooperations start at the oldest retained entry, without tail-seeding.
`office-add` writes the record and token; **the running relay supervisor** notices
URL, participant or token changes, replaces the old worker and clears its
cursor. `office-add` itself does not clear a cursor, and a supervisor starting
after an offline configuration change cannot infer its previous in-memory
settings. A superseded worker cannot append or checkpoint against changed
credentials. Token rotation retains the matching ledger, so the deliberate
oldest-entry replay does not paste old replies again. Reconnect uses jittered
exponential backoff from one to thirty seconds. Finite reads and SIGTERM
cancellation let stop/upgrade shut down idle workers.

Container restart and recreation **with the state volume retained** preserve
the cursor, ledger and cooperation credentials. Dropping the receiving office's
state volume destroys all three; after cooperation is configured again, retained
remote replies can be admitted again because no local evidence survives. An
already lost pre-fix cursor with no ledger has the same irrecoverable ambiguity.
Dropping only the remote state volume destroys its mailbox (unobserved replies
are lost), registry and grants; re-enroll cooperation with its fresh token before
exchanging new messages. The other office's surviving ledger still protects its
known stream IDs. None of these cases guesses that an unknown message was
previously delivered.

Logs report only local aliases, closed event categories, HTTP status and local
admission IDs. They do not contain bearer tokens, remote response bodies,
message text or endpoint URLs. `interoffice_relay.log`, `.pid` and
`.pid.identity` use the shared daemon lifecycle, including pidfd-safe stop.

`h-app/tests/test_interoffice_relay.py` and `test_interoffice_replay.py` use a fake HTTP SSE server plus isolated
Redis namespaces to exercise resume and replay, the normal switch/mailbox
path, late sender records, lead fallback, incarnation checks, policy failures,
cooperation changes, malformed frames, redirect refusal and secret-free logs.
