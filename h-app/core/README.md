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
| `channels.py` | `send()`/`receive()`, unreplied tracking, `DeadLetter`, and best-effort failure feedback to tmux senders |
| `queues.py` | `admit_ingress()` — shared atomic ingress-admission Lua op |
| `registry.py` | read-only registry access (`members`/`is_member`/`port_type`) |
| `policy.py` | import/export tag ACL (`allows`/`require_allowed`) |
| `dispatch.py` | generic port_type -> handler registry and dispatch, invoked only by whatever the switch's kick actually calls -- the switch itself never imports this |
| `logging.py` | contract-shaped JSON event logging and durable stdout mirroring, plus `configure_logging()` — the stdlib logging threshold for entry points |
| `retention.py` | count-based trimming for completed-task and dead-letter queues |
| `windowlog.py` | agent-window log tailing, offset tracking, and bounded truncation |
| `service.py` | the `Switch` class — BLPOP/forward/kick loop, is what "the switch" is |

The tenant participant hash is the wire-visible `registry` resource. Delivery
itself remains outside core: pass a `kick(agent, port_type, envelope)` callback to
`Switch` when an edge module should be notified after ingress admission. With
no callback, forwarding still completes and core records `kick_deferred`.
The production switch uses `transmission` to start
`python -m modules.<port_type>.port <agent>`; `port_type` is currently assumed
to equal its module directory name, and the child reads the frame from ingress.
Broadcast kicks remain deferred until their membership-only path also resolves
participant types.

Core logging uses the `H_MESH_WRITER`, `H_MESH_CUSTODY_FILE`,
`H_MESH_LOG_FILE`, `H_MESH_LOG_QUIET`, and
`H_MESH_LOG_FILE_AGENT_ONLY` environment variables. Switch registry refresh is
configured with `REGISTRY_POLL_SECONDS`.

## Two logging systems, one file

`logging.py` holds both, and they are not interchangeable:

- **Custody records** (`log_record`/`emit`/`mirror`) — one JSON object per
  line on **stdout**, durably mirrored, parsed by conservation checks. The
  variables above configure these. No level, no threshold: a record is either
  written or it is a hole in the evidence.
- **Diagnostics** (`logging.getLogger(...)`, e.g. `dispatch.py`'s two
  `logger.error` calls when a registered handler no longer resolves) — human
  prose on **stderr**, with a threshold set by `H_MESH_LOG_LEVEL`.

`H_MESH_LOG_LEVEL` (default `INFO`) takes level **names** only — `DEBUG`,
`INFO`, `WARNING`, `ERROR`, `CRITICAL`, with `WARN`/`FATAL` as the stdlib
aliases — case- and whitespace-insensitive. A number is not a name:
`H_MESH_LOG_LEVEL=10` is an unrecognised value, not DEBUG. Anything
unrecognised falls back to `INFO` and logs a `WARNING` saying so, rather than
failing a delivery process over a typo in a tenant's env. It is the same knob,
spelled the same way, as `clients/telegram/README.md` documents for the
Telegram client — that client keeps its own copy of the resolver because it
imports nothing from `core`, so the two move together or not at all.

⚠ **`configure_logging()` belongs in an entry point, never at import of a
library module.** Each port's `main()` calls it (`modules/{api,office,
openshell,tmux}/port.py`), which covers both `python -m modules.<type>.port`
as the switch spawns it and `h-mesh <type>-port`, which imports the module and
calls the same `main()`. `core/dispatch.py` deliberately does not configure
anything: it is imported by processes core does not own, including the test
suite, and a library that sets the root logger's level decides verbosity for
all of them. Without a configuring entry point, stdlib logging's lastResort
handler prints WARNING and above as a bare message — no timestamp, no logger
name — and silently drops everything below it.

The threshold is the verbosity knob and nothing else: a real failure keeps its
own severity. Note that `basicConfig` sets the **root** level, so `DEBUG` also
turns up third-party libraries (redis included) in that process.
The forwarding loop survives transient Redis connection failures: it records
the failure, waits for its current poll interval, and lets redis-py reconnect on
the next forwarding attempt.
