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
        req = urllib.request.Request("http://localhost:8000/api/metrics/refresh", method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            pass
        if os.path.exists(METRICS_CACHE_PATH):
            with open(METRICS_CACHE_PATH) as f:
                return json.load(f)
    except Exception as e:
        print(f"Warning: Refresh API failed: {e}")

    return {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "error": "no cached data"}


DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://mini-claude-bot.vercel.app")

# Read METRICS_SECRET from env or dashboard .env file
METRICS_SECRET = os.getenv("METRICS_SECRET", "")
if not METRICS_SECRET:
    _env_path = Path(__file__).resolve().parent.parent.parent / "dashboard" / ".env.vercel"
    if _env_path.exists():
        for line in _env_path.read_text().splitlines():
            if line.startswith("METRICS_SECRET="):
                METRICS_SECRET = line.split("=", 1)[1].strip().strip('"')
                break


def push_to_vercel(metrics: dict) -> bool:
    """Push metrics to the Vercel dashboard's /api/push endpoint (Blob-backed).

    No Edge Config involved — the dashboard stores metrics in Vercel Blob.
    """
    timestamp = metrics.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    push_url = f"{DASHBOARD_URL}/api/push"

    curl_cmd = [
        "curl", "-s", "-X", "POST", push_url,
        "-H", f"Authorization: Bearer {METRICS_SECRET}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(metrics),
        "--max-time", "30",
    ]

    try:
        result = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            resp = result.stdout.strip()
            if '"ok":true' in resp or '"ok": true' in resp:
                print(f"OK: Pushed metrics at {timestamp}")
                return True
            else:
                print(f"ERROR: Push response: {resp[:200]}")
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
    push_to_vercel(metrics)
