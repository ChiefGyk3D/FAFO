# Contributing

## Review and merge

- The merge gate is the aggregate `CI green` check. Merge goes through a head-pinned watcher
  that requires it. One PR merges at a time, and the next PR is re-checked for conflicts
  before it merges.
- Copilot code review does not reliably re-review pushes on its own. On this repo it did not
  review most final commits within a 1 to 2 minute wait. For every push to a PR, request a
  review explicitly: add Copilot as a reviewer from the PR page (Reviewers sidebar), or call
  the GitHub API requested-reviewers endpoint for the Copilot reviewer bot. Some assistant
  setups expose this as a tool (for example `request_copilot_review` in the GitHub MCP
  server), but no tool is required.
- The watcher waits for a review of the final head commit: up to 5 minutes for docs-only PRs,
  7 minutes for ordinary PRs, and 10 minutes for security-sensitive PRs. If no review
  arrives, that is a notice, not a pass.
- Security-sensitive PRs get the 10 minute wait and a new review request after each fix
  push. These are PRs that touch redaction, parsing of untrusted input, file or process
  handling, tokens, or CI workflows. Answer their threads with evidence, then resolve them.
  Repeated rounds are normal: the redaction and transcript PRs took four to six rounds, and
  each round found a real issue.
- A reply to a review thread states what changed, the commit, and the test that proves it.
  A test fix is shown to fail when the code change is reverted.
- An independent adversarial security review, by a separate tool and not Copilot, covers the
  already merged security modules. It is tracked as Task 18 in
  `docs/superpowers/plans/2026-11-27-fafo-plan.md` and runs after the feature PRs merge. It
  reviews current main, not PR diffs. Modules named in the plan: `redact.py`, `ghapi.py`,
  `sshro.py`, `procutil.py`, `config.py`, `registry.py`, `cli.py`, `store.py`, `collector.py`,
  `sources/claude.py`, `adapters/safo.py`, `attribution.py`.

## Process facts

- Run targeted tests locally. CI runs the full suite.
- Timing tests use CPU time or readiness waits, never fixed sleeps.
- No secrets in argv, logs, the repo or CI.
- Keep `Co-Authored-By` trailers as written.
- Docs and the README contain no em dashes; `tests/test_no_em_dash.py` enforces it.
