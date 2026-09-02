# conservation_harness.py

A measuring instrument for `core.channels`' custody chain, not a fix. Run
deliberately against a real Redis; **never wired into the pytest suite**
(confirmed: adding this file changes pytest's collected count by zero,
still true after each round of changes).

## What it establishes

That every envelope entering the custody chain (`popped -> forwarded ->
kick_started -> received -> opened`, `dead_lettered` as the terminal
alternative, plus `processing`/`opening`/`unresolved` on shapes that have
them) reaches exactly one terminal state, and that state is nameable by the
envelope's own `stream_id` -- never by count. Five scenarios:

1. Baseline: 8 named envelopes through the full happy path, catching
   identity crossover (payload A landing under stream_id B) a count-only
   check cannot see.
2. Process death in the pop-then-open (or claim-then-open) gap, via a real
   `SIGKILL` delivered at a deterministic moment -- not a modelled
   interleaving. Confirmed loss on main; confirmed recovery on the fixed
   shape.
3. A durable custody-transfer write failing mid-flight (a proxied RPUSH or
   Lua eval that raises).
4. Death *after* the opening transition: must land in tenant `unresolved`,
   nameable, and never be reopened by a successor (phases shape only).
5. A stopped-and-rehired agent name: must not inherit a predecessor's stuck
   custody, and must not erase a *different* identity's already-recorded
   unresolved evidence (phases shape only).

Every scenario that found loss was falsified by hand before being trusted:
the underlying fix (or the harness's own detection logic) was deliberately
broken, confirmed the scenario reports the failure, then restored.

## What it does NOT reach

Nothing about whether a real *opener* (lifecycle, OpenShell, tmux, board)
classifies its own failures correctly -- this harness only ever supplies
synthetic openers it controls itself (a `list.append`, a `print`-then-sleep).
That is a distinct property, verified by `opener_classification_harness.py`
instead; using this harness's clean transfer-mechanics result as evidence
about opener correctness would repeat the exact blind spot that harness
exists to close.

## Shape-detected, not hash-pinned

Every scenario calls `_custody_shape()`, which inspects
`core.channels._open_received`'s own signature at runtime (`legacy`: no
durable claim at all -- current main; `processing`: a durable claim but a
single opened/dead outcome; `phases`: processing -> opening ->
opened/dead/unresolved). Scenarios that only apply to a given shape
(4 and 5 above) report `SKIPPED`, not `held`, when run against a shape that
doesn't have the concept at all -- a skip is not a pass, and the harness
says so explicitly rather than implying coverage that doesn't exist.

This means it can run against **any hash** without modification and will
correctly detect and exercise whichever shape that hash implements. It has
already been rebuilt twice this session when the underlying implementation
changed in ways the shape-detector couldn't see through (BLPOP -> BLMOVE,
a renamed Lua script) -- shape detection covers structural changes to
`_open_received`'s signature; it does not cover every possible rewrite, and
a scenario that stops matching reality will report a `HARNESS ERROR`
("never reported the expected sync line" or similar) rather than a false
result, by design.

## Concurrent invocations are safe by default

Each invocation generates its own random pod/tenant namespace unless `POD`/
`TENANT` are set explicitly in the environment -- so two colleagues (or the
same person, twice) can run this against the same Redis at the same time
without their keys colliding. This was a real defect, not a theoretical
one: reviewer ran two copies of the documented command concurrently and
got loud, non-attributable failures (`RuntimeError: receive lost
ownership...`, `HARNESS ERROR: worker never reported CLAIMED`) from
namespace collision, not from the custody implementation. The last
scenario in the list runs this exact reproduction on every invocation --
two full concurrent copies of this harness under two independently
generated namespaces -- and requires both to report clean.

Setting `POD`/`TENANT` explicitly is still supported (an acknowledged
advanced option -- comparing two runs by hand against a known, inspectable
namespace, for instance) but two invocations sharing an explicit namespace
concurrently will collide exactly as before; that risk is now opt-in, not
the default.

## How to run

```bash
REDIS_URL=redis://127.0.0.1:6379/0 python h-app/tools/conservation_harness.py
```

Requires a real, reachable Redis -- refuses to run against nothing (the
property under test is about real durable writes). Exit code 0 means every
scenario held or was correctly skipped; nonzero means at least one
violation was found, or a scenario reported a harness error rather than a
result it couldn't support.

## When to run it

Before merging any change to `core.channels`' custody transfer logic
(`receive`, `_open_received`, `send`, the switch's egress-to-ingress
forwarding), or when verifying such a change is being reviewed. Nothing
runs it automatically; it stays deliberate on purpose (see the module
docstring) so a scenario finding a real defect doesn't train anyone to
ignore a red suite.
