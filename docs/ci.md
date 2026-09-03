# CI authority and local verification

## Legacy-name guard

CI scans every tracked UTF-8 text file in the repository and rejects an
unreviewed reference to the predecessor project's identity. Non-UTF-8 tracked
files are treated as binary and excluded; repository source and documentation
must be UTF-8 to receive this protection. A failure prints the file, line
number, and complete offending line so it can be corrected without reproducing
CI locally.

A required compatibility identifier may be allowed on its own line by adding a
source comment containing `legacy-name-allow`, then a colon and the exact
uppercase identifier. The marker must name an identifier present on that line;
it does not allow other occurrences or longer identifiers containing the same
text. Use it only where removing the legacy identifier would remove
compatibility coverage, and treat every new marker as a reviewed policy
exception rather than a general suppression.

The authoritative GitHub Actions workflow is `.github/workflows/ci.yml` from
`main`. It runs for pull requests, merge groups, and pushes to `main`. New topic
branches do not run push CI: a green branch-push check is not evidence that the
branch passed the current suite.

Before asking for review or merge, run the same canonical suite locally from an
installed development environment:

```bash
python -m tools.run_tests
```

Report both the collection count and the pass count. Do not substitute
`pytest tests/`, `unittest discover`, or another explicit subtree; those
commands collect less than the complete suite.

The complete-suite runner accepts no additional pytest arguments. Paths,
selectors, verbosity flags, and configuration overrides are all refused because
the harness must collect and execute one identical, reviewed node set. Use bare
pytest directly for focused development runs, then use the argument-free command
above for merge evidence.

The reviewed node set is checked in at `h-app/tools/test_nodeids.txt`. A plugin
inside the pytest process compares its selected items to that manifest before
the first test runs, then records actual runtest reports. It issues the suite
attestation only after every expected node has reached a terminal test outcome.
The outer runner returns success and issues its public certificate only after
pytest exits zero and the nonce-bound attestation matches that invocation and
manifest. A zero exit with an absent, partial, stale, or mismatched attestation
is a failure. Selection alone and absence of a reported error are not execution
evidence.

