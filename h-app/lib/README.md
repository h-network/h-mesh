# lib

Shared code that's imported *between* modules, but doesn't belong in `core/`
(it's not wire format or switch mechanics) and doesn't own a port the way
everything in `modules/` does. If several modules need the same logic and
none of them should own it exclusively, it goes here.

| directory | what it holds |
|---|---|
| `agentlifecycle/` | start/stop/pause/resume desired-state logic for a participant, callback-driven, no port of its own |
