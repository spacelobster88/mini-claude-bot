"""Unit tests for the /away issue-automation engine.

All external effects (gh, git, harness loop, status poller, sleep) are mocked;
no real network/git/subprocess runs.
"""

import json
import threading

import pytest

from backend.services.away_automation import AwayAutomation


# ── fixtures / helpers ──────────────────────────────────────────────


def _issue(number, title="Fix thing", body="do it", labels=None, assignees=None):
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": n} for n in (labels or [])],
        "assignees": [{"login": a} for a in (assignees or [])],
    }


@pytest.fixture
def repos_file(tmp_path):
    def _write(entries):
        p = tmp_path / "away-repos.json"
        p.write_text(json.dumps(entries))
        return p

    return _write


def make_engine(
    tmp_path,
    *,
    gh_issues=None,
    poll_status=None,
    launch_loop=None,
    gh_run=None,
    git_run=None,
    repos_path=None,
    dry_run=False,
):
    """Build an AwayAutomation with sensible mock defaults."""
    gh_calls = []
    git_calls = []
    launch_calls = []

    if gh_run is None:
        def gh_run(args):
            gh_calls.append(args)
            if args[:2] == ["issue", "list"]:
                return json.dumps(gh_issues or [])
            if args[:2] == ["pr", "create"]:
                return "https://github.com/owner/repo/pull/1\n"
            return ""

    if git_run is None:
        def git_run(args, cwd=None):
            git_calls.append((args, cwd))
            # By default a worker leaves one real commit on its branch, so the
            # artifact gate sees work and the run finalizes (push + draft PR).
            if args[:2] == ["rev-list", "--count"]:
                return "1\n"
            return ""

    if launch_loop is None:
        def launch_loop(**kwargs):
            launch_calls.append(kwargs)
            return {"status": "running"}

    if poll_status is None:
        def poll_status(chat_id, bot_id):
            return {"status": "completed"}

    engine = AwayAutomation(
        launch_loop=launch_loop,
        poll_status=poll_status,
        gh_run=gh_run,
        git_run=git_run,
        repos_path=repos_path or (tmp_path / "missing.json"),
        projects_dir=tmp_path / "Projects",
        sleep=lambda _s: None,
        dry_run=dry_run,
    )
    engine._gh_calls = gh_calls
    engine._git_calls = git_calls
    engine._launch_calls = launch_calls
    return engine


# ── load_repos ──────────────────────────────────────────────────────


def test_load_repos_missing_file(tmp_path):
    engine = make_engine(tmp_path, repos_path=tmp_path / "nope.json")
    assert engine.load_repos() == []


def test_load_repos_parses_and_normalizes(tmp_path, repos_file):
    path = repos_file([{"repo": "owner/a", "label": "auto"}, {"repo": "owner/b"}])
    engine = make_engine(tmp_path, repos_path=path)
    repos = engine.load_repos()
    assert [r["repo"] for r in repos] == ["owner/a", "owner/b"]
    assert repos[0]["label"] == "auto"
    assert repos[1]["base"] == "main"  # default applied


def test_load_repos_ignores_entries_without_repo(tmp_path, repos_file):
    path = repos_file([{"label": "auto"}, {"repo": "owner/c"}, {}])
    engine = make_engine(tmp_path, repos_path=path)
    repos = engine.load_repos()
    assert [r["repo"] for r in repos] == ["owner/c"]


# ── eligible_issues ─────────────────────────────────────────────────


def test_eligible_no_gate_returns_all(tmp_path):
    issues = [_issue(1), _issue(2, labels=["bug"])]
    engine = make_engine(tmp_path, gh_issues=issues)
    out = engine.eligible_issues({"repo": "owner/a"})
    assert {i["number"] for i in out} == {1, 2}


def test_eligible_label_gate(tmp_path):
    issues = [_issue(1, labels=["auto"]), _issue(2, labels=["bug"]), _issue(3)]
    engine = make_engine(tmp_path, gh_issues=issues)
    out = engine.eligible_issues({"repo": "owner/a", "label": "auto"})
    assert [i["number"] for i in out] == [1]


