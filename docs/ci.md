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

GitHub's merge-gating rules — "Require a pull request before merging" and
"Require workflows to pass before merging" — only take effect on pull-request
merges and merge queues. They do not evaluate a direct push. We merge to `main`
by direct push and do not use pull requests, so enabling those rules would gate
a path we never take while leaving the path we do take untouched. Turning them
on would produce the appearance of enforcement without the substance, which is
worse than no rule at all: a reader of the settings page would conclude that
`main` is protected when it is not.

What actually gates a merge here is procedural, not mechanical:

1. **An independent review before merge**, raised as a ticket against a
   specific commit hash. A review verdict attaches to that hash and does not
   carry forward to later commits on the same branch. A branch stays frozen
   while a review ticket against it is open; if it moves anyway, the review is
   re-raised against the new head rather than stretched to cover it.
2. **A full-suite run reproduced by whoever merges**, not accepted as a
   reported number from the branch author. A focused subset passing is
   evidence about the change; only `python -m tools.run_tests` on the merge
   result is evidence about the suite.

Both steps are human discipline and can be skipped by anyone who decides to
skip them. That is the honest cost of not using pull requests, and it should be
weighed rather than forgotten.

⚠ If this project ever adopts pull requests, revisit this section first. The
two rules above become worth enabling the moment merges actually flow through
PRs, and at that point the preferred form is an organization or enterprise
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
