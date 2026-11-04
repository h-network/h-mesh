# Inter-Office Communication Guide

This guide walks a new operator through setting up, pairing, verifying, and troubleshooting inter-office communication between two `h-mesh` offices on a clean machine.

Every command in this guide uses the supported operator workflow (`./container/bootstrap.sh` and `h-mesh-office`) and has been verified end-to-end on fresh containerized offices over both HTTPS (with a private CA) and plaintext lab HTTP.

---

## 1. How Inter-Office Communication Works

Each `h-mesh` office is an isolated pod/tenant with its own Redis bus, switch, agent panes, REST API door (`services.api`), and reply relay (`services.interoffice_relay`).

1. **Cooperation Grant (`coop-grant`):** The receiving office's lead grants a scoped bearer token (`coop.<16-hex-id>.<43-char-secret>`) bound to one local `api`-type participant name (for example, `officex-peer`) and restricted to `Message` envelopes. Only the SHA-256 hash of the secret is stored in the receiving office's Redis.
2. **Address Book Registration (`office-add`):** The sending office's lead registers the remote office's base URL and pipes the cooperation token via `--token-stdin`. Before saving anything, `office-add` calls `GET <url>/whoami` with the token to verify reachability, TLS trust, token validity, API version, and the bound participant name. It also enrols the local `relay` (`api`) participant if not already present.
3. **Direct Send (`send --office`):** Any agent in the sending office can run `h-mesh-office send --office <office> -a <remote-agent> "..."`. The CLI `POST`s the message directly to `<url>/agents/<remote-agent>/envelopes` as the granted participant and records a 24-hour reply-routing receipt (`xsent`) keyed by the returned `stream_id` and the sender's incarnation.
4. **Inbound Reply Relay (`services.interoffice_relay`):** When the remote agent replies to `<participant>` with `--reply-to <stream_id>`, the reply lands in `<participant>`'s mailbox on the remote office. The sending office's `interoffice_relay` daemon streams `GET <url>/agents/<participant>/messages/stream`, matches `in_reply_to` against the local `xsent` receipt, preserves the `correlation_id`, and delivers the reply into the original sender's tmux pane with a cross-office reply hint:
   ```text
   [message from relay] [from <office>/<agent>] <reply text>
   [reply to <office>/<agent>: h-mesh-office send --office <office> -a <agent> "..."]
   ```
   Neither incoming cooperation messages nor relayed replies open client "unanswered message" watchdog timers.

> **Transport security default:** HTTPS (`https://`) with certificate and hostname verification is the default for inter-office communication. Plaintext `http://` to a non-loopback host is refused unless both the receiving office has opted into plaintext (`H_MESH_ALLOW_PLAINTEXT=1` or the interactive plaintext prompt) and the sending office passes `--allow-plaintext` to `office-add`.

---

## 2. Prerequisites

On a clean Linux host, ensure you have:

- **Docker Engine** with the **Docker Compose** plugin (`docker compose version`)
- **Git** (`git --version`) and **OpenSSL** (`openssl version`, used by `container/bootstrap.sh` to validate and stage host TLS certificates before starting containers)
- **Host user UID `1000` or `root`** when staging TLS certificates with `container/bootstrap.sh` (staged private keys in `offices/<pod>/<tenant>/tls/` are mode `0600` owned by container UID `1000`)
- **Network reachability** between the two offices' published API ports (for example, `29280` and `29282` on the host IP `172.16.0.201`)
- **An LLM credential** for the agents: either a Claude OAuth token entered when `bootstrap.sh` prompts for it, or a local OpenAI-compatible model endpoint configured via `PROVIDER_LOCAL_*` in `offices/<pod>/<tenant>/.env`

Clone the repository and enter the checkout:

```bash
umask 022
git clone https://github.com/h-network/h-mesh.git
cd h-mesh
```

---

## 3. Fresh Office Setup (`container/bootstrap.sh`)

