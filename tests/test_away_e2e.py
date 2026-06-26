"""End-to-end dry-run for /away against a REAL local git sandbox.

Exercises the real `git worktree` plumbing (default git_run) with a stub loop
and dry_run=True — no claude, no pushes, no PRs. Validates the part most likely
to break in prod: actually creating a per-issue worktree on an away/ branch.
"""

import json
import subprocess
from pathlib import Path

import pytest

from backend.services.away_automation import AwayAutomation


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not available",
)
def test_away_e2e_real_worktree_dry_run(tmp_path):
    # 1. A throwaway sandbox repo with one commit on main.
    repo = tmp_path / "sandbox"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "Tester"], repo)
    (repo / "README.md").write_text("sandbox\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)

    # 2. Config points at the sandbox via explicit path.
    cfg = tmp_path / "away-repos.json"
    cfg.write_text(json.dumps([{"repo": "owner/sandbox", "path": str(repo)}]))

    issues = [{"number": 1, "title": "Sandbox task", "body": "do x", "labels": [], "assignees": []}]

    def gh_run(args):
        if args[:2] == ["issue", "list"]:
            return json.dumps(issues)
        raise AssertionError(f"gh must NOT be called beyond issue list in dry-run: {args}")

    ran_in = []

    def launch_loop(*, chat_id, message, bot_token, bot_id, cwd, force_auto_mode):
        # Simulate the harness loop doing + committing work inside the worktree.
        ran_in.append(cwd)
        (Path(cwd) / "work.txt").write_text("done\n")
        _git(["add", "."], cwd)
        _git(["commit", "-q", "-m", "implement issue 1"], cwd)
        return {"status": "started"}

    # Real default git_run (subprocess); dry-run skips push + PR.
    eng = AwayAutomation(
        launch_loop=launch_loop,
        poll_status=lambda c, b: {"status": "completed"},
        gh_run=gh_run,
        repos_path=str(cfg),
        projects_dir=str(tmp_path),
        sleep=lambda s: None,
        dry_run=True,
    )

    res = eng.start("e2e", "default", "tok")
    eng._join_workers(timeout=30)

    assert res["queued"] == 1

    # A REAL worktree exists on the away/ branch, with the simulated work committed.
    wt = repo / ".git" / "away-worktrees" / "issue-1"
    assert wt.exists(), "worktree directory was not created"
    assert (wt / "work.txt").exists(), "loop did not run in the worktree cwd"
    assert ran_in and ran_in[0] == str(wt)

    branches = subprocess.run(
        ["git", "branch", "--list", "away/issue-1"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert "away/issue-1" in branches, "away branch was not created"

    # Dry-run: success recorded, but no real PR.
    roundup = eng.stop("e2e")["roundup"]
    assert roundup[0]["status"] == "done"
    assert roundup[0]["pr_url"] == "(dry-run)"
