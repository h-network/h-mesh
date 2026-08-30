# core

The wire format, addressing, and the switch: envelope encoding, Redis key
construction, the registry (who's on the tenant), policy (import/export
ACL), the two delivery channels (send/receive), and the switch daemon that
forwards a message by address without reading its payload.

Everything else is a module that plugs into this.

| file | what it holds |
|---|---|
| `envelope.py` | wire frame: `build`/`parse`/`encode`, header splicing (`stamp_source`, `advance_hop`), address resolution |
| `keys.py` | `prefix()` — the sole Redis key constructor, segment validation |
| `channels.py` | `send()`/`receive()`, unreplied + ack-loop tracking, `DeadLetter` |
| `queues.py` | `admit_ingress()` — shared atomic ingress-admission Lua op |
| `registry.py` | read-only registry access (`members`/`is_member`/`port_type`) |
| `policy.py` | import/export tag ACL (`allows`/`require_allowed`) |
| `service.py` | the `Switch` class — BLPOP/forward/kick loop, is what "the switch" is |
