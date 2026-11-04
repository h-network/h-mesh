# Final Release Gate A Verification (`rc-next-3` @ `86dcddf`)

This document records the live fresh-install Release Gate A verification (plaintext AND HTTPS/TLS with private CA) on `h-lab@172.16.0.201` against [`docs/inter-office.md`](inter-office.md) on candidate commit `86dcddffc0664efdc9612d8f9f28fe5099f66686` (`origin/architect/rc-next-3`, certified `1904/1904`).

---

## 1. Summary Table

| Step | Check | Verdict | Evidence Summary |
|---|---|---|---|
| **1** | Build `h-mesh:rc-next-3-86dcddf` once after `df -h /` (`>= 2.5G`) check; keep image for Gate B | **PASS** | `/` had `6.3G` free (`73%` used). Cloned `86dcddffc0664efdc9612d8f9f28fe5099f66686` to `/tmp/rc-next-3-86dcddf/h-mesh` (`umask 022`) and built `h-mesh:rc-next-3-86dcddf` (`sha256:f0f961b0d9a77d04a91179daf37bd05fed4b676909a7f9fcdf24f51fb4d70a3d`). Image kept on `h-lab` for Gate B. |
| **2a** | Fresh plaintext offices (`rcgatex/lab` @ `29280/29281`, `rcgatey/lab` @ `29282/29283`) via interactive `./container/bootstrap.sh` | **PASS** | Both offices booted all 5 daemons (`core.service`, `services.tmux_reconciler`, `services.watchdog`, `services.interoffice_relay`, `services.api`), generated `0600` 64-hex `API_TOKEN` automatically (unprinted), answered `HTTP 401` on `http://172.16.0.201:29280/agents` and `:29282/agents`, and passed `attach.sh` attach/detach (`rc=0`). |
| **2b** | Plaintext pairing following `docs/inter-office.md` verbatim + `/whoami` wrong `--participant` refusal + `offices --check` | **PASS** | Deliberately passing `--participant wrong-peer` to `office-add` exited `1` with `office: error: nothing saved: the remote office says this token belongs to participant 'rcgatex-peer', but the address book has 'wrong-peer'; use --participant rcgatex-peer` and left `offices` empty (`no offices configured`). Correct `--participant` succeeded with `; verified with the remote office (rcgatey/lab)`. `h-mesh-office offices --check` returned `link: ok` (`exit 0`) in both offices. |
| **2c** | Bidirectional `send --office`, relay reply with `[reply to <office>/<agent>: ...]` hint, preserved `correlation_id`, and local Gate A checks | **PASS** | Both `rcgatex -> rcgatey` (`stream_id=80d3da2c44af46aeb888f134a406eeb9`, `correlation_id=f9f8d138d3ee4096a2e0248a85efbfa8`) and `rcgatey -> rcgatex` (`stream_id=a24f0ce278be485db6ff9130a4aab1e7`, `correlation_id=04fa7ad3679d439b8b5b874b62091bc2`) delivered to `worker`; both local-model workers replied with `--reply-to <stream_id>`, and `interoffice_relay` delivered the replies to each `architect` pane with `[reply to rcgatey/worker: h-mesh-office send --office rcgatey -a worker "..."]` and `[reply to rcgatex/worker: h-mesh-office send --office rcgatex -a worker "..."]` and identical `correlation_id`. Local tmux send, `add`/`take`/`done` (+ `[board]` completion notice), `status`, `peers`, `list`, and `usage` all passed. |
| **2d** | `coop-revoke` + `h-mesh-office offices --check` + `send --office` + `office-rm` | **PASS** | After `h-mesh-office coop-revoke ce39c9ca11ca789a` in `rcgatey`, `h-mesh-office offices --check` in `rcgatex` exited `1` with `link: token problem -- the cooperation token was revoked by the remote office; ask its lead for a fresh coop-grant, then re-run office-add`, and `send --office` exited `1` with `HTTP 401: Unauthorized`. `office-rm rcgatey` removed the entry cleanly. |
| **3** | 10+ minute idle wait (`20:25:56Z` -> `20:36:36Z`, `10m 40s`) | **PASS** | After `10m 40s` idle across both offices, `pod:<pod>:tenant:lab:unreplied` and `unreplied.alerted` remained empty (`''`), and neither lead pane nor `watchdog.log` contained any unanswered-message or watchdog alert. |
| **4a** | Fresh HTTPS/TLS offices (`rctlsx/lab` @ `29280/29281`, `rctlsy/lab` @ `29282/29283`) with private CA via interactive `./container/bootstrap.sh` + bidirectional HTTPS pairing & relay replies | **PASS** | Interactive `bootstrap.sh` validated and staged `/run/h-mesh/tls/{api.crt,api.key,ca.crt}` (`0600`, read-only mount via `compose.tls.yaml`) on the first pass (`exit 0`). Both offices answered `HTTP 401` over `https://`. Bidirectional `office-add --ca-cert /run/h-mesh/tls/ca.crt --token-stdin` verified via `/whoami`, `offices --check` showed `CA /run/h-mesh/tls/ca.crt link: ok`, and bidirectional HTTPS sends + SSE relay replies preserved `correlation_id` (`8bd5ab45f1ed4f6cb72262871c69530b` and `0b29b2031ce84bbd9f8c37cd63d57f5c`) and cross-office reply hints. |
| **4b** | Three negative TLS cases (wrong hostname/IP, expired cert, untrusted CA) | **PASS** | All 3 failed before any application byte was sent (`POST /agents/` count in `rctlsy` `api.log` remained `5 -> 5`): `send --office` exited `1` with `TLS verification failed: ...; the message was NOT sent -- ...`, `office-add` exited `1` with `nothing saved: TLS verification failed: ...`, and `offices --check` exited `1` with `link: TLS problem -- TLS verification failed: ...`. |
| **5 & 6** | Update `docs/inter-office.md`, link from `README.md`, tear down test offices/volumes/checkout, keep `h-mesh:rc-next-3-86dcddf` | **PASS** | `docs/inter-office.md` updated with exact verbatim outputs (no `'PoC'` or `'pending'` wording), linked from `README.md`, `docs/inter-office-poc.md` removed, all `rcgatex`/`rcgatey`/`rctlsx`/`rctlsy` containers/volumes and `/tmp/rc-next-3-86dcddf` removed (`4.9G` free on `/`), and `h-mesh:rc-next-3-86dcddf` retained for Gate B. |

