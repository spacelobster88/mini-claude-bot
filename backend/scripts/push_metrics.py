#!/usr/bin/env python3
"""Collect metrics from local API and push to Vercel dashboard."""
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
DASHBOARD_PUSH_URL = os.getenv("DASHBOARD_PUSH_URL")
METRICS_SECRET = os.getenv("METRICS_SECRET")


def main():
    if not DASHBOARD_PUSH_URL or not METRICS_SECRET:
        print("ERROR: DASHBOARD_PUSH_URL and METRICS_SECRET must be set in .env")
        sys.exit(1)

    # Fetch from local API
    metrics = httpx.get(f"{API_BASE}/api/metrics", timeout=30).json()

    # Push to Vercel
    resp = httpx.post(
        DASHBOARD_PUSH_URL,
        json=metrics,
        headers={"Authorization": f"Bearer {METRICS_SECRET}"},
        timeout=30,
    )

    if resp.status_code == 200:
        print(f"OK: pushed metrics at {metrics['timestamp']}")
    else:
        print(f"ERROR: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    main()
