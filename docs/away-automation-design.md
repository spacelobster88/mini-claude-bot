# Design — gateway-away-automation

Tracking issues: mini-claude-bot #15–#18, telegram-claude-hero #4–#6.

## Contracts

### Timeouts (`session_manager.py`)
- `QUEUE_WAIT_TIMEOUT` default `300 → 900`; `BUSY_STUCK_TIMEOUT` `660 → 960`.
- Invariant: `QUEUE_WAIT_TIMEOUT <= BUSY_STUCK_TIMEOUT` (else queued waiters die to the
  stuck-reset before the wait completes). Harness messages remain no-timeout.
- LaunchAgent env: `GATEWAY_QUEUE_WAIT_TIMEOUT=900`, `GATEWAY_BUSY_STUCK_TIMEOUT=960`.

### `/queue` status — `GET /api/gateway/queue-status/{chat_id}`
```json
{ "busy": true, "busy_message": "…", "elapsed_seconds": 47,
  "slots_used": 1, "slots_max": 16, "queue_wait_remaining": 853 }
```
Reuses `GatewaySession.busy_message` + `_busy_detail()`.

### Worktree-cwd background exec
`send_background(chat_id, message, *, cwd=None, force_auto_mode=False, …)`
- `cwd` → the spawned `claude -p` runs there (a per-issue git worktree).
- `force_auto_mode=True` → inject the AUTO Phase-1 prompt regardless of the global
  `GATEWAY_HARNESS_AUTO_MODE` flag (so `/away` loops self-drive without `/confirm`).

### Away engine — `backend/services/away_automation.py`
- Config `~/.away-repos.json`: `[{ "repo": "owner/name", "label"?: "auto" }]`.
- Eligibility per repo: `label` omitted → all open issues; `label` set → that label only;
  also support assignee `@me` and `[auto]` title-prefix.
- Per issue: `git -C <repo> worktree add <wt> -b away/issue-<n>` → forced-auto harness loop
  (first task = issue body) → on success `gh pr create --draft` linking the issue → TG summary.
- **Safety:** concurrency `≤2` (Centurion-mem-gated); hard cap `N` issues per `/away`
  (`AWAY_MAX_ISSUES`, default 5); **draft PRs only, never main**; `AWAY_DRY_RUN=1` mode.
- State table (persisted) → `/back` roundup + stop new pickups.

### Routes / commands
- `POST /api/gateway/away/start` (chat_id) → print repo list, begin pickups.
- `POST /api/gateway/away/stop` (chat_id) → halt new pickups, return roundup.
- Go: `/queue`, `/away` (Nirmana + start), `/back` (Nirmana + stop + roundup).

## Deploy
Code → both repos; `go build` tch; restart via detached LaunchAgent watchdog. **Restart is
LAST and timing-confirmed with Eddie** (multi-tenant). Dry-run QA on a sandbox repo first.
