#!/usr/bin/env python3
"""Refresh Vercel CLI token and update the dashboard's VERCEL_API_TOKEN env var.

The Vercel CLI stores a short-lived token (~12h) with a refresh token in:
  ~/Library/Application Support/com.vercel.cli/auth.json

This script:
1. Checks if the token is expiring soon (< 2 hours remaining)
2. Uses the refresh token to get a new token
3. Updates the Vercel project env var so the dashboard can write to Edge Config
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

AUTH_FILE = Path.home() / "Library" / "Application Support" / "com.vercel.cli" / "auth.json"
DASHBOARD_DIR = Path.home() / "Projects" / "mini-claude-bot" / "dashboard"
REFRESH_THRESHOLD = 7200  # refresh if < 2 hours remaining


def load_auth() -> dict:
    return json.loads(AUTH_FILE.read_text())


def save_auth(auth: dict):
    AUTH_FILE.write_text(json.dumps(auth, indent=2))


def token_remaining_seconds(auth: dict) -> float:
    return auth.get("expiresAt", 0) - time.time()


def refresh_token(auth: dict) -> dict:
    """Use Vercel API to refresh the token."""
    import httpx

    resp = httpx.post(
        "https://api.vercel.com/registration/token/refresh",
        json={"refreshToken": auth["refreshToken"]},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"ERROR: refresh failed: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    auth["token"] = data["token"]
    auth["expiresAt"] = data["expiresAt"]
    if "refreshToken" in data:
        auth["refreshToken"] = data["refreshToken"]
    return auth


def update_vercel_env(token: str):
    """Update VERCEL_API_TOKEN in the Vercel project."""
    for env in ["production", "development"]:
        # Remove old
        subprocess.run(
            ["vercel", "env", "rm", "VERCEL_API_TOKEN", env, "-y"],
            cwd=DASHBOARD_DIR, capture_output=True, text=True,
        )
        # Add new
        result = subprocess.run(
            ["vercel", "env", "add", "VERCEL_API_TOKEN", env],
            input=token, cwd=DASHBOARD_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"ERROR: failed to set env for {env}: {result.stderr}", file=sys.stderr)


def main():
    auth = load_auth()
    remaining = token_remaining_seconds(auth)
    hours = remaining / 3600

    print(f"Token expires in {hours:.1f}h")

    if remaining > REFRESH_THRESHOLD:
        print("Token still valid, no refresh needed.")
        return

    print("Refreshing token...")
    auth = refresh_token(auth)
    save_auth(auth)
    new_remaining = token_remaining_seconds(auth) / 3600
    print(f"New token expires in {new_remaining:.1f}h")

    print("Updating Vercel dashboard env var...")
    update_vercel_env(auth["token"])
    print("Done.")


if __name__ == "__main__":
    main()