You can run multiple offices on the same host as long as each office has a distinct `POD`/`TENANT` pair and distinct published host ports (`API_PORT` and `SESSION_PORT`). `container/bootstrap.sh` isolates every office under `offices/<pod>/<tenant>/` and runs Compose under the project name `h-mesh-<pod>-<tenant>`.

Below are the two supported transport setups for a pair of fresh offices: **Section 3.1 (HTTPS with a private CA, recommended)** and **Section 3.2 (Plaintext HTTP for an isolated lab testbed)**.

### 3.1 Option A: HTTPS Setup with a Private CA (Recommended Default)

`container/bootstrap.sh` never generates self-signed certificates behind your back; it takes existing **host** paths to your PEM certificate, unencrypted PEM private key, and optional peer CA bundle, validates them with `openssl` before starting Docker Compose, copies them with mode `0600` into `offices/<pod>/<tenant>/tls/`, and mounts that directory read-only at `/run/h-mesh/tls` inside the container (`container/compose.tls.yaml`).

#### Step 1: Generate a private CA and server certificates (if you do not already have them)

Every server certificate must include a `subjectAltName` (`IP:` or `DNS:`) matching the host/IP that remote offices will connect to in the `https://` URL:

```bash
mkdir -p /tmp/h-mesh-certs
 chmod 700 /tmp/h-mesh-certs

# 1. Generate the private CA key and certificate
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /tmp/h-mesh-certs/ca.key \
  -out /tmp/h-mesh-certs/ca.crt \
  -days 30 -subj "/CN=h-mesh-private-ca"

# 2. Generate server key + certificate for Office X (replace 172.16.0.201 with your host IP or DNS name)
openssl req -newkey rsa:2048 -nodes \
  -keyout /tmp/h-mesh-certs/officex.key \
  -out /tmp/h-mesh-certs/officex.csr \
  -subj "/CN=172.16.0.201"
printf 'subjectAltName=IP:172.16.0.201,DNS:localhost\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' \
  > /tmp/h-mesh-certs/san.ext
openssl x509 -req -in /tmp/h-mesh-certs/officex.csr \
  -CA /tmp/h-mesh-certs/ca.crt -CAkey /tmp/h-mesh-certs/ca.key -CAcreateserial \
  -out /tmp/h-mesh-certs/officex.crt -days 30 -extfile /tmp/h-mesh-certs/san.ext

# 3. Generate server key + certificate for Office Y
openssl req -newkey rsa:2048 -nodes \
  -keyout /tmp/h-mesh-certs/officey.key \
  -out /tmp/h-mesh-certs/officey.csr \
  -subj "/CN=172.16.0.201"
openssl x509 -req -in /tmp/h-mesh-certs/officey.csr \
  -CA /tmp/h-mesh-certs/ca.crt -CAkey /tmp/h-mesh-certs/ca.key -CAcreateserial \
  -out /tmp/h-mesh-certs/officey.crt -days 30 -extfile /tmp/h-mesh-certs/san.ext
```

#### Step 2: Bootstrap both TLS offices

Run `./container/bootstrap.sh` interactively (or `./setup.sh --container`) and supply the **host** file paths when prompted for the TLS certificate, TLS key, and optional peer CA bundle:

```text
  TLS certificate HOST path (blank to explicitly accept plaintext): /tmp/h-mesh-certs/officex.crt
  TLS key HOST path: /tmp/h-mesh-certs/officex.key
  Optional peer CA bundle HOST path (blank for system trust): /tmp/h-mesh-certs/ca.crt
```

Or run non-interactively with environment variables:

