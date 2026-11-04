# Inter-Office HTTPS/TLS Live Proof & Initial Defect Evidence (`c5b0566`)

- **Date:** 2026-11-04
- **Tested base commit:** `c5b0566b4b0d144c467022cd1fbb2e4652f54351` (`origin/architect/release-candidate`)
- **Host:** `h-lab@172.16.0.201`
- **Throwaway offices:**
  - `tlstestx/lab` (`API_PORT=29180`, `SESSION_PORT=29181`, project `h-mesh-tlstestx-lab`)
  - `tlstesty/lab` (`API_PORT=29182`, `SESSION_PORT=29183`, project `h-mesh-tlstesty-lab`)

---

## 1. Initial Fresh Bootstrap TLS Findings (`c5b0566`)

### 1.1 Interactive `container/bootstrap.sh` with `API_TLS_CERT` / `API_TLS_KEY` on a Brand-New Office

When entering `/home/ubuntu/.h-mesh/tls/tls.crt` and `/home/ubuntu/.h-mesh/tls/tls.key` at `container/bootstrap.sh`'s interactive TLS prompt on a brand-new office (`tlstestx/lab`), `bootstrap.sh` immediately ran `docker compose -p h-mesh-tlstestx-lab ... up -d --build` before the certificate files existed inside the newly created `h-mesh-tlstestx-lab-state` volume:

- **`docker logs h-mesh-tlstestx-lab-h-mesh-1`:**
  ```text
  Starting daemons (logs written to /home/ubuntu/.h-mesh/run/lab)...
    • switch: starting (pid: 168)
    • tmux_reconciler: starting (pid: 169)
    • watchdog: starting (pid: 170)
    • interoffice_relay: starting (pid: 171)
    • api: starting (pid: 172)
    • startup failed; stopping daemons started by this invocation
    • switch: stopping pid 168...
    • tmux_reconciler: stopping pid 169...
    • watchdog: stopping pid 170...
    • interoffice_relay: stopping pid 171...
  error: api failed to start. Check /home/ubuntu/.h-mesh/run/lab/api.log
    • api: pidfile stale (pid 172 not running), removing
  [entrypoint] shutting down (exit=1)...
  ```
- **`/home/ubuntu/.h-mesh/run/lab/api.log`:**
  ```text
  File "/opt/h-mesh/lib/python3.12/site-packages/uvicorn/config.py", line 131, in create_ssl_context
    ctx.load_cert_chain(certfile, keyfile, get_password)
  FileNotFoundError: [Errno 2] No such file or directory
  ```

### 1.2 Private CA / Self-Signed Certificate Trust on the Sending Office

After populating `/home/ubuntu/.h-mesh/tls/tls.crt` (`0644`) and `/home/ubuntu/.h-mesh/tls/tls.key` (`0600`) (signed by a private test CA `CN=h-mesh-test-ca` with `subjectAltName = IP:172.16.0.201,DNS:localhost,IP:127.0.0.1`) in both `tlstestx` and `tlstesty`:

```text
tlstestx: pids=['api.pid', 'interoffice_relay.pid', 'switch.pid', 'tmux_reconciler.pid', 'watchdog.pid'] https_unauth=401
tlstesty: pids=['api.pid', 'interoffice_relay.pid', 'switch.pid', 'tmux_reconciler.pid', 'watchdog.pid'] https_unauth=401
Granted tokens: tlstesty tid=bbfd08fb66ba1b74 (len=65), tlstestx tid=ecb6c10cb74f7ad9 (len=65)
tlstestx office-add exit=0 out=added office tlstesty -> https://172.16.0.201:29182 (as tlstestx-peer) err=
tlstesty office-add exit=0 out=added office tlstestx -> https://172.16.0.201:29180 (as tlstesty-peer) err=
```

Out-of-the-box `h-mesh-office send --office` (and with `H_MESH_CA_CERT=/home/ubuntu/.h-mesh/tls/ca.crt` set) failed because neither `h-app/modules/office/cli.py` nor `h-app/modules/interoffice/relay.py` configured an `ssl.SSLContext` or CA bundle:

