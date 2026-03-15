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


def get_vercel_token() -> str:
    """Read Vercel CLI auth token."""
    VERCEL_CLI_AUTH = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"
    if not VERCEL_CLI_AUTH.exists():
        raise RuntimeError(f"Vercel CLI auth not found at {VERCEL_CLI_AUTH}")

    auth = json.loads(VERCEL_CLI_AUTH.read_text())
    token = auth.get("token")
    return token


def push_to_vercel(metrics: dict) -> bool:
    """Push metrics to Vercel Edge Config using curl."""
    EDGE_CONFIG_ID = "ecfg_f46v94r95ijeifeq3n0xv0yai1ia"
    VERCEL_CLI_AUTH = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"

    if not VERCEL_CLI_AUTH.exists():
        raise RuntimeError(f"Vercel CLI auth not found at {VERCEL_CLI_AUTH}")

    auth = json.loads(VERCEL_CLI_AUTH.read_text())
    token = auth.get("token")

    timestamp = metrics.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    # Prepare metrics data
    metrics_data = {
        "items": [
            {"operation": "upsert", "key": "metrics_current", "value": json.dumps(metrics)},
            {"operation": "upsert", "key": "metrics_last_push", "value": timestamp},
        ]
    }

    # Use curl to push
    curl_cmd = [
        "curl", "-X", "PATCH",
        f"https://api.vercel.com/v1/edge-config/{EDGE_CONFIG_ID}/items",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(metrics_data),
        "--max-time", "30",  # 30 second timeout
    ]

    try:
        result = subprocess.run(
            curl_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            print(f"✅ OK: Pushed metrics at {timestamp}")
            return True
        else:
            print(f"❌ ERROR: {result.returncode} {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print(f"❌ ERROR: Request timed out after 30 seconds")
        return False
    except Exception as e:
        print(f"❌ ERROR: {e}")
        return False


if __name__ == "__main__":
    metrics = collect_metrics()
    push_to_vercel(metrics)
