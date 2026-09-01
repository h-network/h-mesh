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

## Required repository rule

The stronger enforcement is an organization or enterprise ruleset targeting
`main` with both of these rules:

1. **Require a pull request before merging.**
2. **Require workflows to pass before merging**, selecting
   `.github/workflows/ci.yml` from the `main` branch.

That pins merge authority to the selected workflow, rather than trusting a
same-named status produced by whatever workflow a topic branch contains. If
required-workflow rulesets are unavailable, use branch protection as a weaker
fallback: require the **Authoritative full suite** check from GitHub Actions,
require branches to be up to date before merging, and require pull requests.
The fallback prevents an old workflow from satisfying the check accidentally,
but unlike a pinned required workflow, a future branch could deliberately copy
the check name.

Old branches that already contain the former push-on-every-branch workflow can
still display a green check from that old workflow. Repository changes cannot
retroactively alter workflow files in existing commits. Those checks are
non-authoritative even though they remain visible; only the required workflow
or check configured for `main` decides whether a change may merge.