```bash
# Office X (rctlsx/lab on host ports 29280/29281)
API_TLS_CERT=/tmp/h-mesh-certs/officex.crt \
API_TLS_KEY=/tmp/h-mesh-certs/officex.key \
H_MESH_CA_CERT=/tmp/h-mesh-certs/ca.crt \
AGENTS=architect,worker \
  ./container/bootstrap.sh --pod rctlsx --tenant lab --api-port 29280 --session-port 29281 --non-interactive

# Office Y (rctlsy/lab on host ports 29282/29283, reusing the built image)
API_TLS_CERT=/tmp/h-mesh-certs/officey.crt \
API_TLS_KEY=/tmp/h-mesh-certs/officey.key \
H_MESH_CA_CERT=/tmp/h-mesh-certs/ca.crt \
AGENTS=architect,worker \
  ./container/bootstrap.sh --pod rctlsy --tenant lab --api-port 29282 --session-port 29283 --skip-build --non-interactive
```

What `bootstrap.sh` stages automatically:

- Validates the host certificate, private key, key match, and CA bundle using `openssl` before starting Compose.
- Copies the files into `offices/<pod>/<tenant>/tls/{api.crt,api.key,ca.crt}` (`0700` directory, `0600` files) and mounts them read-only into the container at `/run/h-mesh/tls`.
- Writes `API_TLS_CERT=/run/h-mesh/tls/api.crt`, `API_TLS_KEY=/run/h-mesh/tls/api.key`, and `H_MESH_CA_CERT=/run/h-mesh/tls/ca.crt` into `offices/<pod>/<tenant>/.env` and persists them into `~/.h-mesh/<tenant>/env` inside the state volume.
- Both `services.interoffice_relay` and `h-mesh-office` automatically use `H_MESH_CA_CERT` (`/run/h-mesh/tls/ca.crt`) as the default CA trust store unless overridden per office with `office-add --ca-cert PATH`.

Verify that both HTTPS API doors are listening and require authentication (`HTTP 401`):

```bash
curl -sS --cacert /tmp/h-mesh-certs/ca.crt -o /dev/null -w "HTTP %{http_code}\n" https://172.16.0.201:29280/agents
curl -sS --cacert /tmp/h-mesh-certs/ca.crt -o /dev/null -w "HTTP %{http_code}\n" https://172.16.0.201:29282/agents
```

Expected output:

```text
HTTP 401
HTTP 401
```

### 3.2 Option B: Plaintext HTTP Setup (Lab Testbeds Only)

For an isolated lab testbed without TLS certificates:

- **Interactively:** Run `./container/bootstrap.sh`, leave `TLS certificate HOST path (blank to explicitly accept plaintext):` blank, and answer `y` at `Accept plaintext for this container? [y/N]:`.
- **Non-interactively:** Export `H_MESH_ALLOW_PLAINTEXT=1`:

```bash
# Office X (rcgatex/lab on host ports 29280/29281)
H_MESH_ALLOW_PLAINTEXT=1 AGENTS=architect,worker \
  ./container/bootstrap.sh --pod rcgatex --tenant lab --api-port 29280 --session-port 29281 --non-interactive

# Office Y (rcgatey/lab on host ports 29282/29283, reusing the built image)
H_MESH_ALLOW_PLAINTEXT=1 AGENTS=architect,worker \
  ./container/bootstrap.sh --pod rcgatey --tenant lab --api-port 29282 --session-port 29283 --skip-build --non-interactive
```

> **Why a non-interactive office without TLS or `H_MESH_ALLOW_PLAINTEXT=1` is unreachable from outside:** If neither `API_TLS_CERT`/`API_TLS_KEY` nor `H_MESH_ALLOW_PLAINTEXT=1` is provided during a `--non-interactive` bootstrap, the container still boots, but the entrypoint binds `services.api` to container loopback (`127.0.0.1:8080`). The published host port remains closed until you rerun `bootstrap.sh` with TLS paths or `H_MESH_ALLOW_PLAINTEXT=1`.

### 3.3 Attaching to Your Offices

Each bootstrapped office writes an executable `attach.sh` and a `SUMMARY.md` (with zero secret values) under `offices/<pod>/<tenant>/`:

```bash
./offices/rctlsx/lab/attach.sh
# or:
./container/bootstrap.sh --pod rctlsx --tenant lab --attach
```