---

## 2. Verbatim Transcripts from the Live Run

### 2.1 Plaintext Pairing & `/whoami` Wrong-Participant Refusal (`rcgatex` <-> `rcgatey`)

```text
=== rcgatey: h-mesh-office coop-grant rcgatex-peer ===
participant: rcgatex-peer
token-id: ce39c9ca11ca789a
token: coop.ce39c9ca11ca789a.<REDACTED>
This token is shown once. Give it to the other office's lead (office-add ... --token-stdin); revoke with: office coop-revoke ce39c9ca11ca789a
=== rcgatey: coops & peers -i ===
ce39c9ca11ca789a	rcgatex-peer	Message	active	1793823817.4884148
worker
interfaces (api/office -- recognized, not tmux colleagues): api (api), host (office), rcgatex-peer (api)

=== rcgatex: office-add with WRONG --participant (exit=1) ===
stdout: 
stderr: office: error: nothing saved: the remote office says this token belongs to participant 'rcgatex-peer', but the address book has 'wrong-peer'; use --participant rcgatex-peer
offices after wrong --participant: no offices configured

=== rcgatex: office-add with CORRECT --participant (exit=0) ===
stderr: office: warning: http://172.16.0.201:29282 is plaintext http; the token travels in the clear
stdout: added office rcgatey -> http://172.16.0.201:29282 (as rcgatex-peer); verified with the remote office (rcgatey/lab)
=== rcgatex: offices, offices --check, peers -i ===
rcgatey	http://172.16.0.201:29282	as rcgatex-peer	added by architect
offices --check (exit=0): rcgatey	http://172.16.0.201:29282	as rcgatex-peer	added by architect	link: ok
worker
interfaces (api/office -- recognized, not tmux colleagues): api (api), host (office), relay (api)
```

### 2.2 Bidirectional Plaintext Send, Relay Reply Hint, and `correlation_id` Preservation