def test_eligible_assignee_gate(tmp_path):
    issues = [_issue(1, assignees=["alice"]), _issue(2, assignees=["bob"])]
    engine = make_engine(tmp_path, gh_issues=issues)
    out = engine.eligible_issues({"repo": "owner/a", "assignee": "bob"})
    assert [i["number"] for i in out] == [2]


def test_eligible_title_prefix_gate(tmp_path):
    issues = [_issue(1, title="[auto] do x"), _issue(2, title="manual y")]
    engine = make_engine(tmp_path, gh_issues=issues)
    out = engine.eligible_issues({"repo": "owner/a", "title_prefix": "[auto]"})
    assert [i["number"] for i in out] == [1]


# ── start: hard cap ─────────────────────────────────────────────────


def test_start_hard_cap(tmp_path, repos_file, monkeypatch):
    monkeypatch.setenv("AWAY_MAX_ISSUES", "2")
    issues = [_issue(n) for n in range(1, 6)]  # 5 eligible
    path = repos_file([{"repo": "owner/a"}])
    engine = make_engine(tmp_path, gh_issues=issues, repos_path=path)

    result = engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    assert result["capped"] is True
    assert result["queued"] == 2
    assert len(result["issues"]) == 2
    assert result["repos"] == ["owner/a"]


# ── concurrency bound ───────────────────────────────────────────────


