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
envelope. A replay after the enqueue/checkpoint crash window can therefore
produce separate local stream IDs with the same original correlation ID.

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
opens the message. A reconnect or restart resumes from that cursor and ignores
already committed entry IDs. This is **at-least-once local admission**: if the
process crashes or Redis fails after enqueue but before checkpoint, a retry can
enqueue the same reply again. It does not promise exactly-once delivery. Remote
mailbox retention also limits recovery after a long outage; replies trimmed by
the remote API cannot be recovered from a cursor alone.

New cooperations start at the oldest retained entry, without tail-seeding.
`office-add` clears an alias's cursor whenever its record or token changes. A
running supervisor also notices URL, participant and token changes, replaces
the old worker and clears its cursor; a superseded worker cannot checkpoint
against the new configuration. Reconnect uses jittered exponential backoff
from one to thirty seconds. Finite reads and SIGTERM cancellation let normal
daemon stop/upgrade shut workers down, including idle heartbeat streams.

Logs report only local aliases, closed event categories, HTTP status and local
admission IDs. They do not contain bearer tokens, remote response bodies,
message text or endpoint URLs. `interoffice_relay.log`, `.pid` and
`.pid.identity` use the shared daemon lifecycle, including pidfd-safe stop.

`h-app/tests/test_interoffice_relay.py` uses a fake HTTP SSE server plus isolated
Redis namespaces to exercise resume and replay, the normal switch/mailbox
path, late sender records, lead fallback, incarnation checks, policy failures,
cooperation changes, malformed frames, redirect refusal and secret-free logs.
