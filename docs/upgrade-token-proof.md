# Upgrade token and daemon migration proof

> Historical record: counts, commits and offices are as of 2026-11-04; see docs/inter-office.md for current behaviour.

The runtime under test is `setup-agent/upgrade-token-daemons` at
`28be8419064e8c4e0fe2df08238d56193d1cc5a6`, built on release candidate
`bc001977508fb00caab17f2eb9540657be63bdc2`. The legacy runtime is develop
`4d1f3889c37998f2de040d911147d3a89766a6d1`.

## Native first transition

A fresh develop clone and fresh editable venv reproduce the actual old
upgrader, including its imported Python objects. The clone's own local
`develop` branch tracks `origin/setup-agent/upgrade-token-daemons`, so its
ordinary pull advances to the pinned runtime SHA. Source files and generated
configuration are never hand-edited.

Every application child receives an explicitly constructed environment:

```text
HOME=$R/home
PATH=$R/venv/bin:/home/h-lab/ci/bin:/usr/local/bin:/usr/bin:/bin
USER=h-lab LOGNAME=h-lab LANG=C.UTF-8 TERM=dumb
POD=setup-upgrade-host TENANT=hostproof AGENTS=
H_MESH_STATE_DIR=$R/state H_MESH_RUN_DIR=$R/run H_MESH_WORKDIR=$R/workdir
TMUX_TMPDIR=$R/tmux TMUX_SOCKET=$R/tmux.sock TMUX_SESSION=hostproof
REDIS_URL=redis://127.0.0.1:$PRIVATE_PORT/0 API_BIND=127.0.0.1 API_PORT=0
```

Redis binds an OS-selected ephemeral loopback port, has a unique Unix
socket and private data directory, and disables snapshots/AOF. Its server
run ID is compared over the unique socket and TCP before either setup or
upgrade uses it. No ambient credentials or Telegram settings are inherited.
The empty roster is supported manual-hire mode and needs no model process.

Commands, with `$R` the fresh sandbox directory and `$PRIVATE_PORT` its
allocated Redis port:

```bash
git clone --quiet --branch develop https://github.com/h-network/h-mesh.git "$R/repo"
python3 -m venv "$R/venv"
"$R/venv/bin/python" -m pip install --no-cache-dir -e "$R/repo"
git -C "$R/repo" branch --set-upstream-to=origin/setup-agent/upgrade-token-daemons develop

"$R/repo/setup.sh" --host --pod setup-upgrade-host --tenant hostproof \
  --redis-url "redis://127.0.0.1:$PRIVATE_PORT/0" --session hostproof \
  --tmux-socket "$R/tmux.sock" --tmux-tmpdir "$R/tmux" --venv "$R/venv" \
  --skip-deps --skip-install --non-interactive

# First: old process performs the ordinary documented pull/install.
"$R/venv/bin/h-mesh-upgrade" --pod setup-upgrade-host --tenant hostproof \
  --redis-url "redis://127.0.0.1:$PRIVATE_PORT/0" --session hostproof \
  --tmux-socket "$R/tmux.sock" --tmux-tmpdir "$R/tmux" --venv "$R/venv"

# Second: the documented fresh invocation loads the installed new code.
"$R/venv/bin/h-mesh-upgrade" --pod setup-upgrade-host --tenant hostproof \
  --redis-url "redis://127.0.0.1:$PRIVATE_PORT/0" --session hostproof \
  --tmux-socket "$R/tmux.sock" --tmux-tmpdir "$R/tmux" --venv "$R/venv" \
  --skip-pull --skip-install

# Third: repeat the second command unchanged.
"$R/venv/bin/h-mesh-upgrade" --pod setup-upgrade-host --tenant hostproof \
  --redis-url "redis://127.0.0.1:$PRIVATE_PORT/0" --session hostproof \
  --tmux-socket "$R/tmux.sock" --tmux-tmpdir "$R/tmux" --venv "$R/venv" \
  --skip-pull --skip-install
```

Result: **PASS**, outer process exit `0`. The actual sandbox was
`/home/h-lab/ci/setup-upgrade-host.ejk657i5`, Redis used `48993`, and the
unique unit was `setup-upgrade-host-proof-2978609.service`.

| Invocation | Observed result |
| --- | --- |
| Old ordinary upgrade | Exit `0`, Git reached `28be841`; token absent; only switch, tmux reconciler and watchdog. This intentionally reproduces the old loaded-code behavior. |
| Documented fresh second invocation | Exit `0`; exactly one token setting, mode `0600`; API and relay running alongside the original three daemons; API environment bound to `127.0.0.1`. |
| Identical third invocation | Exit `0`; byte-identical tenant configuration and token; previous daemon PIDs stopped and replaced; exactly one process for each of the five modules. |

The second and third commands ended with:

```text
API is configured for loopback only. To enable non-loopback access, set API_TLS_CERT and API_TLS_KEY, or H_MESH_ALLOW_PLAINTEXT=1 to explicitly accept plaintext.
```

Log: `/home/h-lab/ci/logs/setup-upgrade-host-proof-20261104T174815Z.log`.
No token value was printed.