Inside the tmux session, window `0` (`architect`) is the office lead. All pairing commands (`coop-grant`, `coop-revoke`, `office-add`, `office-rm`) are **lead-only** and must be run by the lead agent (`architect`).

---

## 4. Pairing Two Offices (`coop-grant` and `office-add`)

### The Participant-Name Rule (Read First)

When Office Y grants a cooperation token to Office X with `h-mesh-office coop-grant <participant>`:

- `<participant>` (for example, `rctlsx-peer` or `rcgatex-peer`) is the **local identity inside Office Y** that Office X will speak as.
- Office Y automatically enrols `<participant>` as an `api` interface participant (visible in `h-mesh-office peers -i`, not a tmux colleague) and marks it as an inter-office sender.
- When Office X sends a message to an agent in Office Y, that agent sees `[message from <participant>] ...`.
- When Office X runs `h-mesh-office office-add <name> <url> --token-stdin`, the participant name in Office X's address book **must match** the `<participant>` name that Office Y bound to that token.
- `office-add` automatically verifies the token and participant name against `GET <url>/whoami` before saving anything:
  - If you **omit `--participant`**, `office-add` queries `/whoami` and fills in the exact granted participant name automatically.
  - If you **pass `--participant <name>`**, `office-add` confirms that the remote office reports the same participant name.
  - If you **pass the wrong `--participant` name**, `office-add` refuses immediately (`nothing saved`) and exits `1` without writing to Redis.

### 4.1 Step 1: On Office Y (as Lead), Grant a Token for Office X

In Office Y's `architect` pane, grant a cooperation token for the participant identity Office X will use (`rctlsx-peer`):

```bash
h-mesh-office coop-grant rctlsx-peer
```

Output (`stdout` plus a one-time notice on `stderr`):

```text
participant: rctlsx-peer
token-id: <16-hex-token-id>
token: coop.<16-hex-token-id>.<43-char-secret>
This token is shown once. Give it to the other office's lead (office-add ... --token-stdin); revoke with: office coop-revoke <16-hex-token-id>
```

Verify the grant metadata (`coops` never prints the secret) and confirm `rctlsx-peer` appears under `interfaces` in `peers -i`:

```bash
h-mesh-office coops
h-mesh-office peers -i
```

Output:

```text
<16-hex-token-id>	rctlsx-peer	Message	active	<unix-timestamp>
worker
interfaces (api/office -- recognized, not tmux colleagues): api (api), host (office), rctlsx-peer (api)
```

### 4.2 Step 2: On Office X (as Lead), Add Office Y with `office-add`

In Office X's `architect` pane, pipe the token into `h-mesh-office office-add` using `--token-stdin` (tokens are never accepted on the command line where they would appear in `ps` or shell history).

#### What happens if you pass the wrong `--participant` name

If you accidentally pass `--participant wrong-peer` instead of `rctlsx-peer`, `office-add` probes `GET /whoami` and refuses before saving anything:

```bash
printf '%s\n' 'coop.<16-hex-token-id>.<43-char-secret>' | \
  h-mesh-office office-add rctlsy https://172.16.0.201:29282 \
    --participant wrong-peer \
    --ca-cert /run/h-mesh/tls/ca.crt \
    --token-stdin
```

Output (exit code `1`):

```text
office: error: nothing saved: the remote office says this token belongs to participant 'rctlsx-peer', but the address book has 'wrong-peer'; use --participant rctlsx-peer
```

Running `h-mesh-office offices` confirms the address book is still empty (`no offices configured`).

#### Registering with the correct participant name (HTTPS)

Pass `--participant rctlsx-peer` (or omit `--participant` to let `/whoami` populate it) along with `--ca-cert /run/h-mesh/tls/ca.crt` (or omit `--ca-cert` when `H_MESH_CA_CERT=/run/h-mesh/tls/ca.crt` was staged at bootstrap):

