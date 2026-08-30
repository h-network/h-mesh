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
| `config.py` | local state directory resolution (`H_MESH_STATE_DIR`, default `~/.h-mesh`) |
| `channels.py` | `send()`/`receive()`, unreplied tracking, `DeadLetter` |
| `queues.py` | `admit_ingress()` — shared atomic ingress-admission Lua op |
| `registry.py` | read-only registry access (`members`/`is_member`/`port_type`) |
| `policy.py` | import/export tag ACL (`allows`/`require_allowed`) |
| `logging.py` | contract-shaped JSON event logging and durable stdout mirroring |
| `retention.py` | count-based trimming for completed-task and dead-letter queues |
| `windowlog.py` | agent-window log tailing, offset tracking, and bounded truncation |
| `service.py` | the `Switch` class — BLPOP/forward/kick loop, is what "the switch" is |

The tenant participant hash is the wire-visible `registry` resource. Delivery
itself remains outside core: pass a `kick(agent, envelope)` callback to
`Switch` when an edge module should be notified after ingress admission. With
no callback, forwarding still completes and core records `kick_deferred`.

Core logging uses the `H_MESH_WRITER`, `H_MESH_CUSTODY_FILE`,
`H_MESH_LOG_FILE`, `H_MESH_LOG_QUIET`, and
`H_MESH_LOG_FILE_AGENT_ONLY` environment variables. Switch registry refresh is
configured with `REGISTRY_POLL_SECONDS`.
