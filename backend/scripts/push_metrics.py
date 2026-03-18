#!/usr/bin/env python3
"""Push metrics to Vercel Edge Config using curl (no external dependencies)."""
import json
import subprocess
import sys
import time
import os
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

METRICS_CACHE_PATH = os.path.expanduser("~/.mini-claude-bot/metrics_cache.json")


def trim_limit_metrics(metrics: dict) -> None:
    """Clamp large arrays/string fields to keep Edge Config payload small."""
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
        # Keep the two most-used models by requests
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
            # Keep only recent 10, trim to essential fields
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


AUTH_FILE = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"

# Read Edge Config ID from dashboard .env.vercel
EDGE_CONFIG_ID = os.getenv("EDGE_CONFIG_ID", "")
if not EDGE_CONFIG_ID:
    _env_path = Path(__file__).resolve().parent.parent.parent / "dashboard" / ".env.vercel"
    if _env_path.exists():
        for line in _env_path.read_text().splitlines():
            if line.startswith("EDGE_CONFIG_ID="):
                EDGE_CONFIG_ID = line.split("=", 1)[1].strip().strip('"')
                break


def _get_vercel_token() -> str:
    """Read the fresh Vercel CLI token from auth.json."""
    try:
        with open(AUTH_FILE) as f:
            return json.load(f)["token"]
    except Exception as e:
        print(f"ERROR: Could not read Vercel CLI token: {e}")
        return ""


def push_to_vercel(metrics: dict) -> bool:
    """Push metrics directly to Vercel Edge Config using the local CLI token.

    Bypasses the serverless function to avoid stale-token issues.
    """
    token = _get_vercel_token()
    if not token or not EDGE_CONFIG_ID:
        print("ERROR: Missing Vercel CLI token or EDGE_CONFIG_ID")
        return False

    timestamp = metrics.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    payload = json.dumps({
        "items": [
            {"operation": "upsert", "key": "metrics_current", "value": metrics},
            {"operation": "upsert", "key": "metrics_last_push", "value": timestamp},
        ]
    })

    curl_cmd = [
        "curl", "-s", "-X", "PATCH",
        f"https://api.vercel.com/v1/edge-config/{EDGE_CONFIG_ID}/items",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-d", payload,
        "--max-time", "30",
    ]

    try:
        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            resp = result.stdout.strip()
            if '"ok"' in resp or '"status":"ok"' in resp:
                print(f"OK: Pushed metrics at {timestamp}")
                return True
            else:
                print(f"ERROR: Edge Config response: {resp[:200]}")
                return False
        else:
            print(f"ERROR: curl exit {result.returncode}: {result.stderr[:200]}")
            return False
    except subprocess.TimeoutExpired:
        print("ERROR: Push timed out")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        return False


if __name__ == "__main__":
    metrics = collect_metrics()
    trim_limit_metrics(metrics)
    push_to_vercel(metrics)
