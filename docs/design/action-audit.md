# Action audit (#27): h-mesh side

Status: approved; first slice implemented on `api-agent/action-audit`. Base: `origin/develop` 1757b16.

**Problem.** The switch logs every envelope hop; nothing records what an agent
*did* (shell/tool commands). Capture belongs to h-agent (per-CLI tool hooks,
not in this repo). h-mesh owns the store, the submit endpoint and the read side.

**Verified on develop 1757b16: no action-audit store exists.** `grep -i audit`
hits only the web console's *operator* action log (`clients/web/server.py`,
`audit.jsonl`): a file of console clicks, a different subject, kept separate.
Nearby but not it: the per-agent `activity` stream (`modules/watchdog/activity.py`,
MAXLEN ~1000) holds privacy-reduced tool *names* tailed from CLI logs, no args,
no exit status; and `core.logging` custody lines, which describe envelopes only.

## 1. Storage

- Redis stream `pod:<p>:tenant:<t>:agent:<name>:audit` (via `core.keys.prefix`,
  resource `audit`), one per agent, same Redis as the switch. Per-agent keeps
  one noisy agent from evicting another and mirrors `activity`.
- Bounded: `XADD ... MAXLEN ~ 5000` per agent (about a day of heavy use).
  The stream is a hot window, not the archive.
- Durable copy: each accepted record is also appended as one JSON line to the
  file named by `H_MESH_AUDIT_FILE` (same pattern as `core.logging.mirror`,
  never raises, unset = off). Rotation is the operator's (logrotate); h-mesh
  does not delete it. Without that file, history older than the cap is gone.
- Unlike `activity`, the stream is NOT cleared when an agent is retired
  (lifecycle already preserves `activity.offset` the same way).

## 2. Submit path

**Choice: a new authenticated REST endpoint on the existing api port,
`POST /agents/{agent}/audit`, authenticated with the existing `API_TOKEN`.
Not a new envelope kind.** Why:

- The switch is payload-blind and every envelope costs six custody lines
  (`sent...opened`) plus a destination port. An audit record has no recipient;
  one per tool call would multiply bus traffic by the agent's command rate and
  put audit data into the mesh log it is meant to be independent of.
- Envelope kinds are interpreted by destination ports (tmux/openshell
  `Command`, etc.). A new kind with no destination needs a fake one and
  invites a port to *open* it into a pane.
- The api port already has the token, per-route auth, size limits and SSE/cursor
  reads. The route is additive and ordinary: validate, redact, XADD, 202.
- Cooperation tokens get 403 on it by default (`_authorize_coop_route` is
  deny-by-default), so another office can never write or read it.
- Synchronous-and-small: h-agent hook should fire-and-forget with a short
  timeout; a failed submit must never block the agent's command. It may spool
  locally and retry (h-agent's side).

**Record** (JSON, `v: 1`, body cap 8 KiB, unknown fields rejected):

| field | rule |
|---|---|
| `agent` | from the URL; must be an enrolled agent |
| `ts` | client time (ISO-8601 UTC); server adds `recv_ts` and an id |
| `cli`, `session` | CLI name (`claude`, `codex`, ...), session id (<=64 chars) |
| `call_id` | pairs a `pre` and `post` record of one call (optional) |
| `phase` | `pre` or `post` (`pre` shows a command that never returned) |
| `tool` | tool name (`Bash`, `Edit`, ...) |
| `args` | the command / args, **redacted**, truncated to 2 KiB, `args_truncated` flag |
| `args_sha256` | digest of the redacted, canonical args (full-length, pre-truncation) |
| `exit` | integer or null; `out_bytes` size of output. **Output itself is never stored** |

**Secret redaction.** h-agent masks first (it knows the call); the server masks
again, because the server must not trust the caller. Replace with `[redacted]`:
values of `Authorization: Bearer`, `--token/--password/--secret`-style flags,
`NAME=value` where NAME matches `(TOKEN|SECRET|PASSWORD|KEY|CREDENTIAL)`, cooperation
tokens (`coop.<16hex>.<43>`), and well-known key shapes (`sk-...`, `ghp_...`,
`xox?-...`, PEM blocks). The digest is over the redacted text so it cannot be
used to brute-force a low-entropy secret. Pattern redaction is best-effort:
a secret typed as a bare positional argument is not recognizable. State that in
the docs; do not claim otherwise.

## 3. Read surface

- `h-mesh-office audit [--agent X] [--since 2h|ISO] [--tool T] [--limit N] [--json]`:
  reads the streams directly (same as other office commands), merges agents by
  `recv_ts`, prints one line per record (`ts agent tool exit args`).
- `GET /agents/{agent}/audit?after=<cursor>&limit=` with the existing cursor
  convention of `/activity`, `API_TOKEN` only, for the console later.
- Reads are not access-controlled beyond the token: any agent can read Redis.

## 4. What this cannot guarantee (shared OS user)

All agents run as one OS user with the same Redis and the same `API_TOKEN`.
Therefore, plainly:

- An agent can **forge** a record for any agent (it has the token and Redis).
  `agent` and `session` are asserted, not proven.
- An agent can **skip** auditing: unset/edit its CLI hook config, run commands
  outside the hooked tool, or use a CLI without hooks.
- An agent can **delete or rewrite** the stream and the mirror file.
- Capture covers hooked CLI tool calls only; operator terminal work is covered
  only if h-agent also wraps that shell.
- `ts` is client-supplied; only `recv_ts` is the server's.

So this is an accountability and diagnostics log for honest mistakes and
reconstruction, **not tamper-evident evidence**. Real guarantees need the
deferred separation: a writer under a different OS user (unix socket, append-only
file owned by it), then a per-agent hash chain so deletion is detectable. Out of
scope now; the record format (`v`, `call_id`) leaves room for `prev_hash`.

## 5. Minimal first slice (useful alone)

1. `lib/audit.py`: validate, redact, XADD (+ optional file mirror).
2. `POST /agents/{agent}/audit` and `GET /agents/{agent}/audit` in `modules/api`.
3. `h-mesh-office audit [--agent] [--since]` (read only).
4. Tests (redaction table, bounds, auth, coop 403, merge/ordering) and docs in
   `modules/api/README.md` + `modules/office/README.md`.

Useful with no h-agent change: `curl`/a ten-line Claude Code `PostToolUse` hook
already feeds it. h-agent's hook installer is the follow-up on its side.
Not in the slice: hash chain, separate writer user, output capture, console panel.

## Decisions (lead, approved)

- Opt-in `H_MESH_AUDIT_FILE` mirror: yes. Caps 5000 entries/agent and 8 KiB/record: yes.
- CLI read is not an access boundary with one shared OS user and is not labelled
  as security; the HTTP read needs `API_TOKEN`.
- Server-side re-masking stays; the "cannot guarantee" section is copied
  verbatim into `modules/api/README.md`; `prev_hash` is reserved in the format.
