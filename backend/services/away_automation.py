"""Away issue-automation engine for the /away command.

`AwayAutomation` reads a repo-queue config (`~/.away-repos.json`), finds eligible
open issues, and for each spawns a forced-auto harness loop inside a per-issue git
worktree. On success it opens a *draft* PR linking the issue. All external effects
(gh CLI, git, harness-loop launcher, status poller, sleep) are injectable so the
engine is fully unit-testable with no real network/git/subprocess.

Safety invariants (see docs/away-automation-design.md):
- Concurrency bounded by ``AWAY_MAX_CONCURRENCY`` (default 2).
- Hard cap of ``AWAY_MAX_ISSUES`` (default 5) issues per /away.
- Draft PRs only; never push to or target the base branch as head.
- ``AWAY_DRY_RUN`` (or ctor ``dry_run``) skips the push + real PR create.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger("away_automation")


def _default_gh_run(args: list[str]) -> str:
    """Run `gh <args>` and return stdout."""
    proc = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _default_git_run(args: list[str], cwd: str | None = None) -> str:
    """Run `git <args>` and return stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class AwayAutomation:
    """Self-contained, injectable /away issue-automation engine."""

    def __init__(
        self,
        *,
        launch_loop: Callable[..., dict],
        poll_status: Callable[..., dict],
        gh_run: Callable[[list[str]], str] = _default_gh_run,
        git_run: Callable[..., str] = _default_git_run,
        repos_path: str | Path | None = None,
        projects_dir: str | Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        dry_run: bool = False,
    ):
        self._launch_loop = launch_loop
        self._poll_status = poll_status
        self._gh_run = gh_run
        self._git_run = git_run
        self._repos_path = Path(repos_path) if repos_path else (Path.home() / ".away-repos.json")
        self._projects_dir = Path(projects_dir) if projects_dir else (Path.home() / "Projects")
        self._sleep = sleep
        self._dry_run = dry_run

        # Per-issue state keyed by worker chat_id, plus stop flags / threads.
        self._lock = threading.Lock()
        self._results: dict[str, dict] = {}
        self._stop_flags: set[str] = set()
        self._workers: list[threading.Thread] = []
        # worker_chat_ids currently in flight — guards against double-dispatch
        # of the same (repo, issue) on rapid/repeated /away (issue #20). Cleared
        # when each worker finishes (see _worker) so it's self-healing.
        self._active: set[str] = set()

    # ── config ──────────────────────────────────────────────────────

    def load_repos(self) -> list[dict]:
        """Read + normalize the repo-queue config. Missing file → []."""
        try:
            raw = self._repos_path.read_text()
        except FileNotFoundError:
            return []
        except OSError as e:
            logger.warning("Could not read repos config %s: %s", self._repos_path, e)
            return []

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("Invalid JSON in %s: %s", self._repos_path, e)
            return []

        if not isinstance(data, list):
            return []

        repos: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            repo = entry.get("repo")
            if not repo or not isinstance(repo, str):
                continue
            normalized = {
                "repo": repo,
                "label": entry.get("label"),
                "assignee": entry.get("assignee"),
                "title_prefix": entry.get("title_prefix"),
                "path": entry.get("path"),
                "base": entry.get("base", "main"),
            }
            repos.append(normalized)
        return repos

    # ── eligibility ─────────────────────────────────────────────────

    def eligible_issues(self, entry: dict) -> list[dict]:
        """Return open issues for ``entry`` passing all present gates.

        Gates (AND of whichever are present): label, assignee, title_prefix.
        If none present → all open issues (repo-as-queue).
        """
        repo = entry["repo"]
        out = self._gh_run(
            [
                "issue",
                "list",
                "-R",
                repo,
                "--state",
                "open",
                "--json",
                "number,title,body,labels,assignees",
                "--limit",
                "100",
            ]
        )
        try:
            issues = json.loads(out) if out and out.strip() else []
        except json.JSONDecodeError as e:
            logger.warning("Could not parse gh issue list for %s: %s", repo, e)
            return []

        label = entry.get("label")
        assignee = entry.get("assignee")
        title_prefix = entry.get("title_prefix")

        result: list[dict] = []
        for issue in issues:
            if label:
                labels = {l.get("name") for l in (issue.get("labels") or [])}
                if label not in labels:
                    continue
            if assignee:
                logins = {a.get("login") for a in (issue.get("assignees") or [])}
                # "@me" is a server-side gh filter; here just match the bare login.
                want = assignee[1:] if assignee.startswith("@") and assignee != "@me" else assignee
                if want == "@me":
                    # Can't resolve @me without a network call; skip the gate.
                    pass
                elif want not in logins:
                    continue
            if title_prefix:
                if not (issue.get("title") or "").startswith(title_prefix):
                    continue
            result.append(
                {
                    "number": issue.get("number"),
                    "title": issue.get("title") or "",
                    "body": issue.get("body") or "",
                }
            )
        return result

    # ── public lifecycle ────────────────────────────────────────────

    def _repo_name(self, repo: str) -> str:
        return repo.split("/")[-1]

    def start(self, chat_id: str, bot_id: str, bot_token: str) -> dict:
        """Gather eligible issues across all repos, apply the hard cap, and
        spawn bounded background workers. Returns promptly."""
        max_issues = _env_int("AWAY_MAX_ISSUES", 5)
        max_concurrency = _env_int("AWAY_MAX_CONCURRENCY", 2)

        repos = self.load_repos()
        repo_names = [e["repo"] for e in repos]

        # Clear any prior stop flags for this away run (fresh start).
        with self._lock:
            self._stop_flags = {f for f in self._stop_flags if not f.startswith(f"{chat_id}:")}
            self._workers = []

        # Gather all eligible (entry, issue) pairs.
        gathered: list[tuple[dict, dict]] = []
        for entry in repos:
            try:
                for issue in self.eligible_issues(entry):
                    gathered.append((entry, issue))
            except Exception as e:  # noqa: BLE001 - one bad repo shouldn't sink the run
                logger.warning("eligible_issues failed for %s: %s", entry.get("repo"), e)

        capped = len(gathered) > max_issues
        selected = gathered[:max_issues]

        semaphore = threading.Semaphore(max_concurrency)
        queued_issues: list[dict] = []
        workers: list[threading.Thread] = []
        skipped_inflight = 0

        for entry, issue in selected:
            repo_name = self._repo_name(entry["repo"])
            number = issue["number"]
            branch = f"away/issue-{number}"
            worker_chat_id = f"away-{repo_name}-{number}"

            # Idempotent dispatch (issue #20): never spawn a second worker for an
            # (repo, issue) that already has one in flight, so rapid/repeated /away
            # can't double-dispatch and race the #19 artifact gate. The marker is
            # cleared in _worker's finally, so a finished/failed issue is free to
            # be dispatched again later.
            with self._lock:
                if worker_chat_id in self._active:
                    skipped_inflight += 1
                    continue
                self._active.add(worker_chat_id)

            queued_issues.append(
                {
                    "repo": entry["repo"],
                    "number": number,
                    "title": issue["title"],
                    "branch": branch,
                }
            )

            t = threading.Thread(
                target=self._worker,
                args=(semaphore, chat_id, worker_chat_id, entry, issue, bot_id, bot_token),
                name=f"away-{worker_chat_id}",
                daemon=True,
            )
            workers.append(t)

        with self._lock:
            self._workers = workers
        for t in workers:
            t.start()

        return {
            "repos": repo_names,
            "queued": len(queued_issues),
            "issues": queued_issues,
            "capped": capped,
            "skipped_inflight": skipped_inflight,
        }

    def stop(self, chat_id: str) -> dict:
        """Halt new pickups for ``chat_id`` and return the per-issue roundup."""
        with self._lock:
            self._stop_flags.add(f"{chat_id}:*")
            roundup = list(self._results.values())
        return {"roundup": roundup, "stopped_new_pickups": True}

    def status(self, chat_id: str) -> dict:
        with self._lock:
            results = list(self._results.values())
            running = sum(1 for t in self._workers if t.is_alive())
        return {"running": running, "results": results}

    # ── worker ──────────────────────────────────────────────────────

    def _stop_requested(self, chat_id: str) -> bool:
        with self._lock:
            return f"{chat_id}:*" in self._stop_flags

    def _worker(
        self,
        semaphore: threading.Semaphore,
        chat_id: str,
        worker_chat_id: str,
        entry: dict,
        issue: dict,
        bot_id: str,
        bot_token: str,
    ) -> None:
        semaphore.acquire()
        try:
            # Stop requested before this worker began its loop → skip pickup.
            if self._stop_requested(chat_id):
                return
            self._process_issue(entry, issue, chat_id, worker_chat_id, bot_id, bot_token)
        finally:
            semaphore.release()
            # Release the in-flight dispatch marker (issue #20) so this issue can
            # be dispatched again on a later /away. Always runs — even on skip or
            # crash — so a failed worker never permanently blocks re-dispatch.
            with self._lock:
                self._active.discard(worker_chat_id)

    def _process_issue(
        self,
        entry: dict,
        issue: dict,
        chat_id: str,
        worker_chat_id: str,
        bot_id: str,
        bot_token: str,
    ) -> None:
        repo = entry["repo"]
        repo_name = self._repo_name(repo)
        number = issue["number"]
        title = issue["title"]
        body = issue["body"]
        base = entry.get("base") or "main"
        branch = f"away/issue-{number}"

        local = entry.get("path") or str(self._projects_dir / repo_name)
        wt = str(Path(local) / ".git" / "away-worktrees" / f"issue-{number}")

        state = {
            "repo": repo,
            "number": number,
            "title": title,
            "branch": branch,
            "status": "failed",
            "pr_url": None,
            "commits": 0,
        }

        try:
            # 1+2. Create a per-issue worktree on a fresh branch off base. Fetch
            # origin first so the worktree sees current fixtures/context (issue #19
            # root cause #4: a stale local base made workers miss the very fixtures
            # the issue referenced). Best-effort — fall back to the local base if
            # there's no remote / we're offline. Never fatal.
            start_point = base
            try:
                self._git_run(["fetch", "origin", base], cwd=local)
                start_point = f"origin/{base}"
            except Exception as e:  # noqa: BLE001 - offline / no remote → local base
                logger.info("away: fetch origin %s failed, using local base: %s", base, e)
            try:
                self._git_run(["worktree", "add", "-b", branch, wt, start_point], cwd=local)
            except Exception:  # noqa: BLE001 - leftover shell from an interrupted run
                # A 0-commit ``away/issue-N`` worktree/branch left by a prior
                # interrupted /away must never permanently block re-creation
                # (issue #21, proposed fix #3). GC the shell and retry exactly
                # once. The leftover is scratch for *this same issue*, which we
                # are about to redo from ``start_point`` anyway; nothing shipped
                # is at risk (push only happens on success in _finalize).
                self._gc_away_worktree(local, wt, branch)
                self._git_run(["worktree", "add", "-b", branch, wt, start_point], cwd=local)

            # 3. Frame the issue as a self-driving harness task and launch the loop.
            message = self._frame_message(repo, number, title, body)
            self._launch_loop(
                chat_id=worker_chat_id,
                message=message,
                bot_token=bot_token,
                bot_id=bot_id,
                cwd=wt,
                force_auto_mode=True,
            )

            # 4. Poll until terminal or timeout.
            terminal = self._wait_for_loop(worker_chat_id, bot_id)

            if terminal == "completed":
                # Artifact-based completion gate (issue #19 root cause #2): a loop
                # that "completed" but left the branch empty has shipped nothing.
                # Report it honestly as failed instead of pushing an empty branch
                # and opening a no-op PR. Runs *before* _finalize so the gate holds
                # even under AWAY_DRY_RUN — otherwise dry-run would mask the bug.
                commits = self._count_commits(base, branch, wt)
                state["commits"] = commits
                if commits == 0:
                    # ``away/issue-N`` is empty — but the work may have landed
                    # outside it (direct commit to base, a differently-named
                    # branch, a squash, or a merged PR / prior session). Verify
                    # delivery by artifact, not by this branch's PR status
                    # (issue #21, proposed fixes #1+#2): a CLOSED-as-completed
                    # issue is delivered, not a failure. Only a genuinely
                    # undelivered empty branch is a real failure.
                    if self._issue_delivered(repo, number):
                        state["status"] = "done"
                        state["delivered_elsewhere"] = True
                    else:
                        state["status"] = "failed"
                        state["error"] = (
                            "harness loop completed but produced no commits (empty branch)"
                        )
                    # Either way this is a 0-commit shell: GC it so a later
                    # /away can cleanly re-create the branch instead of skipping
                    # on a pre-existing empty branch (issue #21, proposed fix #3).
                    self._gc_away_worktree(local, wt, branch)
                else:
                    pr_url = self._finalize(repo, base, branch, number, title, wt)
                    state["status"] = "done"
                    state["pr_url"] = pr_url
            else:
                state["status"] = "failed"
                state["error"] = f"harness loop {terminal}"
        except Exception as e:  # noqa: BLE001 - record, never crash the thread
            logger.exception("away worker failed for %s#%s", repo, number)
            state["status"] = "failed"
            state["error"] = str(e)
        finally:
            with self._lock:
                self._results[worker_chat_id] = state

    def _frame_message(self, repo: str, number: int, title: str, body: str) -> str:
        return (
            f"You are working autonomously on GitHub issue {repo}#{number} in this "
            f"git worktree. Fully implement the change end-to-end and COMMIT it when done.\n\n"
            f"Issue title: {title}\n\n"
            f"Issue body:\n{body}\n\n"
            f"Acceptance: the change is complete, tests (if any) pass, and the work is "
            f"committed on the current branch. Do not open a PR — that is handled for you."
        )

    def _wait_for_loop(self, worker_chat_id: str, bot_id: str) -> str:
        """Poll poll_status until status is terminal or AWAY_LOOP_TIMEOUT. Returns
        the terminal status string (completed/failed/timeout)."""
        timeout = _env_int("AWAY_LOOP_TIMEOUT", 3600)
        deadline = time.monotonic() + timeout
        poll_interval = 5.0
        while True:
            info = self._poll_status(worker_chat_id, bot_id)
            status = (info or {}).get("status")
            if status in ("completed", "failed"):
                return status
            if time.monotonic() >= deadline:
                return "timeout"
            self._sleep(poll_interval)

    def _count_commits(self, base: str, branch: str, wt: str) -> int:
        """Commits on ``branch`` ahead of ``base`` — the artifact-gate signal.

        Any error (unreadable worktree, bad output) → 0, i.e. "no artifacts",
        which the caller treats as *not done* rather than a silent success.
        """
        try:
            out = self._git_run(["rev-list", "--count", f"{base}..{branch}"], cwd=wt)
        except Exception:  # noqa: BLE001 - unreadable → treat as no artifacts
            return 0
        try:
            return int((out or "").strip() or 0)
        except ValueError:
            return 0

    def _issue_delivered(self, repo: str, number: int) -> bool:
        """True if the issue already landed outside ``away/issue-N``.

        Used only when that branch is empty (issue #21): an issue is "delivered"
        if it is CLOSED as completed — the work reached base / another branch / a
        merged PR even though this branch produced nothing. Closed as NOT_PLANNED
        is a dismissal, not a delivery.

        Fails **closed**: any gh/parse error → ``False`` (treat as not delivered),
        so a flaky network can never let a genuinely-empty branch be reported as
        a fabricated success.
        """
        try:
            out = self._gh_run(
                ["issue", "view", str(number), "-R", repo, "--json", "state,stateReason"]
            )
            data = json.loads(out) if out and out.strip() else {}
        except Exception:  # noqa: BLE001 - unreachable / unparseable → not delivered
            return False
        if not isinstance(data, dict):
            return False
        state = str(data.get("state") or "").upper()
        reason = str(data.get("stateReason") or "").upper()
        return state == "CLOSED" and reason != "NOT_PLANNED"

    def _gc_away_worktree(self, local: str, wt: str, branch: str) -> None:
        """Remove a 0-commit away worktree + its branch so a later /away can
        cleanly re-create it (issue #21, proposed fix #3).

        Every step is best-effort and individually guarded — GC must never be
        fatal nor flip a delivered issue to failed. Callers only invoke this on a
        verified-empty branch (commits == 0) or on a leftover shell that is about
        to be rebuilt, so ``branch -D`` discards nothing that shipped.
        """
        for args in (
            ["worktree", "remove", "--force", wt],
            ["worktree", "prune"],
            ["branch", "-D", branch],
        ):
            try:
                self._git_run(args, cwd=local)
            except Exception as e:  # noqa: BLE001 - best-effort cleanup, never fatal
                logger.info("away: gc step %s failed (non-fatal): %s", args[:2], e)

    def _finalize(self, repo: str, base: str, branch: str, number: int, title: str, wt: str) -> str:
        """Push the branch and open a draft PR. Returns the PR URL.

        Safety: never targets/pushes base as head; PRs are ALWAYS --draft.
        """
        if branch == base:
            raise ValueError(f"refusing to use base branch '{base}' as PR head")

        if self._dry_run or _env_truthy("AWAY_DRY_RUN"):
            return "(dry-run)"

        self._git_run(["push", "-u", "origin", branch], cwd=wt)
        out = self._gh_run(
            [
                "pr",
                "create",
                "--draft",
                "-R",
                repo,
                "--base",
                base,
                "--head",
                branch,
                "--title",
                f"{title} (#{number})",
                "--body",
                f"Automated by /away. Closes #{number}.",
            ]
        )
        return (out or "").strip()

    # ── test helper ─────────────────────────────────────────────────

    def _join_workers(self, timeout: float | None = None) -> None:
        """Join all spawned worker threads (used by tests for determinism)."""
        with self._lock:
            workers = list(self._workers)
        for t in workers:
            t.join(timeout)


# ── module singleton ────────────────────────────────────────────────

_instance: AwayAutomation | None = None
_instance_lock = threading.Lock()


def get_away_automation() -> AwayAutomation:
    """Lazily build the real instance wired to the session manager."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                from backend.services.session_manager import get_session_manager

                manager = get_session_manager()

                def launch_loop(*, chat_id, message, bot_token, bot_id, cwd, force_auto_mode):
                    return manager.send_background(
                        chat_id,
                        message,
                        bot_token,
                        bot_id=bot_id,
                        cwd=cwd,
                        force_auto_mode=force_auto_mode,
                    )

                def poll_status(chat_id, bot_id):
                    return manager.get_background_status(chat_id, bot_id=bot_id)

                _instance = AwayAutomation(launch_loop=launch_loop, poll_status=poll_status)
    return _instance
