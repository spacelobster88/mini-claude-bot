"""End-to-end integration tests for the /away engine (real threads, mocked externals).

Complements the unit tests in test_away_automation.py by driving a full
config -> start -> worker -> worktree/launch/poll/push/draft-PR -> roundup cycle.
"""

import json

from backend.services.away_automation import AwayAutomation


def _gh_factory(issues, pr_url="https://github.com/owner/repoA/pull/99"):
    calls = []

    def gh_run(args):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            return json.dumps(issues)
        if args[:2] == ["pr", "create"]:
            return pr_url + "\n"
        return ""

    gh_run.calls = calls
    return gh_run


def _engine(tmp_path, *, gh, git_calls, launches, dry_run=False):
    def git_run(args, cwd=None):
        git_calls.append(args)
        return ""

    def launch_loop(*, chat_id, message, bot_token, bot_id, cwd, force_auto_mode):
        launches.append({"chat_id": chat_id, "cwd": cwd, "force": force_auto_mode, "message": message})
        return {"status": "started"}

    repos = tmp_path / "away-repos.json"
    repos.write_text(json.dumps([{"repo": "owner/repoA"}]))
    return AwayAutomation(
        launch_loop=launch_loop,
        poll_status=lambda chat_id, bot_id: {"status": "completed"},
        gh_run=gh,
        git_run=git_run,
        repos_path=str(repos),
        projects_dir=str(tmp_path / "Projects"),
        sleep=lambda s: None,
        dry_run=dry_run,
    )


def test_away_end_to_end_opens_draft_pr(tmp_path):
    issues = [{"number": 1, "title": "Add feature", "body": "Do the thing", "labels": [], "assignees": []}]
    gh = _gh_factory(issues)
    git_calls, launches = [], []
    eng = _engine(tmp_path, gh=gh, git_calls=git_calls, launches=launches)

    res = eng.start("chat1", "default", "tok")
    eng._join_workers(timeout=5)

    assert res["repos"] == ["owner/repoA"]
    assert res["queued"] == 1

    # Loop launched in the per-issue worktree with forced auto-mode.
    assert launches and launches[0]["force"] is True
    assert "issue-1" in launches[0]["cwd"]
    assert "owner/repoA#1" in launches[0]["message"]

    # git: worktree add THEN push.
    git_ops = [a[0] for a in git_calls]
    assert "worktree" in git_ops and "push" in git_ops

    # gh: a DRAFT pr create whose head is the away branch (never base).
    pr_calls = [c for c in gh.calls if c[:2] == ["pr", "create"]]
    assert pr_calls, "no PR created"
    assert "--draft" in pr_calls[0]
    head_idx = pr_calls[0].index("--head") + 1
    assert pr_calls[0][head_idx] == "away/issue-1"

    # Roundup reflects success + the PR URL.
    roundup = eng.stop("chat1")["roundup"]
    assert len(roundup) == 1
    assert roundup[0]["status"] == "done"
    assert roundup[0]["pr_url"].startswith("https://")


def test_away_end_to_end_dry_run_skips_push_and_pr(tmp_path):
    issues = [{"number": 2, "title": "Risky change", "body": "...", "labels": [], "assignees": []}]
    gh = _gh_factory(issues)
    git_calls, launches = [], []
    eng = _engine(tmp_path, gh=gh, git_calls=git_calls, launches=launches, dry_run=True)

    eng.start("chatX", "default", "tok")
    eng._join_workers(timeout=5)

    # Worktree still created + loop launched, but NO push and NO real PR.
    git_ops = [a[0] for a in git_calls]
    assert "worktree" in git_ops
    assert "push" not in git_ops
    assert not [c for c in gh.calls if c[:2] == ["pr", "create"]]

    roundup = eng.stop("chatX")["roundup"]
    assert roundup[0]["pr_url"] == "(dry-run)"