def test_concurrency_bound(tmp_path, repos_file, monkeypatch):
    monkeypatch.setenv("AWAY_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("AWAY_MAX_ISSUES", "6")
    issues = [_issue(n) for n in range(1, 7)]
    path = repos_file([{"repo": "owner/a"}])

    in_flight = 0
    max_seen = 0
    lock = threading.Lock()
    start_barrier = threading.Event()

    def launch_loop(**kwargs):
        nonlocal in_flight, max_seen
        with lock:
            in_flight += 1
            max_seen = max(max_seen, in_flight)
        # Hold the slot briefly so overlap is observable.
        start_barrier.wait(timeout=0.2)
        with lock:
            in_flight -= 1
        return {"status": "running"}

    engine = make_engine(
        tmp_path, gh_issues=issues, repos_path=path, launch_loop=launch_loop
    )
    engine.start("chat1", "default", "tok")
    # Let workers accumulate, then release.
    threading.Timer(0.1, start_barrier.set).start()
    engine._join_workers(timeout=10)

    assert max_seen <= 2


# ── draft-PR-only safety ────────────────────────────────────────────


def test_draft_pr_only_and_no_main_push(tmp_path, repos_file):
    path = repos_file([{"repo": "owner/a", "base": "main"}])
    engine = make_engine(tmp_path, gh_issues=[_issue(7)], repos_path=path)

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    pr_calls = [c for c in engine._gh_calls if c[:2] == ["pr", "create"]]
    assert len(pr_calls) == 1
    args = pr_calls[0]
    assert "--draft" in args
    head = args[args.index("--head") + 1]
    base = args[args.index("--base") + 1]
    assert head == "away/issue-7"
    assert head != base

    # Never `git push` of main; only the away branch is pushed.
    pushes = [a for (a, _cwd) in engine._git_calls if a and a[0] == "push"]
    for push in pushes:
        assert "main" not in push
        assert "away/issue-7" in push


# ── dry-run ─────────────────────────────────────────────────────────


def test_dry_run_skips_pr_create(tmp_path, repos_file):
    path = repos_file([{"repo": "owner/a"}])
    engine = make_engine(tmp_path, gh_issues=[_issue(8)], repos_path=path, dry_run=True)

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    pr_calls = [c for c in engine._gh_calls if c[:2] == ["pr", "create"]]
    assert pr_calls == []

    results = engine.status("chat1")["results"]
    assert len(results) == 1
    assert results[0]["pr_url"] == "(dry-run)"
    assert results[0]["status"] == "done"


def test_dry_run_via_env(tmp_path, repos_file, monkeypatch):
    monkeypatch.setenv("AWAY_DRY_RUN", "1")
    path = repos_file([{"repo": "owner/a"}])
    engine = make_engine(tmp_path, gh_issues=[_issue(9)], repos_path=path)

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    pr_calls = [c for c in engine._gh_calls if c[:2] == ["pr", "create"]]
    assert pr_calls == []
    assert engine.status("chat1")["results"][0]["pr_url"] == "(dry-run)"


# ── stop ────────────────────────────────────────────────────────────


def test_stop_returns_roundup_and_sets_flag(tmp_path, repos_file):
    path = repos_file([{"repo": "owner/a"}])
    engine = make_engine(tmp_path, gh_issues=[_issue(10)], repos_path=path, dry_run=True)

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.stop("chat1")
    assert result["stopped_new_pickups"] is True
    assert isinstance(result["roundup"], list)
    assert any(r["number"] == 10 for r in result["roundup"])


def test_stop_before_pickup_skips_new_work(tmp_path, repos_file, monkeypatch):
    monkeypatch.setenv("AWAY_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("AWAY_MAX_ISSUES", "3")
    path = repos_file([{"repo": "owner/a"}])
    issues = [_issue(n) for n in (11, 12, 13)]

    gate = threading.Event()
    launched = []
    lock = threading.Lock()

    def launch_loop(**kwargs):
        with lock:
            launched.append(kwargs["chat_id"])
        gate.wait(timeout=2)
        return {"status": "running"}

    engine = make_engine(
        tmp_path, gh_issues=issues, repos_path=path, launch_loop=launch_loop, dry_run=True
    )
    engine.start("chat1", "default", "tok")
    # First worker is holding the single concurrency slot; stop now.
    engine.stop("chat1")
    gate.set()
    engine._join_workers(timeout=10)

    # With concurrency=1, later issues must be skipped once stop is set.
    assert len(launched) < 3


# ── artifact-based completion gate (issue #19) ──────────────────────


def _git_run_with_commits(commits, git_calls, *, fetch_raises=False):
    """Build a git_run mock that reports ``commits`` ahead of base."""

    def git_run(args, cwd=None):
        git_calls.append((args, cwd))
        if fetch_raises and args[:1] == ["fetch"]:
            raise RuntimeError("no remote 'origin'")
        if args[:2] == ["rev-list", "--count"]:
            return f"{commits}\n"
        return ""

    return git_run


def test_empty_branch_completed_marked_failed_no_pr(tmp_path, repos_file):
    """A 'completed' loop that left zero commits is a failure, not a silent done."""
    path = repos_file([{"repo": "owner/a"}])
    git_calls = []
    engine = make_engine(
        tmp_path,
        gh_issues=[_issue(20)],
        repos_path=path,
        git_run=_git_run_with_commits(0, git_calls),
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.status("chat1")["results"][0]
    assert result["status"] == "failed"
    assert result["commits"] == 0
    assert "no commits" in result["error"]
    # No PR opened and no branch pushed for an empty branch.
    assert [c for c in engine._gh_calls if c[:2] == ["pr", "create"]] == []
    assert [a for (a, _c) in git_calls if a and a[0] == "push"] == []


def test_empty_branch_failed_even_under_dry_run(tmp_path, repos_file):
    """The gate runs before _finalize, so dry-run can't mask the empty branch."""
    path = repos_file([{"repo": "owner/a"}])
    git_calls = []
    engine = make_engine(
        tmp_path,
        gh_issues=[_issue(21)],
        repos_path=path,
        dry_run=True,
        git_run=_git_run_with_commits(0, git_calls),
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.status("chat1")["results"][0]
    assert result["status"] == "failed"
    assert result["pr_url"] is None


def test_commits_present_finalizes_and_records_count(tmp_path, repos_file):
    """A branch with commits ahead of base finalizes and reports the count."""
    path = repos_file([{"repo": "owner/a"}])
    git_calls = []
    engine = make_engine(
        tmp_path,
        gh_issues=[_issue(22)],
        repos_path=path,
        git_run=_git_run_with_commits(3, git_calls),
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.status("chat1")["results"][0]
    assert result["status"] == "done"
    assert result["commits"] == 3
    assert len([c for c in engine._gh_calls if c[:2] == ["pr", "create"]]) == 1


# ── delivery-by-artifact for empty branches (issue #21) ─────────────


def _gh_run_with_issue_state(gh_calls, state, reason=None, *, pr_url="https://x/pull/1\n"):
    """gh_run mock that reports a given issue state/stateReason on `issue view`."""

    def gh_run(args):
        gh_calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps([])  # eligibility is supplied separately via gh_issues
        if args[:2] == ["issue", "view"]:
            payload = {"state": state}
            if reason is not None:
                payload["stateReason"] = reason
            return json.dumps(payload)
        if args[:2] == ["pr", "create"]:
            return pr_url
        return ""

    return gh_run


def _engine_with_states(tmp_path, repos_file, issue_no, *, state, reason=None):
    """Build an engine whose worker leaves an empty branch and whose issue
    reports the given closed/open state — exercising the #21 delivery gate."""
    path = repos_file([{"repo": "owner/a"}])
    gh_calls = []
    git_calls = []

    base_gh = _gh_run_with_issue_state(gh_calls, state, reason)

    def gh_run(args):
        # eligible_issues still needs the real issue list; delegate the rest.
        if args[:2] == ["issue", "list"]:
            gh_calls.append(args)
            return json.dumps([_issue(issue_no)])
        return base_gh(args)

    engine = make_engine(
        tmp_path,
        repos_path=path,
        gh_run=gh_run,
        git_run=_git_run_with_commits(0, git_calls),  # empty branch
    )
    engine._gh_calls = gh_calls
    engine._git_calls2 = git_calls
    return engine, gh_calls, git_calls


def test_empty_branch_but_issue_closed_marked_done_not_failed(tmp_path, repos_file):
    """#21: work landed outside away/issue-N (issue CLOSED) → done, not a false failure."""
    engine, gh_calls, git_calls = _engine_with_states(
        tmp_path, repos_file, 7, state="CLOSED", reason="COMPLETED"
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.status("chat1")["results"][0]
    assert result["status"] == "done"
    assert result["delivered_elsewhere"] is True
    assert result["commits"] == 0
    assert result.get("pr_url") is None
    # No PR fabricated, no push for an empty branch...
    assert [c for c in gh_calls if c[:2] == ["pr", "create"]] == []
    assert [a for (a, _c) in git_calls if a and a[0] == "push"] == []
    # ...and the 0-commit shell is GC'd so a later /away can retry cleanly.
    assert any(a[:2] == ["worktree", "remove"] for (a, _c) in git_calls)
    assert any(a == ["branch", "-D", "away/issue-7"] for (a, _c) in git_calls)


def test_empty_branch_issue_closed_not_planned_is_failure(tmp_path, repos_file):
    """A NOT_PLANNED close is a dismissal, not a delivery → stays failed."""
    engine, _gh, git_calls = _engine_with_states(
        tmp_path, repos_file, 8, state="CLOSED", reason="NOT_PLANNED"
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.status("chat1")["results"][0]
    assert result["status"] == "failed"
    assert "no commits" in result["error"]
    # Still GC'd so the empty shell can't block retry.
    assert any(a[:2] == ["worktree", "remove"] for (a, _c) in git_calls)


def test_empty_branch_issue_still_open_is_failure(tmp_path, repos_file):
    """Empty branch + still-open issue = genuinely undelivered → failed (fail closed)."""
    engine, _gh, git_calls = _engine_with_states(
        tmp_path, repos_file, 9, state="OPEN"
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.status("chat1")["results"][0]
    assert result["status"] == "failed"
    assert result.get("delivered_elsewhere") is None


def test_delivery_check_fails_closed_on_gh_error(tmp_path, repos_file):
    """If `gh issue view` errors, treat as not-delivered (never a false 'done')."""
    path = repos_file([{"repo": "owner/a"}])
    git_calls = []

    def gh_run(args):
        if args[:2] == ["issue", "list"]:
            return json.dumps([_issue(10)])
        if args[:2] == ["issue", "view"]:
            raise RuntimeError("gh: network unreachable")
        return ""

    engine = make_engine(
        tmp_path,
        repos_path=path,
        gh_run=gh_run,
        git_run=_git_run_with_commits(0, git_calls),
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.status("chat1")["results"][0]
    assert result["status"] == "failed"
    assert result.get("delivered_elsewhere") is None


def test_worktree_add_retries_once_after_gc_on_leftover(tmp_path, repos_file):
    """#21 fix #3: a leftover shell that breaks `worktree add` is GC'd + retried once."""
    path = repos_file([{"repo": "owner/a"}])
    git_calls = []
    state = {"add_attempts": 0}

    def git_run(args, cwd=None):
        git_calls.append((args, cwd))
        if args[:2] == ["worktree", "add"]:
            state["add_attempts"] += 1
            if state["add_attempts"] == 1:
                raise RuntimeError("fatal: 'away/issue-11' already exists")
            return ""
        if args[:2] == ["rev-list", "--count"]:
            return "2\n"  # second add succeeds → branch has commits → finalize
        return ""

    engine = make_engine(
        tmp_path,
        gh_issues=[_issue(11)],
        repos_path=path,
        git_run=git_run,
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    result = engine.status("chat1")["results"][0]
    assert state["add_attempts"] == 2  # retried exactly once
    # GC ran between the two add attempts.
    assert any(a[:2] == ["worktree", "remove"] for (a, _c) in git_calls)
    assert result["status"] == "done"
    assert result["commits"] == 2


# ── stale-base fetch (issue #19 root cause #4) ──────────────────────


def test_worktree_fetches_origin_and_branches_from_origin_base(tmp_path, repos_file):
    path = repos_file([{"repo": "owner/a", "base": "main"}])
    git_calls = []
    engine = make_engine(
        tmp_path,
        gh_issues=[_issue(23)],
        repos_path=path,
        git_run=_git_run_with_commits(1, git_calls),
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    assert ["fetch", "origin", "main"] in [a for (a, _c) in git_calls]
    adds = [a for (a, _c) in git_calls if a[:2] == ["worktree", "add"]]
    assert len(adds) == 1
    # Branch off origin/main, not the stale local main.
    assert adds[0][-1] == "origin/main"


def test_fetch_failure_falls_back_to_local_base(tmp_path, repos_file):
    """Offline / no remote: fetch fails silently and the worker still proceeds."""
    path = repos_file([{"repo": "owner/a", "base": "main"}])
    git_calls = []
    engine = make_engine(
        tmp_path,
        gh_issues=[_issue(24)],
        repos_path=path,
        git_run=_git_run_with_commits(1, git_calls, fetch_raises=True),
    )

    engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)

    adds = [a for (a, _c) in git_calls if a[:2] == ["worktree", "add"]]
    assert len(adds) == 1
    assert adds[0][-1] == "main"  # fell back to local base
    # The worker is not derailed by the fetch failure.
    assert engine.status("chat1")["results"][0]["status"] == "done"


# ── idempotent dispatch: no double-dispatch on rapid /away (issue #20) ──


def test_inflight_dedup_skips_duplicate_dispatch(tmp_path, repos_file):
    """A re-entered /away must not spawn a second worker for an in-flight issue."""
    path = repos_file([{"repo": "owner/a"}])
    entered = threading.Event()
    gate = threading.Event()

    def launch_loop(**kwargs):
        entered.set()
        gate.wait(timeout=5)  # hold the worker in flight
        return {"status": "running"}

    engine = make_engine(
        tmp_path, gh_issues=[_issue(30)], repos_path=path, launch_loop=launch_loop, dry_run=True
    )

    first = engine.start("chat1", "default", "tok")
    assert entered.wait(timeout=5)  # worker is mid-flight, holding the marker
    first_workers = list(engine._workers)  # capture before the next start() resets it

    # Re-enter /away while the first worker is still running.
    second = engine.start("chat1", "default", "tok")
    assert second["queued"] == 0
    assert second["skipped_inflight"] >= 1

    gate.set()
    for t in first_workers:
        t.join(timeout=5)
    assert first["queued"] == 1


def test_inflight_marker_cleared_allows_redispatch(tmp_path, repos_file):
    """Once a worker finishes, its marker clears so the issue can dispatch again."""
    path = repos_file([{"repo": "owner/a"}])
    engine = make_engine(tmp_path, gh_issues=[_issue(31)], repos_path=path, dry_run=True)

    first = engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)  # worker finishes → finally clears the marker
    assert first["queued"] == 1

    # A later /away re-dispatches the same issue (self-healing dedup).
    second = engine.start("chat1", "default", "tok")
    engine._join_workers(timeout=5)
    assert second["queued"] == 1
    assert second["skipped_inflight"] == 0