```bash
printf '%s\n' 'coop.<16-hex-token-id>.<43-char-secret>' | \
  h-mesh-office office-add rctlsy https://172.16.0.201:29282 \
    --participant rctlsx-peer \
    --ca-cert /run/h-mesh/tls/ca.crt \
    --token-stdin
```

Output:

```text
added office rctlsy -> https://172.16.0.201:29282 (as rctlsx-peer); verified with the remote office (rctlsy/lab)
```

#### Registering on a plaintext HTTP lab testbed

If the two offices were bootstrapped with `H_MESH_ALLOW_PLAINTEXT=1` (Section 3.2), use `http://` and add `--allow-plaintext`:

```bash
printf '%s\n' 'coop.<16-hex-token-id>.<43-char-secret>' | \
  h-mesh-office office-add rcgatey http://172.16.0.201:29282 \
    --participant rcgatex-peer \
    --token-stdin \
    --allow-plaintext
```

Output:

```text
office: warning: http://172.16.0.201:29282 is plaintext http; the token travels in the clear
added office rcgatey -> http://172.16.0.201:29282 (as rcgatex-peer); verified with the remote office (rcgatey/lab)
```

### 4.3 Step 3: Verify the Address Book and Link Health (`offices --check`)

Still in Office X, inspect the address book, probe the live link via `offices --check`, and confirm the local `relay (api)` participant was automatically enrolled:

```bash
h-mesh-office offices
h-mesh-office offices --check
h-mesh-office peers -i
```

Output (HTTPS):

```text
rctlsy	https://172.16.0.201:29282	as rctlsx-peer	added by architect	CA /run/h-mesh/tls/ca.crt
rctlsy	https://172.16.0.201:29282	as rctlsx-peer	added by architect	CA /run/h-mesh/tls/ca.crt	link: ok
worker
interfaces (api/office -- recognized, not tmux colleagues): api (api), host (office), relay (api)
```

### 4.4 Step 4: Pair the Reverse Direction (Office X -> Office Y)

Cooperation tokens are directional: Office Y's grant allows Office X to send to Office Y and read replies addressed to `rctlsx-peer`. So that agents in Office Y can also initiate messages to Office X, repeat the grant in the opposite direction:

1. In **Office X** (as `architect`):
   ```bash
   h-mesh-office coop-grant rctlsy-peer
   ```
2. In **Office Y** (as `architect`):
   ```bash
   printf '%s\n' 'coop.<16-hex-token-id>.<43-char-secret>' | \
     h-mesh-office office-add rctlsx https://172.16.0.201:29280 \
       --participant rctlsy-peer \
       --ca-cert /run/h-mesh/tls/ca.crt \
       --token-stdin
   h-mesh-office offices --check
   ```

---

## 5. Sending and Replying Across Offices

### 5.1 Sending a Message (`send --office`)

From any agent in Office X (for example, `architect`), send a message to `worker` in Office Y:

```bash
h-mesh-office send --office rctlsy -a worker \
  "Hello from rctlsx architect! Please reply using the --reply-to command shown in your reply hint."
```

Output (exit code `0`):

```text
sent to rctlsy/worker: 96 bytes (6b71d5cf1c7b455a852b87a07814e0aa)
```

For multi-line or long messages, pass the body on standard input or from a file so the shell never parses it:

```bash
h-mesh-office send --office rctlsy -a worker --stdin <<'EOF'
Multi-line message body
from Office X to Office Y.
EOF
```

#### Exit codes for `h-mesh-office send --office`

| Exit code | Meaning | Safe to retry? |
|---|---|---|
| `0` | Accepted by the remote office (`HTTP 202` with a valid `stream_id`). | Completed |
| `1` | Refused or failed before any application byte was sent (bad arguments, unknown office, connection refused, DNS failure, TLS verification/handshake failure, or `HTTP 4xx`). The message was **NOT** sent. | Yes, after fixing the reported error |
| `2` | CLI flag/syntax error (`argparse`). | Fix syntax |
| `3` | Outcome unknown: the connection broke mid-flight after the TLS handshake, timed out, returned `HTTP 5xx` or `3xx`, or returned a malformed `2xx` body. The message **may** have been accepted. | Check with the remote office before retrying |

