# CI authority and local verification

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

The runner binds the suite to the h-mesh tree containing the directory from
which it was invoked. On every run it prints that tree, the resolved runner
module, and the directory handed to pytest. Quote those paths with the counts
when reporting suite evidence. If an installed `tools.run_tests` resolves from
a different clone than the invoking tree (for example, in a worktree using the
main clone's editable environment), the runner exits non-zero before collection
instead of silently testing the installed clone. Run it with an environment
installed from the tree being verified; do not treat that refusal as a reason
to fall back to a narrower pytest command.

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
