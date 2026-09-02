# OpenShell module

This module provides the NVIDIA OpenShell SDK wrapper and delivery port for
agents whose `port_type` is `openshell`. A delivery starts a fresh headless CLI
process inside the agent's existing sandbox and waits for it to exit. Claude,
Codex, and agy resume their most recent on-disk session to preserve continuity.

Run one delivery kick with `h-app` on `PYTHONPATH`:

```sh
python -m modules.openshell.port AGENT
```

The runner reads `POD`, `TENANT`, and optionally `REDIS_URL`, then uses the
shared delivery lock before consuming one ingress envelope. The runtime needs
`openshell>=0.0.116,<1` and `redis`.

## Gateway configuration

Set `OPENSHELL_GATEWAY_ENDPOINT`. Optional TLS and authentication variables
are `OPENSHELL_GATEWAY_TLS_CA`, `OPENSHELL_GATEWAY_TLS_CERT`,
`OPENSHELL_GATEWAY_TLS_KEY`, and `OPENSHELL_GATEWAY_BEARER_TOKEN`.

CLI credentials are profile-scoped. The default profile suffix is `DEFAULT`;
hyphens in profile names become underscores and the suffix is uppercased.

- Claude reads `CLAUDE_OAUTH_TOKEN_SUFFIX`; the value is passed only as
  `CLAUDE_CODE_OAUTH_TOKEN` for the one headless exec.
- Codex reads `CODEX_AUTH_JSON_SUFFIX` and agy reads `AGY_AUTH_JSON_SUFFIX`.
  Their credential file is written immediately before the CLI exec and wiped
  from the sandbox in a `finally` block immediately afterward.

Attachments are validated with the shared schema, written under
`/sandbox/attachments/STREAM_ID` through a temporary file and atomic rename,
then announced to the CLI. Non-empty CLI output is sent back to the envelope's
source as a correlated `Message`.

That reply also carries `in_reply_to` automatically, set directly from the
envelope's own `stream_id` -- exact by construction, not opt-in like
`office send --reply-to`, because there is no interactive agent here to
cooperate or fail to: `_reply()` runs synchronously, in the same call that
received the envelope, for Message, Command, and Attachment alike. Delivery
is still recorded via `lib/reply_correlation.py`'s `record_delivered()`
before the reply is sent, so `modules/api/port.py`'s `deliver_api` validates
this path the same as tmux's opt-in one -- an automatic claim gets no less
scrutiny than a manual one.
