# CLAUDE.md — mini-claude-bot

## macOS Permissions

- **iTerm2** has been granted Automation, Accessibility, and Full Disk Access permissions.
- When running `osascript` (e.g., to send email via Mail.app), it must run from a process with macOS Automation permissions (e.g., iTerm2).
- Python subprocesses and other background processes may be blocked by macOS TCC if they lack these permissions.

## Daily Reports & Email Sending

- Report generation: `reports/scripts/generate_report.py`
- Email sending uses **JXA** (JavaScript for Automation) via `osascript -l JavaScript reports/scripts/send_email.js`
- Flow: Python generates PDF → writes email params to `reports/output/pending_email.json` → JXA script reads queue, sends via Mail.app, deletes queue file on success
- If automated send fails (e.g., from cron), the queue file is preserved for manual retry: `osascript -l JavaScript reports/scripts/send_email.js`
- Cron jobs (APScheduler, job IDs 2 & 3):
  - Chinese report: 9AM Shanghai time → sent to `REPORT_TO_PRIMARY` (童总)
  - English report: 9AM LA time → sent to `REPORT_TO_SECONDARY`