### 5.2 Receiving and Replying in the Remote Office (`--reply-to`)

In Office Y's `worker` tmux pane, the message arrives from `rctlsx-peer` with a `--reply-to <stream_id>` hint:

```text
[message from rctlsx-peer] Hello from rctlsx architect! Please reply using the --reply-to command shown in your reply hint.
[reply to rctlsx-peer: h-mesh-office send -a rctlsx-peer --reply-to 6b71d5cf1c7b455a852b87a07814e0aa "..."]
```

To reply back to the sender in Office X, `worker` runs the command from the hint line:

```bash
h-mesh-office send -a rctlsx-peer --reply-to 6b71d5cf1c7b455a852b87a07814e0aa \
  "ACK from rctlsy worker — reply correlated and delivered."
```

Output in `rctlsy/worker`:

```text
sent to rctlsx-peer: 58 bytes (924e733b7192488e9b71c6ef2cb02f95)
```

### 5.3 Receiving the Relayed Reply in the Sending Office

`services.interoffice_relay` in Office X streams `GET https://172.16.0.201:29282/agents/rctlsx-peer/messages/stream`, matches `in_reply_to` (`6b71d5cf1c7b455a852b87a07814e0aa`) against the local `xsent` receipt, verifies the sender incarnation, preserves the `correlation_id`, and delivers the reply to `architect`'s tmux pane:

```text
[message from relay] [from rctlsy/worker] ACK from rctlsy worker — reply correlated and delivered.
[reply to rctlsy/worker: h-mesh-office send --office rctlsy -a worker "..."]
```

Notice that the reply hint on a relayed message automatically points across offices (`h-mesh-office send --office rctlsy -a worker "..."`), never at the local inbound-only `relay` participant.

If a remote agent sends a message to `rctlsx-peer` without `--reply-to` (or after the 24-hour `xsent` receipt has expired, or after the original sender agent has been re-hired under a new incarnation), `services.interoffice_relay` routes the message to Office X's `lead` (`architect`).

---

## 6. Revoking Cooperation Tokens and Removing Offices

### 6.1 Revoking a Token on the Granting Office (`coop-revoke`)

To cut off a remote office's access immediately, run `h-mesh-office coop-revoke <token-id>` as the lead in the office that issued the token (use `h-mesh-office coops` to list token IDs):

```bash
h-mesh-office coops
h-mesh-office coop-revoke <16-hex-token-id>
h-mesh-office coops
```

Output:

```text
<16-hex-token-id>	rctlsx-peer	Message	active	<unix-timestamp>
revoked <16-hex-token-id>
<16-hex-token-id>	rctlsx-peer	Message	revoked	<unix-timestamp>
```

Revocation takes effect immediately on the next request:

- On the peer office, `h-mesh-office offices --check` reports the revoked token and exits `1`:
  ```text
  rctlsy	https://172.16.0.201:29282	as rctlsx-peer	added by architect	CA /run/h-mesh/tls/ca.crt	link: token problem -- the cooperation token was revoked by the remote office; ask its lead for a fresh coop-grant, then re-run office-add
  office: error: 1 office link(s) not ok
  ```
- Any `h-mesh-office send --office rctlsy -a worker "..."` fails immediately with exit `1`:
  ```text
  office: error: remote office refused the message: HTTP 401: Unauthorized -- the remote office did not accept our cooperation token (wrong, revoked or not for this office); ask its lead for a fresh coop-grant, then re-run office-add
  ```

### 6.2 Removing an Office from the Address Book (`office-rm`)

To remove a remote office and its stored cooperation token from the local address book (lead only):

```bash
h-mesh-office office-rm rctlsy
h-mesh-office offices
```

Output:

```text
removed office rctlsy
no offices configured
```

