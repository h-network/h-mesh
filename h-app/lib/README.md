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
| `attachment_schema.py` | attachment wire/schema limits (size, mime type, base64 validation), shared by any port that delivers attachments |
