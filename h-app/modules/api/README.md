# api

The REST endpoint and mailbox port used by external clients. HTTP callers send
envelopes, inspect fixed tenant state providers, and poll or stream replies.
Inbound envelopes for a participant whose registry `port_type` is `api` are
drained by `deliver_api` into that participant's Redis Stream inbox.

| file | what it holds |
|---|---|
| `server.py` | `ApiSettings`, FastAPI application factory, authentication, REST routes, SSE responses, and generated REST documentation |
| `port.py` | `deliver_api` ingress handler, mailbox retention bound, and the `python -m modules.api.port AGENT` delivery entrypoint the switch spawns per kick |
| `__main__.py` | package execution shim to the thin service launcher |

The continuously running entrypoint is `h-app/services/api.py`. Uvicorn owns
the server loop. The SSE generators are connection-lived polling responses,
not independent daemons.

Delivery itself is a separate, one-shot subprocess per kick, same shape as
every other port module: `python -m modules.api.port AGENT` reads `POD`,
`TENANT`, and optionally `REDIS_URL`, takes the shared delivery lock, checks
the pause marker, and calls `deliver_api` once. This is what
`core.service.transmission` actually spawns for any destination whose
registry `port_type` is `api` -- if this file has no `main()`/`__main__`
guard, that subprocess runs, does nothing, and exits 0, so every api-bound
envelope (Telegram replies included) sits in ingress forever with no error
anywhere. `tests/test_api.py`'s `RealApiPortSubprocessTests` runs this
exact subprocess against a real Redis to catch that class of regression --
a unit test of `deliver_api()` alone, or a mock asserting the right argv was
Popen'd, cannot.

The external route contract intentionally preserves the predecessor's API so
Telegram and web clients remain ordinary API consumers. Internally it uses h-mesh names:
`core.channels`, `core.envelope`, `core.keys`, and `core.registry`.
