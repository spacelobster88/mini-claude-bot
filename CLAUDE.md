# CLAUDE.md — mini-claude-bot

## macOS Permissions

- **iTerm2** has been granted Automation, Accessibility, and Full Disk Access permissions.
- When running `osascript` (e.g., to send email via Mail.app), commands must run from an iTerm2 context.
- Python subprocesses and other processes (e.g., APScheduler cron jobs) calling `osascript` will be blocked by macOS because they lack these permissions.
- For cron jobs that need to send email, use `job_type: "shell"` routed through iTerm2, or invoke osascript via a shell script that runs in the permitted context.