```text
=== X -> Y send --office ===
sent to rcgatey/worker: 131 bytes (80d3da2c44af46aeb888f134a406eeb9)
=== Y -> X send --office ===
sent to rcgatex/worker: 131 bytes (a24f0ce278be485db6ff9130a4aab1e7)

=== rcgatex/architect pane (relayed reply from rcgatey/worker) ===
  ❯ 2026-11-04T20:24:08Z #da550c64 [message from relay] [from rcgatey/worker] Hello rcgatex architect! How can I assist
    [reply to rcgatey/worker: h-mesh-office send --office rcgatey -a worker "..."]

=== rcgatey/architect pane (relayed reply from rcgatex/worker) ===
  ❯ 2026-11-04T20:24:09Z #c44eb3c5 [message from relay] [from rcgatex/worker] Got it, architect. Replying using the
    [reply to rcgatex/worker: h-mesh-office send --office rcgatex -a worker "..."]

=== rcgatex xsent.s80d3da2c44af46aeb888f134a406eeb9 ===
  {'sender': 'architect', 'sender_incarnation': 'af3af774816549558f97f476a8f1f6e5', 'correlation_id': 'f9f8d138d3ee4096a2e0248a85efbfa8', 'office': 'rcgatey', 'agent': 'worker', 'ts': '2026-11-04T20:24:05.186Z'}
--- rcgatex interoffice_relay.log ---
  {"ts":"2026-11-04T20:24:08.271Z","module":"interoffice","event":"sent","writer":"interoffice","stream_id":"da550c645e9146e98f70cce2cb3b13c9","correlation_id":"f9f8d138d3ee4096a2e0248a85efbfa8","source":"relay","destination":"architect"}
  2026-11-04 20:24:08,272 [INFO] modules.interoffice.relay: relay office=rcgatey event=admitted destination=architect stream_id=da550c645e9146e98f70cce2cb3b13c9

=== rcgatey xsent.sa24f0ce278be485db6ff9130a4aab1e7 ===
  {'sender': 'architect', 'sender_incarnation': '524540f15c064e3fb5ffb06e7e462074', 'correlation_id': '04fa7ad3679d439b8b5b874b62091bc2', 'office': 'rcgatex', 'agent': 'worker', 'ts': '2026-11-04T20:24:05.500Z'}
--- rcgatey interoffice_relay.log ---
  {"ts":"2026-11-04T20:24:09.233Z","module":"interoffice","event":"sent","writer":"interoffice","stream_id":"c44eb3c5a59a4ea0921aaa5b07c67101","correlation_id":"04fa7ad3679d439b8b5b874b62091bc2","source":"relay","destination":"architect"}
  2026-11-04 20:24:09,234 [INFO] modules.interoffice.relay: relay office=rcgatex event=admitted destination=architect stream_id=c44eb3c5a59a4ea0921aaa5b07c67101
```

### 2.3 10+ Minute Idle Check (`20:25:56Z` -> `20:36:36Z`) and `coop-revoke` + `offices --check`

```text
Idle start UTC: 2026-11-04T20:25:56.792673+00:00
Idle end UTC:   2026-11-04T20:36:36.455862+00:00
=== rcgatex post-10m-idle check ===
  unreplied: ''
  unreplied.alerted: ''
  lead pane unanswered/watchdog lines: []
  watchdog.log alert lines: []
=== rcgatey post-10m-idle check ===
  unreplied: ''
  unreplied.alerted: ''
  lead pane unanswered/watchdog lines: []
  watchdog.log alert lines: []

=== rcgatey coop-revoke ce39c9ca11ca789a (exit=0) ===
revoked ce39c9ca11ca789a
ce39c9ca11ca789a	rcgatex-peer	Message	revoked	1793823817.4884148

=== rcgatex offices --check after coop-revoke (exit=1) ===
stdout: rcgatey	http://172.16.0.201:29282	as rcgatex-peer	added by architect	link: token problem -- the cooperation token was revoked by the remote office; ask its lead for a fresh coop-grant, then re-run office-add
stderr: office: error: 1 office link(s) not ok

=== rcgatex send --office after coop-revoke (exit=1) ===
stderr: office: error: remote office refused the message: HTTP 401: Unauthorized -- the remote office did not accept our cooperation token (wrong, revoked or not for this office); ask its lead for a fresh coop-grant, then re-run office-add
```

### 2.4 HTTPS/TLS Pairing with Private CA (`rctlsx` <-> `rctlsy`) and Bidirectional Relay Replies

