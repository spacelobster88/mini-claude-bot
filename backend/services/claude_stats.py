"""Parse Claude CLI stats from session JSONL files."""
import json
import os
import glob
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

PST = timezone(timedelta(hours=-8))


def read_claude_stats() -> dict:
    """Scan all session JSONL files for live usage data."""
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return {
            "total_sessions": 0, "total_messages": 0, "total_requests": 0,
            "first_session_date": None, "model_usage": {},
            "daily_activity": [], "context_avg": 0, "context_max": 0,
        }

    daily = defaultdict(lambda: {"messages": 0, "sessions": set()})
    model_totals = defaultdict(lambda: {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0, "requests": 0,
    })
    context_sizes = []
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
                        if msg_type not in ("user", "assistant") or not ts:
                            continue

                        total_messages += 1
                        utc_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        date = utc_dt.astimezone(PST).strftime("%Y-%m-%d")
                        daily[date]["messages"] += 1
                        daily[date]["sessions"].add(session_id)
                        if first_date is None or date < first_date:
                            first_date = date
                        if last_date is None or date > last_date:
                            last_date = date

                        # Parse token usage from assistant messages
                        if msg_type == "assistant":
                            msg = d.get("message", {})
                            if not isinstance(msg, dict):
                                continue
                            usage = msg.get("usage", {})
                            model = msg.get("model", "unknown")
                            if not usage or model == "<synthetic>":
                                continue
                            inp = usage.get("input_tokens", 0)
                            out = usage.get("output_tokens", 0)
                            cr = usage.get("cache_read_input_tokens", 0)
                            cw = usage.get("cache_creation_input_tokens", 0)
                            model_totals[model]["input_tokens"] += inp
                            model_totals[model]["output_tokens"] += out
                            model_totals[model]["cache_read_tokens"] += cr
                            model_totals[model]["cache_creation_tokens"] += cw
                            model_totals[model]["requests"] += 1
                            ctx = inp + cr + cw
                            if ctx > 0:
                                context_sizes.append(ctx)
            except (OSError, IOError):
                continue

    sorted_dates = sorted(daily.keys())
    activity = [
        {"date": d, "messages": daily[d]["messages"], "sessions": len(daily[d]["sessions"])}
        for d in sorted_dates
    ]

    return {
        "total_sessions": total_sessions,
        "total_messages": total_messages,
        "total_requests": len(context_sizes),
        "first_session_date": first_date,
        "model_usage": dict(model_totals),
        "daily_activity": activity,
        "context_avg": sum(context_sizes) // len(context_sizes) if context_sizes else 0,
        "context_max": max(context_sizes) if context_sizes else 0,
    }
