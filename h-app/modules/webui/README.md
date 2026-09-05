# webui module

A relay port_type for live-streaming another agent's progress to a connected
browser tab. Registered directly in the tenant registry (`port_type`
`webui`), the same way `claude_sdk` is -- no `office hire` path for either
one yet.

| file | what it holds |
|---|---|
| `port.py` | `deliver_webui` -- drains one agent's ingress, relaying every `Progress`/`Message` envelope it receives onto that agent's own "inbox" Redis Stream (same key/shape `modules/api/port.py`'s `deliver_api` writes) |
| `routes.py` | `register_webui_routes` -- the served HTML page and the two read endpoints (JSON poll, SSE), mounted onto the already-running `api` service |

## No separate daemon

Unlike `modules/api`/`modules/session`, this module ships no
`services/webui.py` and is not in `services/daemons.py`'s daemon tables.
`services/daemons.py`'s own module docstring documents watchdog and session
both shipping once as a console script nothing in the documented start path
ever actually invoked -- a new, always-forgettable daemon is a known,
previously-real risk here, not a hypothetical one. Mounting the browser-facing
routes onto the api service that's already running (already wizard-enabled,
already TLS/bind-aware, already daemon-lifecycle-managed) sidesteps that whole
class of risk; `deliver_webui` itself is delivered as a one-shot subprocess
per kick like every other port_type, same as `claude_sdk`.

## Registering a webui agent

```
HSET pod:<pod>:tenant:<tenant>:registry <agent-name> webui
```

No credentials, profile, or options resource -- this port has nothing to
configure. Once registered, `office send -a <agent-name> --context ...` isn't
relevant here (no memory); a `claude_sdk` agent's `live_to` (see
`modules/claude_sdk/README.md`) is what actually sends this agent anything.

## Browser access

Once Telegram (and therefore the `api` service) is configured, open:

```
https://<host>:<api-port>/agents/<agent-name>/live
```

The served page needs the same `API_TOKEN` every other api route requires,
entered into a field on the page itself -- it does **not** use `EventSource`
(which cannot set an `Authorization` header); its own JS opens the live
stream with `fetch()` and parses the `text/event-stream` framing by hand
instead, so the token-only auth boundary the rest of the api service
already has stays exactly as-is, not loosened for this one page.

`GET /agents/{agent}/live/events` (cursor catch-up, `?after=<cursor>`) and
`GET /agents/{agent}/live/stream` (SSE, same cursor semantics) are the two
underlying data routes the page itself uses; either is also a normal
Bearer-token REST/SSE endpoint for any other client. Both 404 for an unknown
agent or one whose `port_type` isn't `webui`. See `modules/api/README.md`.

## Unfamiliar kinds

Any envelope kind other than `Progress`/`Message` dead-letters cleanly via
`core.channels`'s own "unknown kind" handling -- nothing webui-specific to
build for that. `tools/smoke_webui.py` proves the reverse direction too: a
`Progress` envelope sent to a `claude_sdk` agent (which has no `Progress`
opener) dead-letters the same way, never crashing the delivery subprocess.