```text
Out-of-the-box send exit=3
  stdout=
  stderr=office: error: request to the remote office failed mid-flight ([SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1011)) -- the message may have been accepted; do not blindly retry
tlstestx interoffice_relay.log:
2026-11-04 19:08:47,616 [WARNING] modules.interoffice.relay: relay office=tlstesty event=disconnected
2026-11-04 19:08:48,731 [WARNING] modules.interoffice.relay: relay office=tlstesty event=disconnected
```

---

## 2. Negative TLS Verification Tests (`wronghost`, `expired`, `untrusted`)

With `SSL_CERT_FILE=/home/ubuntu/.h-mesh/tls/ca.crt` set in the sender, all three invalid certificate cases were rejected during the TLS handshake without any silent downgrade to `http://` (note: on `c5b0566`, `_NOT_SENT_ERRORS` in `h-app/modules/office/cli.py:2088` omitted `ssl.SSLError`, so these exited `3` instead of `1`):

```text
=== Negative TLS test: wrong-hostname (CN=wrong.example.invalid, signed by trusted CA) ===
exit=3
stderr=office: error: request to the remote office failed mid-flight ([SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: IP address mismatch, certificate is not valid for '172.16.0.201'. (_ssl.c:1011)) -- the message may have been accepted; do not blindly retry

=== Negative TLS test: expired-cert (notAfter=2020-01-02, signed by trusted CA) ===
exit=3
stderr=office: error: request to the remote office failed mid-flight ([SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has expired (_ssl.c:1011)) -- the message may have been accepted; do not blindly retry

=== Negative TLS test: untrusted-self-signed-cert (not signed by trusted CA) ===
exit=3
stderr=office: error: request to the remote office failed mid-flight ([SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate (_ssl.c:1011)) -- the message may have been accepted; do not blindly retry
```

---

## 3. Both-Way HTTPS Send & Relay Reply Verification (with CA Trusted)

Once `ca.crt` was trusted inside both containers and `services.interoffice_relay` restarted:

```text
X->Y HTTPS send: sent to tlstesty/worker: 196 bytes (e7335c45c1b643d6a2bd5ec7e7370a87)
Y->X HTTPS send: sent to tlstestx/worker: 196 bytes (d942da4da3a44ab8a209e2f5b4c7468a)

=== tlstestx interoffice_relay.log ===
{"ts":"2026-11-04T19:10:18.590Z","module":"interoffice","event":"sent","writer":"interoffice","stream_id":"c28492c1c5d64140b3ad0edc1be1d5b0","correlation_id":"bf674f9358664d19bac732e0fee37693","source":"relay","destination":"architect"}
2026-11-04 19:10:18,590 [INFO] modules.interoffice.relay: relay office=tlstesty event=admitted destination=architect stream_id=c28492c1c5d64140b3ad0edc1be1d5b0

=== tlstestx architect pane (relayed HTTPS reply) ===
  ❯ [message from relay] [from tlstesty/worker] https ack from tlstesty/worker
    [reply to tlstesty/worker: h-mesh-office send --office tlstesty -a worker "..."]

=== tlstesty interoffice_relay.log ===
{"ts":"2026-11-04T19:10:18.365Z","module":"interoffice","event":"sent","writer":"interoffice","stream_id":"7456a865a42344bf883b02bf9c49f761","correlation_id":"96ed3ff45e11478db80e60ee26122783","source":"relay","destination":"architect"}
2026-11-04 19:10:18,365 [INFO] modules.interoffice.relay: relay office=tlstestx event=admitted destination=architect stream_id=7456a865a42344bf883b02bf9c49f761

=== tlstesty architect pane (relayed HTTPS reply) ===
  ❯ [message from relay] [from tlstestx/worker] https ack from tlstestx/worker
    [reply to tlstestx/worker: h-mesh-office send --office tlstestx -a worker "..."]
```
