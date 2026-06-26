# Fix issue #21 — /away false PR-create failures + empty away/issue-N branches

## Problem
`_process_issue` marks a 0-commit `away/issue-N` as a hard **failure** even when the
work was actually delivered elsewhere (direct commit to main, merged PR, retry), and
leaves the empty branch/worktree behind, blocking idempotent retries.

## Plan
- [ ] Add `_issue_delivered(repo, number)` — artifact check: issue CLOSED & not NOT_PLANNED.
- [ ] In `commits == 0` path: delivered ⇒ `done` + `delivered_elsewhere`; else ⇒ `failed`.
- [ ] Add `_gc_away_worktree(local, wt, branch)` (worktree remove + prune + branch -D, best-effort).
- [ ] GC the empty shell in the `commits == 0` path (both delivered & failed).
- [ ] GC-and-retry-once around `worktree add` so a leftover shell never blocks `/away`.
- [ ] Tests: delivered-elsewhere ⇒ done+no-pr+gc; not-delivered ⇒ failed+gc; add-retry idempotency.
- [ ] Run away + nirmana suites; commit, push, close #21.

## Review
(filled after execution)
