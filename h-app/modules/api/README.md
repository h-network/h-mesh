# api

The REST endpoint and mailbox port used by external clients. HTTP callers send
envelopes, inspect fixed tenant state providers, and poll or stream replies.
Inbound envelopes for a participant whose registry `port_type` is `api` are
drained by `deliver_api` into that participant's Redis Stream inbox.

| file | what it holds |
|---|---|
| `server.py` | `ApiSettings`, FastAPI application factory, authentication, REST routes, SSE responses, and generated REST documentation |
| `port.py` | `deliver_api` ingress handler and mailbox retention bound |
| `__main__.py` | package execution shim to the thin service launcher |

The continuously running entrypoint is `h-app/services/api.py`. Uvicorn owns
the server loop. The SSE generators are connection-lived polling responses,
not independent daemons.

The external route contract intentionally preserves the predecessor's API so
Telegram and web clients remain ordinary API consumers. Internally it uses h-mesh names:
`core.channels`, `core.envelope`, `core.keys`, and `core.registry`.
