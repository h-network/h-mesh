# office

The `office` CLI and its receiving port, together. `office` builds and sends
envelopes (hire/letGo/pause/resume, send, board, peers, etc); this module's
own port receives the hire/retire/pause/resume envelopes back and calls into
agentlifecycle to act on them. Same domain, both sides, one owner --
`agentlifecycle` lives here now, not in `lib/`, since this is its only
real caller.

Not built yet -- scaffolding only.
