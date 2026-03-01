"""Parse Claude CLI stats from session files and stats-cache.json."""
import json
import os
import glob
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

PST = timezone(timedelta(hours=-8))


def _scan_sessions() -> dict:
    """Scan JSONL session files for live daily activity data."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return {"total_sessions": 0, "total_messages": 0, "daily_activity": []}

    daily = defaultdict(lambda: {"messages": 0, "sessions": set()})
    total_sessions = 0
    total_messages = 0
    first_date = None
    last_date = None

    for proj_dir in glob.glob(str(base / "*")):
        for f in glob.glob(os.path.join(proj_dir, "*.jsonl")):
            session_id = os.path.basename(f).replace(".jsonl", "")
            total_sessions += 1
            try:
                with open(f) as fh:
                    for line in fh:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = d.get("timestamp")
                        msg_type = d.get("type", "")
                        if msg_type in ("user", "assistant") and ts:
                            total_messages += 1
                            # Convert UTC timestamp to PST for date grouping
                            utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            date = utc_dt.astimezone(PST).strftime("%Y-%m-%d")
                            daily[date]["messages"] += 1
                            daily[date]["sessions"].add(session_id)
                            if first_date is None or date < first_date:
                                first_date = date
                            if last_date is None or date > last_date:
                                last_date = date
            except (OSError, IOError):
                continue

    sorted_dates = sorted(daily.keys())
    activity = [
        {
            "date": d,
            "messages": daily[d]["messages"],
            "sessions": len(daily[d]["sessions"]),
        }
        for d in sorted_dates
    ]

    return {
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "daily_activity": activity,
        "first_date": first_date,
        "last_date": last_date,
    }


def _read_cache(stats_path: str | None = None) -> dict:
    """Read token usage from stats-cache.json (may be stale)."""
    path = Path(stats_path or Path.home() / ".claude" / "stats-cache.json")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    return {
        "model_usage": {
            model: {
                "input_tokens": u.get("inputTokens", 0),
                "output_tokens": u.get("outputTokens", 0),
                "cache_read_tokens": u.get("cacheReadInputTokens", 0),
                "cache_creation_tokens": u.get("cacheCreationInputTokens", 0),
                "cost_usd": u.get("costUSD", 0),
            }
            for model, u in data.get("modelUsage", {}).items()
        },
        "cache_computed_date": data.get("lastComputedDate"),
    }


def read_claude_stats(stats_path: str | None = None) -> dict:
    """Combine live session data with cached token usage."""
    live = _scan_sessions()
    cache = _read_cache(stats_path)

    return {
        "total_sessions": live["total_sessions"],
        "total_messages": live["total_messages"],
        "first_session_date": live.get("first_date"),
        "last_computed_date": cache.get("cache_computed_date"),
        "model_usage": cache.get("model_usage", {}),
        "daily_activity": live["daily_activity"],
    }
