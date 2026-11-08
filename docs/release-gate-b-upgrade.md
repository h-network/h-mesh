# Release gate B: FAIL

On 2026-11-04, the documented first native-host upgrade from current develop
`4d1f3889c37998f2de040d911147d3a89766a6d1` to release candidate
`bc001977508fb00caab17f2eb9540657be63bdc2` returned success while leaving
`API_TOKEN` absent and starting neither API nor inter-office relay.
Testing stopped on that defect, as required. This branch changes evidence
only; no product fix, second upgrade, or setup workaround was attempted.

## Step results

| Ticket step | Result | Evidence and limits |
| --- | --- | --- |
| 1. Fresh old container install and state | PASS | Real develop bootstrap; no token/API/relay; four board tickets, one unread API inbox envelope, enrolled API clients, and both client/relay unreplied records created by old `core.channels.send`. |
| 2. Container transition to candidate | NOT RUN | Disk guard refused candidate Compose up at 1.6 GB free, before the transition. The old throwaway was then removed. |
| 3. Preservation, token stability, watchdog pruning, no duplicate hires/daemons | NOT RUN for container | Old state was established, but no completed container transition or second rerun exists. Native host token/daemon migration failed; do not infer preservation or watchdog results from the old baseline. |
| 4. Old-data compatibility | NOT VERIFIED | Baseline registry used ordinary `office`/`api`/`tmux` values and tenant-scoped keys. Candidate consumption of that state was not reached. |
| 5. Fully isolated native host upgrade | FAIL | Real old setup followed by real `h-mesh-upgrade`: Git reached candidate, command returned `0` and printed healthy, token still absent, only three old daemons started. |

## Container baseline and disk stop

The fresh checkout used umask `022`; the wrapper's temporary parent and
logs were private. The only project was `h-mesh-setup-gate-b-proof`:

```bash
env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  H_MESH_IMAGE=h-mesh-setup-release-gate-b:develop-4d1f388 \
  AGENTS=architect,worker DEFAULT_CLI=codex \
  ./container/bootstrap.sh --pod setup-gate-b --tenant proof \
  --api-port 18210 --session-port 18211 --non-interactive
```

This built and bootstrapped develop through its normal container path.
Published health returned `000`; the generated office `.env` lacked
`API_TOKEN`. Process inspection found exactly one each of `core.service`,
`services.tmux_reconciler`, and `services.watchdog`, and zero API/relay
daemons.

Using normal `StartAgent` envelopes sent through `core.channels.send`, the
fixture enrolled `teleclient` and `relay` as API-type participants. Normal
office `add` commands created two tickets each for `architect` and `worker`.
A normal message to `teleclient` reached its Redis inbox stream. Messages
from both API participants to the lead opened real old-code unreplied
records. Registry, incarnation IDs, exact ticket records, and inbox content
were snapshotted inside the throwaway state volume.

The old build has no inter-office CLI. The fixture therefore seeded one
`offices` hash entry named `legacy`, with a loopback URL, participant
`remotelead`, and no token, to represent the legacy reserved-relay fallback
without enabling remote network traffic. It aged both unreplied timestamps
by one hour to make a later watchdog check observable. These were explicit
test-data writes, not `.env` edits. The watchdog check never ran on the
candidate.

Initial VM disk was 6.3 GB free; it fell to 3.5 GB on lock acquisition.
An early wrapper crossed into candidate building while the new disk rule
was arriving; that own-project build was cancelled and cleaned. A revised
attempt enforced a hard 2.5 GiB guard before every subsequent build/up.
Architect authorized unused builder-cache cleanup and borrowing
`h-mesh:release-gate-a-bc00197` with `--skip-build`. Cache-only
`docker builder prune -f` runs reported 2.615 GB and 2.589 GB reclaimed.
The borrowed image was never retagged or removed.

On the guarded attempt, after the old baseline passed, the guard saw 1.6 GB
free and refused this planned command before executing it:

```bash
env -i HOME="$HOME" PATH=/usr/local/bin:/usr/bin:/bin \
  H_MESH_IMAGE=h-mesh:release-gate-a-bc00197 H_MESH_ALLOW_PLAINTEXT=1 \
  ./container/bootstrap.sh --pod setup-gate-b --tenant proof \
  --api-port 18210 --session-port 18211 --non-interactive --skip-build
```

All container teardown used explicit `-p h-mesh-setup-gate-b-proof`, the
matching Compose files and generated office env file, and
`down -v --remove-orphans`. Only the owned develop image tag was removed.
The private checkout/state were deleted. Operator and gate-A projects and
their volumes were not targeted.