Cleanup uses authenticated daemon identities only under `$R/run`, the
private tmux socket and child Redis process, then deletes `$R`. A unique
transient systemd unit with `KillMode=control-group` reaps any remaining
sandbox descendants. Process counting is restricted to that unit's cgroup.

## Container bootstrap rerun

The separate fresh checkout starts at the same old develop SHA. Only pod
`setup-upgrade-token`, tenant `proof`, Compose project
`h-mesh-setup-upgrade-token-proof`, and published ports `18220`/`18221`
are used. It inherits no credentials and supplies no token or model.

```bash
env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  H_MESH_IMAGE=h-mesh-setup-release-gate-b:develop-4d1f388 \
  AGENTS=architect,worker DEFAULT_CLI=codex \
  ./container/bootstrap.sh --pod setup-upgrade-token --tenant proof \
  --api-port 18220 --session-port 18221 --non-interactive
```

Normal old-code office commands create two board tickets per agent. Normal
`StartAgent` envelopes enroll API-type `teleclient` and `relay`, and ordinary
messages create an unread inbox record and both old unreplied entries.
The fixture snapshots registry/incarnations, exact ticket records, inbox
bytes and the old offices address book inside the retained state volume.
It seeds the legacy offices record directly because develop has no
inter-office CLI; its loopback URL has no cooperation token and cannot
open a relay worker. Both unreplied timestamps are aged to exercise the
real new watchdog after the transition.

To limit disk usage, the proof stops only its own project **without `-v`**
and removes only its own old image before building the candidate. This
retains the old office configuration and named volumes:

```bash
H_MESH_TENANT_ENV_FILE="$PWD/offices/setup-upgrade-token/proof/.env" \
  H_MESH_IMAGE=h-mesh-setup-release-gate-b:develop-4d1f388 \
  docker compose -p h-mesh-setup-upgrade-token-proof \
  -f container/compose.yaml -f container/compose.ports.yaml \
  --env-file offices/setup-upgrade-token/proof/.env down --remove-orphans

docker image rm h-mesh-setup-release-gate-b:develop-4d1f388
# Architect-authorized cache-only cleanup; never image/volume pruning.
docker builder prune -f
git checkout --detach 28be8419064e8c4e0fe2df08238d56193d1cc5a6

env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  H_MESH_IMAGE=h-mesh-setup-upgrade-token:28be841 H_MESH_ALLOW_PLAINTEXT=1 \
  ./container/bootstrap.sh --pod setup-upgrade-token --tenant proof \
  --api-port 18220 --session-port 18221 --non-interactive

# Repeat exactly the candidate bootstrap command, adding --skip-build.
```

Every build/up boundary has a `df -h /` check and a hard stop below
2.5 GiB available. The repeated command reuses the exact candidate image.
No hand edits to `.env` occur. Token verification checks presence, line count,
mode and digest equality without printing the token.

Result: **PASS**, outer wrapper exit `0`.

| Check | Observed result |
| --- | --- |
| Old install | Generated `.env` has no API token; published API returns `000`; exactly three original daemons. |
| Candidate bootstrap against old state | Token present exactly once, mode `0600`; API and relay start automatically; published `/health` returns HTTP `401` without credentials. |
| State preservation | Registry, incarnation IDs, all four exact ticket records, API inbox bytes and offices hash match the old snapshot. One tmux window per agent and one process per daemon. |
| Real post-upgrade watchdog | Old relay unreplied and alerted entries pruned with no new relay alert; a new alert for the ordinary API client is delivered and its unanswered backlog remains. |
| Identical bootstrap rerun with `--skip-build` | Same token digest, daemon PIDs, windows and persisted state; no duplicate hire or daemon. |

Boundary free space was 8.9 GiB before the old build, 8.4 GiB before the
candidate build/up and 5.0 GiB before the unchanged-image rerun. After
cleanup it was 6.9 GiB. The candidate image tag was
`h-mesh-setup-upgrade-token:28be841`; no borrowed image was retagged or
removed.

Sandbox: `/home/h-lab/ci/setup-upgrade-container.XbyxAq`.
Log: `/home/h-lab/ci/logs/setup-upgrade-container-20261104T174845Z.log`.
The generated configuration was never hand-edited; token checks printed
presence and mode only.

Final cleanup uses explicit `-p h-mesh-setup-upgrade-token-proof` with the
matching Compose/env files and `down -v --remove-orphans`, removes only
the two owned image tags and the proof checkout. Operator offices and
volumes, other agents' images, and host Redis are never targeted.

## Canonical suite

Architect independently accounted for **19 added / 0 removed** nodes:
4 token cases, 1 concurrency case, 1 multiline-input case, 8 transport and
Telegram combinations, 1 persistence failure, 1 startup failure, 2 failed
update paths and 1 real fresh-interpreter restart/repeatability case.
The manifest retains all 1429 base nodes, for **1448** total. The existing
concurrency case covers both threads and eight independent interpreters.

The final branch, including this evidence document, must be validated by
the VM's serialized canonical runner; its exact-SHA result and log path are
reported to architect after the run, without a subsequent commit:

```bash
env -u AGENT_NAME -u TENANT ssh h-lab@172.16.0.201 \
  '~/ci/run-suite.sh setup-agent/upgrade-token-daemons'
```

No tests, setup commands or daemons run on the live office host.