---

## 7. Troubleshooting

### 7.1 `office-add` Refuses with Participant Mismatch (`nothing saved`)

- **Symptom:**
  ```text
  office: error: nothing saved: the remote office says this token belongs to participant 'rctlsx-peer', but the address book has 'wrong-peer'; use --participant rctlsx-peer
  ```
- **Cause:** The `--participant` flag passed to `office-add` does not match the participant name the remote lead passed to `h-mesh-office coop-grant <participant>`.
- **Fix:** Re-run `office-add` with `--participant <exact-name-reported>` (or omit `--participant` completely and let `office-add` read it from the remote office's `/whoami` endpoint).

### 7.2 HTTP `401 Unauthorized` on `office-add`, `offices --check`, or `send --office`

| Message reported by `h-mesh-office` | Cause | Fix |
|---|---|---|
| `the cooperation token was revoked by the remote office; ask its lead for a fresh coop-grant, then re-run office-add` | The remote office lead ran `h-mesh-office coop-revoke <token-id>`. | Ask the remote office lead to run `h-mesh-office coop-grant <participant>` and update your address book with `h-mesh-office office-add ... --token-stdin`. |
| `the remote office does not recognise this token (wrong token, or it was issued by another office); ask its lead for a fresh coop-grant, then re-run office-add` | The token string was mistyped, belongs to a different office, or the remote office's Redis state was wiped. | Obtain a fresh `coop-grant` token from the target office's lead and re-run `office-add`. |
| `the participant this token was granted to is no longer enrolled at the remote office; ask its lead for a fresh coop-grant, then re-run office-add` | The cooperation participant was removed from the remote office's registry after the grant. | Have the remote lead run `h-mesh-office coop-grant <participant>` to re-enrol the participant and mint a fresh token. |

### 7.3 HTTP `403 Forbidden`

| Symptom / Detail | Cause | Fix |
|---|---|---|
| `remote office refused the message: HTTP 403: 'as' must be the participant this token is bound to -- the remote office's cooperation policy does not allow this token to send to '...'` | The stored participant name in the address book does not match the token's bound participant (for example, if `--no-verify` was used with the wrong `--participant`), or a raw HTTP caller spoofed `"as"`. | Run `h-mesh-office offices --check` to see the expected participant name, then re-run `h-mesh-office office-add` with the matching `--participant`. |
| `HTTP 403: envelope kind not permitted for a cooperation token` | A raw HTTP caller attempted to send a non-`Message` kind (`Command`, `StartAgent`, `StopAgent`, `Pause`, `Resume`). | Cooperation tokens only permit `Message` envelopes. Lifecycle commands cannot cross office boundaries. |
| `HTTP 403: destination not permitted for a cooperation token` (or CLI error `cannot send to 'host': not an agent` / `invalid agent name 'all'`) | Attempting to send to reserved participants (`all`, `host`, `api`). | Address a specific enrolled agent in the remote office (for example, `-a architect` or `-a worker`). |
| `HTTP 403: not permitted for a cooperation token` | A cooperation token was used against internal tenant endpoints (`/agents`, `/board`, `/alerts`, `/agents/<name>/activity`). | Cooperation tokens are restricted to `GET /whoami`, `POST /agents/<target>/envelopes`, and `GET /agents/<bound-participant>/messages[/stream]`. |

### 7.4 HTTP `404 Not Found`

- **Symptom on `send --office`:**
  ```text
  office: error: remote office refused the message: HTTP 404: Target agent does not exist -- the remote office has no agent 'nonexistent' (check the name with its lead)
  ```
  **Fix:** Verify the target agent's name with the remote office's lead (`h-mesh-office peers` in the remote office).
- **Symptom on `GET /agents/<other>/messages`:**
  ```text
  {"detail":"invalid client agent"} (HTTP 404)
  ```
  **Cause:** A cooperation token can only read the mailbox of the single `<participant>` it is bound to. Reading any other agent's mailbox is refused with `404`.

### 7.5 TLS Verification and Handshake Errors (Exit `1` — `NOT sent` / `nothing saved`)

TLS verification is never disabled. Because no application byte is written before the TLS handshake completes, every TLS verification or handshake failure exits `1` and guarantees the message was **NOT** sent:

| Error message | Cause | Fix |
|---|---|---|
| `TLS verification failed: unable to get local issuer certificate; the message was NOT sent -- if the remote office uses a private CA, trust it with --ca-cert PATH (or H_MESH_CA_CERT); the CA currently configured for this office is /run/h-mesh/tls/ca.crt` (or `self-signed certificate`) | The remote office's certificate was signed by a CA not trusted by the sending office's configured CA bundle. | Stage the peer CA bundle at bootstrap (`H_MESH_CA_CERT=/host/path/to/ca.crt ./container/bootstrap.sh ...`) or pass `--ca-cert /run/h-mesh/tls/ca.crt` to `h-mesh-office office-add`. |
| `TLS verification failed: IP address mismatch, certificate is not valid for '172.16.0.201'.; the message was NOT sent -- use the host name the certificate was issued for, or have the remote office reissue it for this name` (or `Hostname mismatch`) | The `https://` URL host/IP does not match any `subjectAltName` (`IP:` or `DNS:`) on the remote office's certificate. | Reissue the remote office's certificate with `subjectAltName` covering its host IP/DNS name and restage it with `bootstrap.sh`, or use the DNS name the certificate was issued for. |
| `TLS verification failed: certificate has expired; the message was NOT sent -- the remote office must renew its certificate` | The remote office's leaf or CA certificate is past its `notAfter` date. | Renew the certificate on the remote host and rerun `./container/bootstrap.sh` with the renewed `API_TLS_CERT` and `API_TLS_KEY`. |
| `TLS handshake with https://... failed: [SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:...) -- check that the url is the https door of an h-mesh office (the other end may be plain http)` | The remote office is serving plain HTTP on that port, not HTTPS. | Either enable TLS on the remote office via `bootstrap.sh`, or use `http://` with `--allow-plaintext` in a lab testbed. |

### 7.6 Published API Port Closed (`Connection refused`)

- **Symptom:**
  ```text
  office: error: cannot reach the remote office at http://172.16.0.201:29282: [Errno 111] Connection refused
  ```
- **Cause:**
  1. The remote office was bootstrapped with `--non-interactive` without providing either `API_TLS_CERT`/`API_TLS_KEY` or `H_MESH_ALLOW_PLAINTEXT=1`. In that case, `entrypoint.sh` restricts `services.api` to container loopback (`127.0.0.1:8080`) so the published host port stays closed.
  2. Or `H_MESH_BIND_PORTS=0` (`--no-bind-ports`) was selected at bootstrap time.
  3. Or a host firewall (`ufw` / security group) blocks the chosen `API_PORT`.
- **Fix:** Check `offices/<pod>/<tenant>/SUMMARY.md` on the remote host. If it reports loopback-only API, rerun `./container/bootstrap.sh` for that pod/tenant with `API_TLS_CERT` and `API_TLS_KEY` (or `H_MESH_ALLOW_PLAINTEXT=1` in a lab).

### 7.7 Missing Token or Non-Lead Command Refusal

- **`stdin did not carry a cooperation token of the form coop.<16 hex>.<43 chars>`:**
  Ensure you pipe the full `coop.<id>.<secret>` line from `h-mesh-office coop-grant` into `h-mesh-office office-add ... --token-stdin` (do not pass the `token-id` alone or the office's internal `API_TOKEN`).
- **`office <command> is lead-only (the lead is 'architect', you are 'worker')`:**
  `coop-grant`, `coop-revoke`, `office-add`, and `office-rm` can only be executed from the lead agent's pane (`architect`). Any agent can run `h-mesh-office offices`, `h-mesh-office coops`, and `h-mesh-office send --office`.
