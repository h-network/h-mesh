# h-mesh

An agentic CLI framework where agents address each other directly, by name, and a single switch forwards every message to its destination. Work is tracked on a shared board per agent: pull a ticket, work it, mark it done.

The design borrows its layering from networking, not just its name -- a bus layer that only knows envelopes and queues, a switch layer that forwards by address without reading payloads, and an edge layer where each agent's actual delivery -- a terminal, a mailbox, or another destination-specific target -- lives. Same separation of concerns Ethernet uses between physical transport, switching, and the endpoint.

Every endpoint is a module, and every module owns its own port -- the piece that assembles an outgoing envelope and disassembles an incoming one into that endpoint's specific action.

A Telegram interface is included, talking to the framework over the same REST API any external client would use -- a separate module, not a special case.

Every hop a message takes is logged, the same way a real packet's path can be traced -- nothing moves silently.
