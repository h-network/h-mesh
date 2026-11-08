# Fresh API-token bootstrap proof

> Historical record: counts, commits and offices are as of 2026-11-04; see docs/inter-office.md for current behaviour.

Verified on the isolated h-lab VM on 2026-11-04 from pushed commit
`91afbe654bd3766cc5c45d5de488002be0a2b462`, using two fresh pods. The
checkout was `/home/h-lab/ci/setup-api-token.p9CkxD/repo`; both offices and
their volumes were removed afterward. No `.env` was hand edited, no
`API_TOKEN` was supplied, and no Telegram or model credentials were used.

The proof held `~/ci/suite.lock`, serializing it with suite runs. It used
an owned image tag; bootstrap performed the normal Compose build for each
install, without `--skip-build` or an entrypoint override. The source clone
used normal umask `022`; bootstrap independently restricted secret files to
`0600`.

## Commands

From the VM, with a newly created private temporary parent directory:

```bash
(umask 022; git clone --quiet --single-branch \
  --branch setup-agent/bootstrap-api-token \
  https://github.com/h-network/h-mesh.git \
  /home/h-lab/ci/setup-api-token.p9CkxD/repo)
cd /home/h-lab/ci/setup-api-token.p9CkxD/repo
git rev-parse HEAD

env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  H_MESH_IMAGE=h-mesh-setup-api-token:91afbe6 \
  AGENTS=architect DEFAULT_CLI=codex H_MESH_ALLOW_PLAINTEXT=1 \
  ./container/bootstrap.sh --pod setup-api-open --tenant proof \
  --api-port 18090 --session-port 18091 --non-interactive

curl -s --max-time 3 -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:18090/health

env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  H_MESH_IMAGE=h-mesh-setup-api-token:91afbe6 \
  AGENTS=architect DEFAULT_CLI=codex \
  ./container/bootstrap.sh --pod setup-api-safe --tenant proof \
  --api-port 18092 --session-port 18093 --non-interactive

curl -s --max-time 3 -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:18092/health
```

The wrapper captured each bootstrap's stdout/stderr to its own temporary
log, then checked that the generated token was absent from that log,
`SUMMARY.md`, and `attach.sh`. It checked token presence and random format
internally, reporting only presence and file mode. For each container it
read `api.pid` and `interoffice_relay.pid` under
`/home/ubuntu/.h-mesh/run/proof`, checked both processes with `kill(pid, 0)`,
and sent an authenticated request to `http://127.0.0.1:8080/health` from
inside the container. The bearer token was read from the container's
environment within that process and never printed.

## Results

| Fresh office | Generated `.env` | API and relay | Internal authenticated health | Published health |
| --- | --- | --- | --- | --- |
| `setup-api-open/proof` | Token present, mode `0600` | Both alive | HTTP `200` | HTTP `401` without credentials |
| `setup-api-safe/proof` | Token present, mode `0600` | Both alive | HTTP `200` | `000`, connection closed as intended |

The open install's closing output reported its generated summary and attach
script and the exact Compose status/log/stop commands. The safe install's
last output line was also present in its `SUMMARY.md`:

```text
API is loopback-only inside the container; published API port 18092 is closed. To open it, rerun bootstrap with API_TLS_CERT and API_TLS_KEY (in-container paths), or H_MESH_ALLOW_PLAINTEXT=1 to explicitly accept plaintext.
```

Cleanup used only these two generated projects:

```bash
H_MESH_TENANT_ENV_FILE="$PWD/offices/setup-api-open/proof/.env" \
  H_MESH_IMAGE=h-mesh-setup-api-token:91afbe6 \
  docker compose -p h-mesh-setup-api-open-proof \
  -f container/compose.yaml -f container/compose.ports.yaml \
  --env-file offices/setup-api-open/proof/.env down -v --remove-orphans
H_MESH_TENANT_ENV_FILE="$PWD/offices/setup-api-safe/proof/.env" \
  H_MESH_IMAGE=h-mesh-setup-api-token:91afbe6 \
  docker compose -p h-mesh-setup-api-safe-proof \
  -f container/compose.yaml -f container/compose.ports.yaml \
  --env-file offices/setup-api-safe/proof/.env down -v --remove-orphans
```

The proof exited `0`, removed its temporary checkout, and retained its
credential-free report at
`/home/h-lab/ci/logs/setup-api-token-fresh-20261104T143820Z.log`. It did not
touch the operator's default, vm/home, vm/ef, laba, or labb offices.

## Suite isolation

The changed host setup/hire/upgrade environment builders explicitly use
`API_BIND=127.0.0.1` and `API_PORT=0`; no fixed substitute API port is used.
The forced-hire fixture allocates
`tempfile.mkdtemp(prefix="h_mesh_test_hire_status_dispatch_")` and explicitly
sets its daemon run directory to `Path(tmpdir) / "run"` and state directory
to `Path(tmpdir) / "state"`. Its cleanup glob is exactly
`run_dir.glob("*.pid")`, or
`/tmp/h_mesh_test_hire_status_dispatch_<random>/run/*.pid`. That path comes
from the local temporary-directory variable, never `HOME`, persisted
configuration, or an `offices/` directory.