```text
=== TLS bootstrap rctlsx/lab exited 0 (prompts answered: 13/13) ===
=== TLS bootstrap rctlsy/lab exited 0 (prompts answered: 13/13) ===
rctlsx https://172.16.0.201:29280/agents -> HTTP 401
rctlsy https://172.16.0.201:29282/agents -> HTTP 401

=== rctlsx office-add rctlsy over HTTPS (exit=0) ===
stdout: added office rctlsy -> https://172.16.0.201:29282 (as rctlsx-peer); verified with the remote office (rctlsy/lab)
rctlsx offices --check (exit=0): rctlsy	https://172.16.0.201:29282	as rctlsx-peer	added by architect	CA /run/h-mesh/tls/ca.crt	link: ok

=== rctlsy office-add rctlsx over HTTPS (exit=0) ===
stdout: added office rctlsx -> https://172.16.0.201:29280 (as rctlsy-peer); verified with the remote office (rctlsx/lab)
rctlsy offices --check (exit=0): rctlsx	https://172.16.0.201:29280	as rctlsy-peer	added by architect	CA /run/h-mesh/tls/ca.crt	link: ok

=== HTTPS X -> Y send --office ===
sent to rctlsy/worker: 107 bytes (016db095a9fd4db78b45c23f31e747d6)
=== HTTPS Y -> X send --office ===
sent to rctlsx/worker: 107 bytes (44e3ce4cb97d4ecf85aecb9b5177953d)

=== rctlsx/architect pane (HTTPS relayed reply from rctlsy/worker) ===
  ❯ 2026-11-04T20:39:46Z #11ffd29a [message from relay] [from rctlsy/worker] Hello! Thanks for the greeting over HTTPS.
    [reply to rctlsy/worker: h-mesh-office send --office rctlsy -a worker "..."]

=== rctlsy/architect pane (HTTPS relayed reply from rctlsx/worker) ===
  ❯ 2026-11-04T20:39:46Z #abffa7e5 [message from relay] [from rctlsx/worker] Hello architect! Received your message. How
    [reply to rctlsx/worker: h-mesh-office send --office rctlsx -a worker "..."]

=== rctlsx HTTPS correlation_id check (expected 8bd5ab45f1ed4f6cb72262871c69530b) ===
  {"ts":"2026-11-04T20:39:46.535Z","module":"interoffice","event":"sent","writer":"interoffice","stream_id":"11ffd29a95bd47e389e657219b3795f5","correlation_id":"8bd5ab45f1ed4f6cb72262871c69530b","source":"relay","destination":"architect"}

=== rctlsy HTTPS correlation_id check (expected 0b29b2031ce84bbd9f8c37cd63d57f5c) ===
  {"ts":"2026-11-04T20:39:46.654Z","module":"interoffice","event":"sent","writer":"interoffice","stream_id":"abffa7e5b61e4cebb781af254b5b0424","correlation_id":"0b29b2031ce84bbd9f8c37cd63d57f5c","source":"relay","destination":"architect"}
```

### 2.5 Three Negative TLS Verification Cases (All Exit `1` with `the message was NOT sent` and Zero POSTs)

```text
=== Negative TLS Case 1. Wrong hostname/IP (SAN=DNS:wrong.example.invalid) ===
send --office exit=1
send --office stderr: office: error: TLS verification failed: IP address mismatch, certificate is not valid for '172.16.0.201'.; the message was NOT sent -- use the host name the certificate was issued for, or have the remote office reissue it for this name
offices --check exit=1
offices --check stdout: rctlsy	https://172.16.0.201:29282	as rctlsx-peer	added by architect	CA /run/h-mesh/tls/ca.crt	link: TLS problem -- TLS verification failed: IP address mismatch, certificate is not valid for '172.16.0.201'. -- use the host name the certificate was issued for, or have the remote office reissue it for this name
office-add exit=1
office-add stderr: office: error: nothing saved: TLS verification failed: IP address mismatch, certificate is not valid for '172.16.0.201'. -- use the host name the certificate was issued for, or have the remote office reissue it for this name
POST /agents/ count in rctlsy api.log: before=5, after=5

=== Negative TLS Case 2. Expired certificate (notAfter=2020-01-02) ===
send --office exit=1
send --office stderr: office: error: TLS verification failed: certificate has expired; the message was NOT sent -- the remote office must renew its certificate
offices --check exit=1
offices --check stdout: rctlsy	https://172.16.0.201:29282	as rctlsx-peer	added by architect	CA /run/h-mesh/tls/ca.crt	link: TLS problem -- TLS verification failed: certificate has expired -- the remote office must renew its certificate
office-add exit=1
office-add stderr: office: error: nothing saved: TLS verification failed: certificate has expired -- the remote office must renew its certificate
POST /agents/ count in rctlsy api.log: before=5, after=5

=== Negative TLS Case 3. Untrusted issuer (signed by rogue_ca.crt) ===
send --office exit=1
send --office stderr: office: error: TLS verification failed: unable to get local issuer certificate; the message was NOT sent -- if the remote office uses a private CA, trust it with --ca-cert PATH (or H_MESH_CA_CERT); the CA currently configured for this office is /run/h-mesh/tls/ca.crt
offices --check exit=1
offices --check stdout: rctlsy	https://172.16.0.201:29282	as rctlsx-peer	added by architect	CA /run/h-mesh/tls/ca.crt	link: TLS problem -- TLS verification failed: unable to get local issuer certificate -- if the remote office uses a private CA, trust it with --ca-cert PATH (or H_MESH_CA_CERT); the CA currently configured for this office is /run/h-mesh/tls/ca.crt
office-add exit=1
office-add stderr: office: error: nothing saved: TLS verification failed: unable to get local issuer certificate -- if the remote office uses a private CA, trust it with --ca-cert PATH (or H_MESH_CA_CERT); the CA currently configured for this office is /run/h-mesh/tls/ca.crt
POST /agents/ count in rctlsy api.log: before=5, after=5
```