Execution accounting requires a non-skipped call-phase report. A setup skip
produces no call report; an in-body `pytest.skip()` produces a skipped call
report. Neither counts as execution. The harness distinguishes `not executed:
skipped` (including pytest's reason) from `not executed: no call-phase report`,
returns non-zero, and issues no certificate.

This makes the required environment a hard prerequisite for merge evidence. In
particular, tests gate on Redis during their call phase, so a machine without
reachable Redis cannot produce a suite certificate. Start the required service
and rerun; the diagnostic enumerates the exact skipped nodes. A skip failure is
not a manifest mismatch, and regenerating the manifest cannot fix it.

Bare focused pytest can exit zero while reporting skips. That is valid pytest
behavior, but it is not merge evidence: it proves neither that the skipped test
body ran nor that its guarantee held. Use bare pytest for focused development
feedback and the canonical runner for merge evidence. A terminal call-phase
outcome is execution accounting; it is not by itself proof that the test's
assertions adequately verify their intended guarantee.

The evidence is the pair of runner exit status zero **and** its post-validation
certificate line. Pytest and the tests share stdout with the runner, so child
code can print identical text; line presence alone is never evidence. Every
human verifier, script, and CI job consuming this result must check the runner
process's exit status. Grepping stdout for the certificate sentence is not
verification. An unenumerated termination that prevents the outer runner from
completing its check cannot produce the success pair, although the harness does
not diagnose why the process ended.

The runner removes external `PYTEST_ADDOPTS` and `PYTEST_PLUGINS`, disables
ambient plugin autoloading, overrides repository `addopts`, and rejects parsed
zero-runtest modes. This prevents a collect-only or setup-only invocation from
being reported as a successful suite.

Any test addition, removal, or rename makes the harness fail with separately
labeled `added` and `missing` node IDs. `missing` means a previously reviewed
test would not execute; `added` means a new or renamed test has not yet been
accounted for in the manifest.

Those diagnostics compare current collection with the checked-in manifest
snapshot inherited from `main`, not with the branch's latest commit. Until the
branch deliberately updates that snapshot, a long-open branch reports its
entire cumulative test change across every rework. An unexpectedly large count
therefore does not by itself prove that the manifest is broken or stale. It can
also result from a stale, wrong-base, or corrupted manifest, so every `added`
and `missing` entry still requires accounting regardless of the totals.

A `missing` entry can be legitimate: a test may have been renamed, or its
coverage may have been absorbed into another test that asserts strictly more.
Those cases should account for the old node ID and its replacement explicitly.
A deliberately deleted test needs its own coverage rationale; an unexplained
missing entry is evidence of possible lost coverage, not routine drift.

Regenerating the file mechanically clears the mismatch whether or not anyone
reviewed it. **Account first, regenerate second.** Explain every missing and
added entry in independent review and preserve that accounting in the manifest
update's commit message. Only then regenerate the manifest from the repository
root and review its diff before running the harness again:

```bash
PYTHONPATH=h-app python -m pytest --collect-only -q \
  | sed -n '/^h-app\/.*::/p' > h-app/tools/test_nodeids.txt
```

The harness makes node-set drift visible; it cannot prove that a branch author
reviewed or honestly accounted for a regenerated manifest. In this repository,
independent review of the manifest diff is a procedural trust boundary because
there is no protected review or signing mechanism that the branch author cannot
also change.

The runner binds the suite to the h-mesh tree containing the directory from
which it was invoked. On every run it prints that tree, the resolved runner
module, and the directory handed to pytest. Quote those paths with the counts
when reporting suite evidence. If an installed `tools.run_tests` resolves from
a different clone than the invoking tree (for example, in a worktree using the
main clone's editable environment), the runner exits non-zero before collection
instead of silently testing the installed clone. Run it with an environment
installed from the tree being verified; do not treat that refusal as a reason
to fall back to a narrower pytest command.

Before collection, the runner starts a fresh child with the same sanitized
environment that pytest and test-spawned processes will inherit. That child
must import `services.daemons` from the named tree. A missing or cross-tree
import fails immediately with the interpreter, module path, expected tree, and
editable-install command instead of surfacing later as a product-test failure.
The runner deliberately does not inject the checkout into `PYTHONPATH`: doing
so would let source imports hide a broken installation. The diagnostic
instructs the caller to create and install into the checkout's own `.venv`; it
never tells you to install into the invoking interpreter, which may be a shared
runtime you do not own. The runner only prints these remediation commands and
does not execute them. Follow those tree-local commands, then rerun the
canonical suite. A manually
supplied `PYTHONPATH` that points at the named tree can satisfy the child-import
check, but that only demonstrates the import property for that invocation; the
documented production-equivalent setup remains an editable install from the
tree under test.

The complete suite is expected to pass both from an editable install of the
named tree and when a caller explicitly supplies that tree's `h-app` directory
on `PYTHONPATH`. These are distinct environment contracts: the latter proves
only that invocation's first-party import path and may resolve third-party
dependencies from a different environment. CI uses the editable-install form.
Use an owned environment installed with `.[test]` when reproducing CI or
reporting results intended to be comparable with CI; a `PYTHONPATH` run is
useful source-tree evidence but is not a reproduction of CI's dependency
environment.

## There is no enforced merge gate, and that is deliberate

Nothing in the repository prevents a change from reaching `main` without
passing CI. This is a decision, not an oversight, and it is recorded here so
that nobody mistakes the absence of a rule for a rule nobody got around to
configuring.

As of this writing the repository has **no rulesets and no branch protection on
`main`** — `GET /repos/{owner}/{repo}/rulesets` returns an empty list and
`GET /repos/{owner}/{repo}/branches/main/protection` returns 404. Nothing is
being bypassed. Nothing is configured.

⚠ Do not justify that state by claiming GitHub's rules cannot gate a direct
push. They can. Branch rulesets and protected-branch rules target *updates to
the branch*, not merely actions taken through the pull-request UI: required
status checks must pass before a collaborator can change the targeted branch,
and "require a pull request before merging" rejects direct pushes outright
unless an actor has bypass. An earlier draft of this document asserted the
opposite and was wrong.

The real obstacle is our own workflow triggers, and it is specific:
`.github/workflows/ci.yml` runs on `pull_request`, `merge_group`, and pushes to
`main`. It does **not** run on pushes to topic branches. So under the current
configuration a commit sitting on a topic branch acquires no check run
automatically. Turn on required status checks for `main` while merging by
direct push, and the ordinary direct-push workflow stops working — not because
the code fails, but because nothing in the present setup produces the required
context for a commit before it lands on `main`. Requiring a pull request has
the same effect more directly: it ends direct pushes by design.

Note the boundary of that claim. It is about *this* workflow configuration, not
about what GitHub permits. A required context is satisfied by a check run **or
a commit status**, and the commit-status API lets any sufficiently permitted
user or integration create a status for an arbitrary SHA. Adding a
`workflow_dispatch` trigger would be another route. So a topic commit *can* be
made to carry the required context without changing `ci.yml` at all — the gate
is not technically unreachable, and this document should not be read as
claiming it is.

That is the actual trade, and it is about which operating model we want rather
than what is possible. The practical routes are adopting pull requests, or
restoring CI on topic-branch pushes so commits carry a check before they land;
a hand-posted commit status would also satisfy the rule, but a gate whose
required evidence a human can mint on demand is a gate in name only, so it is
not offered here as a serious option. None of these has been taken. Until one
is, the absence of a gate is a standing decision to rely on process instead —
made knowingly, and revisitable whenever someone is willing to pay for one of
those changes.

What gates a merge here today is procedural, not mechanical:

1. **An independent review before merge**, raised as a ticket against a
   specific commit hash. A review verdict attaches to that hash and does not
   carry forward to later commits on the same branch. A branch **must remain
   frozen** while a review ticket against it is open. This is a rule, not an
   observed property: heads have moved under open tickets, and the required
   response is to re-raise the review against the new head rather than stretch
   the old verdict to cover it.
2. **A full-suite run reproduced by whoever merges**, not accepted as a
   reported number from the branch author. A focused subset passing is
   evidence about the change; only a full run on the merge result is evidence
   about the suite.

   ⚠ State which tree the run was in. `python -m tools.run_tests` prints the
   invoking repository tree, the resolved runner module, and the cwd handed to
   pytest on every run. If the runner module belongs to a different checkout,
   it refuses with a non-zero exit before collection rather than producing a
   believable pass number about the wrong tree.

Both steps are human discipline and can be skipped by anyone who decides to
skip them. That is the honest cost of not having a mechanical gate, and it
should be weighed rather than forgotten.

⚠ If this project ever adopts pull requests, revisit this section first. Both
rules become straightforward to enable once merges actually flow through
PRs — a pull request gives each commit a check to satisfy, which is the piece
missing today. The preferred form is then an organization or enterprise
ruleset targeting `main` that requires a pull request and pins
`.github/workflows/ci.yml` **from the `main` branch** as a required workflow.
Pinning the workflow to `main` is the part that matters: it stops a topic
branch from satisfying the requirement with a same-named status produced by a
workflow the branch itself contains. Where required-workflow rulesets are
unavailable, branch protection requiring the **Authoritative full suite** check
plus up-to-date branches is a weaker fallback — it prevents an old workflow
from satisfying the check accidentally, but a future branch could still
deliberately copy the check name.

## Green checks that mean nothing

Old branches that already contain the former push-on-every-branch workflow can
still display a green check from that old workflow. Repository changes cannot
retroactively alter workflow files in existing commits. Those checks are
non-authoritative even though they remain visible; only the required workflow
or check configured for `main` decides whether a change may merge.
