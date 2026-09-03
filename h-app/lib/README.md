# lib

Shared code that's imported *between* modules, but doesn't belong in `core/`
(it's not wire format or switch mechanics) and doesn't own a port the way
everything in `modules/` does. If several modules need the same logic and
none of them should own it exclusively, it goes here.

| directory/file | what it holds |
|---|---|
| `agentlifecycle/` | start/stop/pause/resume desired-state logic for a participant, callback-driven, no port of its own |
| `ingress_snapshot.py` | the atomic "drain everything queued" primitive, used by any port's own delivery handler |
| `board_interaction.py` | centralized board/ticket operations -- `add_ticket` (write an incoming `AddTicket` to a board), `normalize_ticket`/`serialize_ticket` (the one ticket shape every reader/writer of a board entry uses, including `office`'s own board commands) |

`add_ticket` reports a positive `RPUSH` result as a confirmed board write. An
exception or impossible nonpositive result after the call is outcome-unknown,
not proof of rejection: the opener exception remains generic so receive
custody preserves the exact envelope in the tenant `unresolved` list. Only a
failure proven before an effect may use the explicit dead-letter path. After a
positive result proves creation, both success-log and task-audit observations
are independently best-effort: their failure cannot reclassify the created
ticket as unresolved, and one observer cannot prevent the other from running.
| `attachment_schema.py` | attachment wire/schema limits (size, mime type, base64 validation), shared by any port that delivers attachments |
| `reply_correlation.py` | `record_delivered`/`was_delivered` -- one `SET key source EX DELIVERED_TTL_SECONDS` per (recipient agent, stream_id) backing opt-in reply correlation, so a reply can't claim an id delivered by one API client while answering a different one, and provenance expires (1h) rather than being retained by count. Openers (`modules/tmux/port.py`, `modules/openshell/port.py`) record a delivery; `modules/api/port.py`'s `deliver_api` is the only reader, validating a claimed `in_reply_to` -- including the source it claims -- against it before that claim ever reaches a client |
| `chat_memory.py` | `ChatMemory` -- a hot-tier, TTL-evicted turn buffer per (pod, tenant, agent, chat_id), adapted from h-nat's `h-memory` design onto `core.keys.prefix()` and a synchronous Redis client. Long-term/semantic memory (what happens after a turn's TTL elapses) is deliberately out of scope; see the module docstring |
| `chat_cycle.py` | `run_chat_cycle` -- read a chat_id's prior turns from a `ChatMemory`, dispatch the assembled prompt through any `str -> str` callable, persist both turns. Reimplements the pattern of h-nat's h-orchestrator `h_chat_cycle` natively (no NAT plugin scaffolding here); dispatcher-agnostic, no Claude-specific knowledge. `modules/claude_sdk/port.py` is the first caller |
