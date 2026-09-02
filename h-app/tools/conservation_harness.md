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
6. Retirement itself (`lib.agentlifecycle.lifecycle.stop_agent`): must not
   turn admitted custody into absence, AND must move each seeded identity to
   EXACTLY ONE terminal location, never zero, never more than one. Seeds one
   distinct identity in each of `ingress`, `processing`, and `opening`, plus
   one genuinely completed `opened` receipt, calls the real `stop_agent`,
   then counts every occurrence of each seeded identity across every
   terminal custody sink this file actually reads and classifies (tenant
   `undeliverable`, tenant `unresolved`, the target's own `opened`, the
   target's own `dead`, and tenant `retired_inbox` -- see below for what
   makes that list exhaustive at this hash, what would invalidate it, and
   why "reads and classifies" replaced "reasons about and skips" for
   `retired_inbox` specifically) combined (see `_stream_id_occurrences`,
   which keeps every parsed record rather than indexing by identity -- a
   dict/set collapses two occurrences into one and cannot prove
   "exactly once" at all, only "at least one"). Absence, more-than-one, and
   wrong-sink-with-the-right-count all fail distinctly. Then rehires the
   same name and asserts the successor inherits none of it and the
   retirement evidence is byte-identical before and after (phases shape
   only, and only where `stop_agent`/`receive_undeliverable_key` exist on
   the tree under test).

Every scenario that found loss was falsified by hand before being trusted:
the underlying fix (or the harness's own detection logic) was deliberately
broken, confirmed the scenario reports the failure, then restored.

⚠ Scenario 6's exactly-once claim was itself falsely green once. Reviewer
found that an earlier version built it on `_undeliverable_stream_ids`/
`_unresolved_stream_ids` (dicts keyed by stream_id) and `_stream_ids_in` (a
set) -- each silently collapses duplicate occurrences of the same identity
into one, so a misattributed duplicate followed by a correctly-attributed
one would collapse to the good record, reporting clean while hiding both
the duplication and the bad record. Confirmed by falsifying that exact
shape against the pre-fix code (silently clean) and the fixed code (caught,
reported `DUPLICATED`).

⚠ A second round found the fix itself incomplete: the occurrence scan only
covered the three sinks this scenario's own happy path names (undeliverable/
unresolved/opened), omitting the target's own `dead` list -- a real
terminus `core.channels.py` dead-letters straight into, and one
`stop_agent`'s own Lua never touches (not in its `KEYS` list at all), so an
identity duplicated into `dead` was invisible. Reviewer's exact
reproduction -- append a genuine undeliverable record's raw envelope
directly to the target's own `dead` list after a normal retirement --
reported clean before this fix. Added `dead` to the scan; confirmed the
same reproduction is RED against the pre-fix code and GREEN against this
one. This is the third time this file's own detection logic has been the
false-negative, not the thing it was pointed at -- see
`_decode_evidence_envelope`'s docstring for the first.

⚠ WHAT MAKES THE FOUR-SINK LIST A CHECKABLE CLAIM, NOT A FOURTH GUESS: after
the `dead` gap, the question was whether the sink list is complete or just
hasn't been caught missing a fifth yet. Traced rather than guessed: exactly
TWO Lua scripts in this whole tree ever move a raw envelope out of
processing/opening custody -- `_TRANSFER_RECEIVE_CUSTODY` (`core/channels.py`)
and `_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA` (`lib/agentlifecycle/lifecycle.py`).
Every call site of both was read completely. `_TRANSFER_RECEIVE_CUSTODY` has
four call sites (three inside `_open_received`, one in `receive()`'s
successor-recovery sweep), naming exactly three destinations across all of
them: `dead`, `unresolved`, `opened`. `_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA`
has one call site (`stop_agent`), naming exactly two: `undeliverable`,
`unresolved`. The union is the same four this scenario scans, and no fifth
appears anywhere in either script's call sites.

That trace is exhaustive AT THIS HASH -- it is not something this instrument
derives itself, and that is the limit to state plainly rather than imply
past: **this scenario assumes there are only two scripts that ever move a
raw ENVELOPE (something carrying a `stream_id`, sourced from KEYS[3]/[4]/[5]
processing/opening/ingress) out of custody.** If a third one is added, or
either script grows a new destination for that kind of record, this scan
goes stale exactly the way it did three times already, and nothing here
would catch that drift automatically. Deriving the sink list from the
source itself (AST-reading both scripts' call sites, the way
`_custody_shape()` already inspects `_open_received`'s signature and
`_transfer_script()` resolves the Lua constant by identity) would close
this permanently -- recorded as a real, separate follow-up, not started
here.

