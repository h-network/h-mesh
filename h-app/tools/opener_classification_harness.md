# opener_classification_harness.py

A verifier for opener classification correctness, not a fix. Run
deliberately against a real Redis; **never wired into the pytest suite**
(confirmed: adding this file changes pytest's collected count by zero).

## What it establishes

`conservation_harness.py` proves the custody chain's transfer mechanics
GIVEN a raise or return the harness itself dictates. It says nothing about
whether a REAL opener sorts its own failures correctly -- whether it ever
claims a failure is a PROVEN pre-effect rejection (`DeadLetter`) when the
failure could just as easily mean the effect already happened and the
response was lost. That is what this instrument checks, driving the real
decorated/guarded opener code (never a synthetic raise standing in for the
opener's own classification decision -- that would repeat the exact blind
spot this instrument exists to close).

Four scenarios, one per opener family:

1. **lifecycle** (`lib/agentlifecycle/lifecycle.py` + `modules/office/
   port.py`): a real prior write commits, then an injected failure on a
   later write simulates an outcome-unknown continuation.
2. **openshell** (`modules/openshell/port.py`): a fake client whose
   `exec_sandbox` *provably runs* (recorded, certain evidence) before
   raising, simulating a response lost after real execution.
3. **board** (`lib/board_interaction.py`): a proxy performs the real RPUSH
   first -- durable, independently checkable via `llen()` -- then raises,
   simulating a response lost after a write that landed.
4. **tmux** (`modules/tmux/port.py`): the real `message_opener` driven with
   only the tmux process boundary faked (`list_windows`/`submit_text`),
   simulating "paste-buffer already succeeded, the submitting send-keys
   then failed".

Scenarios 1-3 assert by DESTINATION and IDENTITY on shapes where
`unresolved` exists (a stream_id reached `dead` vs `unresolved`), never by
exception type. Scenario 4 (and the underlying mechanism check in 1-3)
additionally asserts by exception TYPE, deliberately -- see below.

Every scenario is falsified by hand, in the direction that matters for its
own claim: a scenario reporting a defect is confirmed to flip clean once
the real fix is applied; a scenario reporting "clean" (tmux) is confirmed
to catch a *manufactured* version of the defect it claims is absent, not
just assumed clean from reading the code.

## What it does NOT reach

`tmux` and `board` were established directly by reading every `DeadLetter`
call site in their modules and grepping every use of the exception types
that could reach one -- not sampled. If a future call site is added without
updating this harness, it is not automatically covered. Nothing here
verifies the `api` opener or any opener not listed above; that is an
explicitly open question, not assumed answered.

## Shape-awareness, and a note on WHICH assertion applies where

Scenarios 1-3 check `core.keys.receive_unresolved_key` for `None` first: on
a shape with no `unresolved` sink at all (current main), "reached `dead`"
is that shape's entire single-outcome custody design, not a specific
classifier decision -- these scenarios report `SKIPPED`, not `held`, on
that shape, established by actually running there and observing the
structural reason, not assumed. Scenario 4 (tmux) does not depend on
`unresolved` existing at all -- it asserts the exception TYPE that escapes
`message_opener` directly, which is meaningful on any shape, since tmux's
own `DeadLetter` sites are unrelated to which custody shape is installed.

It runs against **any hash**, same as `conservation_harness.py` -- this has
already been exercised across the exact hash a defect was found at
(`5815118`) and the exact hash the fix landed at (`212e9d6`), with no
changes to the harness needed between them, because both scenarios attach
to stable module boundaries (`deliver_office`, `deliver_openshell`,
`add_ticket`, `message_opener`) rather than to custody internals.

## Concurrent invocations are safe by default

Same fix as `conservation_harness.py`, same reason: each invocation
generates its own random pod/tenant namespace unless `POD`/`TENANT` are
set explicitly. `--only=NAME` runs a single named scenario in isolation
(`lifecycle`, `openshell`, `board`, `tmux`, or `concurrency`) -- the
concurrency self-check uses this itself, spawning two separate
`--only=lifecycle` subprocesses (not threads: an earlier in-process
threaded version of this check was built first and failed intermittently,
traced to `contextlib.redirect_stdout` mutating the single process-global
`sys.stdout` across threads -- see that scenario's own comment for the
full account) and requiring both to report clean, since `board`'s
legitimate current failure would make "any scenario failed" useless as a
collision signal for a full concurrent run.

## How to run

```bash
REDIS_URL=redis://127.0.0.1:6379/0 python h-app/tools/opener_classification_harness.py
```

Requires a real, reachable Redis. Exit code 0 means every scenario held or
was correctly skipped; nonzero means a real misclassification was found.

## When to run it

Before merging any change to an opener's own exception handling --
lifecycle's `_record_lifecycle`/`_lifecycle_opener`, OpenShell's
`exec_sandbox`/whatever replaces `guarded()`, board's `add_ticket`, or
tmux's `message_opener`/`command_opener`/`attachment_opener` -- or when
reviewing such a change. Stays out of the suite for the same reason
`conservation_harness.py` does.
