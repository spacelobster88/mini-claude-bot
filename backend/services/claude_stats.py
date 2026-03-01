"""Parse Claude CLI stats from ~/.claude/stats-cache.json."""
import json
from pathlib import Path


def read_claude_stats(stats_path: str | None = None) -> dict:
    path = Path(stats_path or Path.home() / ".claude" / "stats-cache.json")
    if not path.exists():
        return {"error": "stats file not found"}

    data = json.loads(path.read_text())

    return {
        "last_computed_date": data.get("lastComputedDate"),
        "total_sessions": data.get("totalSessions", 0),
        "total_messages": data.get("totalMessages", 0),
        "first_session_date": data.get("firstSessionDate"),
        "model_usage": {
            model: {
                "input_tokens": u.get("inputTokens", 0),
                "output_tokens": u.get("outputTokens", 0),
                "cache_read_tokens": u.get("cacheReadInputTokens", 0),
                "cache_creation_tokens": u.get("cacheCreationInputTokens", 0),
                "web_search_requests": u.get("webSearchRequests", 0),
                "cost_usd": u.get("costUSD", 0),
            }
            for model, u in data.get("modelUsage", {}).items()
        },
        "daily_activity": [
            {
                "date": d["date"],
                "messages": d.get("messageCount", 0),
                "sessions": d.get("sessionCount", 0),
                "tool_calls": d.get("toolCallCount", 0),
            }
            for d in data.get("dailyActivity", [])
        ],
        "daily_model_tokens": [
            {"date": d["date"], "tokens_by_model": d.get("tokensByModel", {})}
            for d in data.get("dailyModelTokens", [])
        ],
        "hour_counts": data.get("hourCounts", {}),
    }