Baseline/guard log:
`/home/h-lab/ci/logs/release-gate-b-20261104T172041Z.log`.

## Native host reproduction

The README's **Upgrading and restarting daemons** section documents
`h-mesh-upgrade`: `git pull --ff-only`, reinstall, then restart daemons.
This probe exercised that actual first-transition process, rather than
checking out candidate and launching its upgrader in a fresh interpreter.

Sandbox root was `/home/h-lab/ci/release-gate-b-host.gs27e9ph` (called `$R`
below). The probe created a fresh clone of develop under `$R/repo`, a fresh
`python3 -m venv $R/venv`, and installed the old checkout editable with
`pip install --no-cache-dir -e $R/repo`. Its own throwaway local `develop`
branch tracked `origin/architect/release-candidate`, so the documented
ordinary pull would advance from old to the intended release candidate.
No source files were edited.

All host child processes received an explicitly constructed environment:

```text
HOME=$R/home
PATH=$R/venv/bin:/home/h-lab/ci/bin:/usr/local/bin:/usr/bin:/bin
USER=h-lab LOGNAME=h-lab LANG=C.UTF-8 TERM=dumb
POD=setup-gate-b-host TENANT=hostproof AGENTS=
H_MESH_STATE_DIR=$R/state H_MESH_RUN_DIR=$R/run H_MESH_WORKDIR=$R/workdir
TMUX_TMPDIR=$R/tmux TMUX_SOCKET=$R/tmux.sock TMUX_SESSION=hostproof
REDIS_URL=redis://127.0.0.1:50591/0 API_BIND=127.0.0.1 API_PORT=0
```

No ambient credentials, Telegram settings, or token values were inherited.
The host used manual-hire mode with an empty roster, avoiding any external
model/agent process. Redis ran from the installed CI executable with
loopback port `50591`, private data directory and `$R/redis.sock`, no RDB
snapshots or AOF. A matching server `run_id` over its unique Unix socket and
TCP endpoint authenticated that endpoint before setup used it.

Commands under that environment:

```bash
$R/repo/setup.sh --host --pod setup-gate-b-host --tenant hostproof \
  --redis-url redis://127.0.0.1:50591/0 --session hostproof \
  --tmux-socket $R/tmux.sock --tmux-tmpdir $R/tmux --venv $R/venv \
  --skip-deps --skip-install --non-interactive

$R/venv/bin/h-mesh-upgrade --pod setup-gate-b-host --tenant hostproof \
  --redis-url redis://127.0.0.1:50591/0 --session hostproof \
  --tmux-socket $R/tmux.sock --tmux-tmpdir $R/tmux --venv $R/venv
```

The old install passed with no token and only the expected three daemons.
The upgrade printed:

```text
✓ Upgrade complete, daemons are healthy.
```

Its measured result was:

```json
{"upgrade_exit":0,"old_sha":"4d1f3889c37998f2de040d911147d3a89766a6d1","resulting_sha":"bc001977508fb00caab17f2eb9540657be63bdc2","api_token_present":false,"daemon_pidfiles":["switch","tmux_reconciler","watchdog"]}
```

The probe stopped immediately with verdict exit `10`. No second upgrade
was attempted. Cleanup authenticated/stopped only `$R/run` daemon
identities, stopped the child private Redis and own tmux socket, deleted
`$R`, and reaped remaining descendants through the unique transient unit
`setup-release-host-b-2955504.service`. It never used the VM's Redis port
6379 or a fixed API port.

Host evidence log:
`/home/h-lab/ci/logs/release-gate-b-host-20261104T172612Z.log`.

## Cause and remaining work

`services.upgrade` imports `services.daemons` before it pulls/reinstalls.
Those Python objects, including the old daemon roster, stay loaded for the
rest of the first self-upgrade. Future child interpreters see candidate
source, but the parent still selects only the old three daemons.
Separately, this upgrade path never calls setup's token-generation or
persistence logic. A source checkout reaching candidate therefore does
not establish its new bootstrap invariants.

The candidate contains a comment warning about the first-transition import
boundary; that warning cannot alter an upgrader already running from old
develop. A later fix must address the actual first upgrade, not only a
second invocation imported from candidate.

No canonical suite was started after the verified defect: the ticket
required STOP and the lead accepted the gate as FAIL. The supplied base
certificate was 1429/1429; this evidence-only branch does not claim a new
suite run or any completed container/watchdog migration result.
