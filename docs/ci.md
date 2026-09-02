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
`main`. It does **not** run on pushes to topic branches. So a commit sitting on
a topic branch has no check run attached to it at all. Turn on required status
checks for `main` while merging by direct push, and every direct push is
rejected forever — not because the code fails, but because the required check
can never come into existence for a commit that has not already landed on
`main`. Requiring a pull request has the same effect by design: it ends direct
pushes.

That is the actual trade. A mechanical gate here is not weak, it is
*load-bearing*: enabling one means adopting pull requests, or restoring CI on
topic-branch pushes so commits can carry a check before they land. Both are
real options and neither has been taken. Until one is, the absence of a gate is
a standing decision to rely on process instead — made knowingly, and revisitable
the moment someone is willing to pay for one of those two changes.

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

   ⚠ State which tree the run was in. `python -m tools.run_tests` resolves its
   own repository root from the installed module's path, so invoking it from a
   git worktree or a second checkout runs the suite against the *install* clone
   while you stand in the branch — exiting 0 with a believable count. Confirm
   with `python -c "import tools.run_tests as m; print(m.REPO_ROOT)"` and check
   the path is the tree you meant to test.

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
