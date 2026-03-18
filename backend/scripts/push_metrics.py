#!/usr/bin/env python3
"""Push metrics to the dashboard via Vercel Blob (through /api/push)."""
import json
import sys
import time
import os
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

METRICS_CACHE_PATH = os.path.expanduser("~/.mini-claude-bot/metrics_cache.json")


def trim_limit_metrics(metrics: dict) -> None:
    """Clamp large arrays/string fields to keep payload small."""
    if not isinstance(metrics, dict):
        return

    cron = metrics.get("cron_jobs")
    if isinstance(cron, list):
        trimmed = []
        allowed_keys = {"id", "name", "cron_expression", "enabled", "last_run_at", "last_result_preview", "timezone"}
        for job in cron[:8]:
            job = {k: job.get(k) for k in allowed_keys}
            preview = job.get("last_result_preview")
            if isinstance(preview, str) and len(preview) > 60:
                job["last_result_preview"] = preview[:60] + "…"
            trimmed.append(job)
        metrics["cron_jobs"] = trimmed

    memory = metrics.get("memory")
    if isinstance(memory, dict):
        items = memory.get("items")
        if isinstance(items, list):
            per_cat_limit = 1
            total_limit = 7
            per_cat_counts = {}
            limited = []
            for item in items:
                if len(limited) >= total_limit:
                    break
                cat = item.get("category", "general")
                count = per_cat_counts.get(cat, 0)
                if count >= per_cat_limit:
                    continue
                per_cat_counts[cat] = count + 1
                trimmed_item = dict(item)
                content = trimmed_item.get("content")
                if isinstance(content, str) and len(content) > 160:
                    trimmed_item["content"] = content[:160] + "…"
                limited.append(trimmed_item)
            memory["items"] = limited

    usage = metrics.get("claude_usage")
    if isinstance(usage, dict):
        daily = usage.get("daily_activity")
        if isinstance(daily, list) and len(daily) > 14:
            usage["daily_activity"] = daily[-14:]
    model_usage = usage.get("model_usage") if isinstance(usage, dict) else None
    if isinstance(model_usage, dict) and len(model_usage) > 2:
        most_used = sorted(model_usage.items(), key=lambda kv: kv[1].get("requests", 0), reverse=True)[:2]
        usage["model_usage"] = {k: v for k, v in most_used}

    harness = metrics.get("harness")
    if isinstance(harness, dict):
        for key in ("running_jobs", "completed_jobs"):
            arr = harness.get(key)
            if isinstance(arr, list) and len(arr) > 3:
                harness[key] = arr[:3]
        archived = harness.get("archived_projects")
        if isinstance(archived, list):
            trimmed_archived = []
            for p in archived[-8:]:
                trimmed_archived.append({
                    "project_name": (p.get("project_name") or "unknown")[:50],
                    "archived_at": p.get("archived_at"),
                    "tasks_done": p.get("tasks_done", 0),
                    "tasks_total": p.get("tasks_total", 0),
                })
            harness["archived_projects"] = trimmed_archived

    services = metrics.get("services")
    if isinstance(services, list) and len(services) > 12:
        metrics["services"] = services[:12]

    chat = metrics.get("chat")
    if isinstance(chat, dict):
        for field in ("oldest_message", "newest_message"):
            val = chat.get(field)
            if isinstance(val, str) and len(val) > 40:
                chat[field] = val[:40]


def collect_metrics() -> dict:
    """Read metrics from local cache (written by POST /api/metrics/refresh).

    Falls back to calling the refresh API if no cache exists.
    """
    if os.path.exists(METRICS_CACHE_PATH):
        try:
            with open(METRICS_CACHE_PATH) as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not read cache: {e}, refreshing via API")

    # Fallback: call the local refresh API
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8000/api/metrics/refresh", method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            pass
        if os.path.exists(METRICS_CACHE_PATH):
            with open(METRICS_CACHE_PATH) as f:
                return json.load(f)
    except Exception as e:
        print(f"Warning: Refresh API failed: {e}")

    return {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "error": "no cached data"}


def _load_env() -> dict[str, str]:
    """Load env vars from backend .env file if present."""
    env = {}
    for env_path in [
        Path(__file__).resolve().parent.parent.parent / ".env",  # project root
        Path(__file__).resolve().parent.parent / ".env",          # backend/
        Path(__file__).resolve().parent.parent.parent / "dashboard" / ".env.local",
    ]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"')
    return env


def push_to_dashboard(metrics: dict) -> bool:
    """POST metrics to the dashboard /api/push endpoint (Vercel Blob)."""
    import urllib.request

    env = _load_env()
    push_url = os.getenv("DASHBOARD_PUSH_URL") or env.get("DASHBOARD_PUSH_URL", "")
    secret = os.getenv("METRICS_SECRET") or env.get("METRICS_SECRET", "")

    if not push_url or not secret:
        print("ERROR: Missing DASHBOARD_PUSH_URL or METRICS_SECRET")
        return False

    timestamp = metrics.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    metrics["_last_push"] = timestamp
    body = json.dumps(metrics).encode()

    req = urllib.request.Request(
        push_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print(f"OK: Pushed metrics at {timestamp}")
                return True
            else:
                print(f"ERROR: Response: {result}")
                return False
    except Exception as e:
        print(f"ERROR: {e}")
        return False


if __name__ == "__main__":
    metrics = collect_metrics()
    trim_limit_metrics(metrics)
    push_to_dashboard(metrics)