⚠ THE INVALIDATING CONDITION ARRIVED IMMEDIATELY, AND THE FIRST RESPONSE TO
IT WAS ITSELF THE FOURTH FALSE-CLEAN. `_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA`
gained a real fifth RPUSH destination the same day this section was
written -- `retired_inbox_key`, conserving an api-type agent's `inbox`
STREAM content on retirement. The first fix reasoned that retired_inbox
records can never carry a `stream_id` (a different top-level shape,
`{entry_id, fields}` vs `{envelope}`) and used that reasoning to justify
**never reading the key at all**. Reviewer's finding: that is not a parser
correctly rejecting an unparseable shape -- it is a sink this scenario
never looked at. A genuine envelope record duplicated into `retired_inbox`
(reviewer's reproduction: copy one real undeliverable record there after a
normal retirement) was therefore invisible, not correctly excluded, and
the scenario reported clean. "We do not read it" and "we read it and
deliberately treat this recognized shape as contributing no identity" are
different claims; only the second is an enforced boundary, and only the
second is what's here now.

`retired_inbox` IS now read on every run, via `_retired_inbox_occurrences`,
and every record is classified into exactly one of three outcomes: the
recognized non-envelope shape (`entry_id` + `fields`, no `envelope`)
contributes no identity, correctly; an envelope-bearing record (has
`envelope`) is decoded and its `stream_id` DOES count as a real occurrence,
so a duplicate landing here -- by any cause, not just the one reproduced --
triggers `DUPLICATED` like any other sink; anything else (fails to parse,
or matches neither shape) is a SCHEMA ANOMALY, reported as a failure rather
than silently skipped. A real, valid retired-inbox CONTROL case is part of
the scenario's own setup now (two genuine `XADD` entries into the target's
own `inbox`, with the target registered as an `api`-port agent), not a
script run once by hand and left uncommitted -- the scenario asserts the
real `stop_agent` actually conserved them (`>= 2` records found) before
trusting anything else it measured, and asserts `retired_inbox`'s own
evidence is byte-identical across the same-name-rehire check too, matching
every other tenant evidence key.

Falsified three ways by hand, each confirmed RED against the pre-fix hash
and GREEN against this one: a genuine record duplicated into
`retired_inbox` (reviewer's exact reproduction), and a record matching
neither recognized shape at all. If a FUTURE destination ever carries a
`stream_id` under a name this scan doesn't already read, that is still the
condition that invalidates this trace -- but "arrives in a sink this file
already reads" is a smaller, checked failure mode now, not a repeat of
"never looked."

## What it does NOT reach

Nothing about whether a real *opener* (lifecycle, OpenShell, tmux, board)
classifies its own failures correctly -- this harness only ever supplies
synthetic openers it controls itself (a `list.append`, a `print`-then-sleep).
That is a distinct property, verified by `opener_classification_harness.py`
instead; using this harness's clean transfer-mechanics result as evidence
about opener correctness would repeat the exact blind spot that harness
exists to close.

Scenario 6 does not independently re-derive `stop_agent`'s own dedicated
harm tests -- hostile non-UTF-8 raw bytes surviving hex-encoding exactly,
and the read-only CLI's malformed-record handling being non-consuming
(`tests/test_agentlifecycle.py::test_undeliverable_record_preserves_non_utf8_raw_exactly`,
`tests/test_office_cli.py::test_undeliverable_malformed_record_is_reported_without_consuming`).
Those were run directly against a real Redis on the same exact hash this
scenario was verified against, and passed, but that is the suite's job, not
this instrument's; duplicating it here would test the same claim twice
without widening what's covered.

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
forwarding), **or to `lib.agentlifecycle.lifecycle`'s retirement custody
handling** (`stop_agent`, `_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA`) -- or when
verifying either kind of change is being reviewed. Reviewer's finding: an
earlier version of this section named only `core.channels`, so a
lifecycle-side maintainer adding a new custody destination could reasonably
never encounter this instrument's sink-list assumption at all. Both
`_TRANSFER_RECEIVE_CUSTODY` and `_REMOVE_MEMBERSHIP_AND_OWN_LEAD_LUA` also
carry a short comment pointing back here, so a maintainer editing either
script directly (not just this doc) hits the warning too. Nothing runs it
automatically; it stays deliberate on purpose (see the module docstring) so
a scenario finding a real defect doesn't train anyone to ignore a red
suite.
