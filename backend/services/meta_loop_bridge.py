"""Bridge to AROS Meta Loop service — fire-and-forget event emission and status caching."""
import json
import logging
import time
import threading
import httpx

logger = logging.getLogger(__name__)

META_LOOP_URL = "http://localhost:8200"
STATUS_CACHE_TTL = 60  # seconds


class MetaLoopBridge:
    """Fire-and-forget bridge to the AROS Meta Loop service."""

    def __init__(self, base_url: str = META_LOOP_URL):
        self.base_url = base_url
        self._status_cache: dict | None = None
        self._status_cache_time: float = 0

    def emit_event(self, bot_id: str, event_type: str, session_id: str | None = None, data: dict | None = None) -> None:
        """Fire-and-forget: POST event to meta-loop webhook."""
        def _send():
            try:
                with httpx.Client(timeout=5.0) as client:
                    client.post(f"{self.base_url}/api/meta-loop/event", json={
                        "bot_id": bot_id,
                        "event_type": event_type,
                        "session_id": session_id,
                        "data": data,
                    })
            except Exception as e:
                logger.debug(f"Meta-loop event emission failed (non-fatal): {e}")

        threading.Thread(target=_send, daemon=True).start()

    def trigger_cycle(self, trigger: str = "harness_complete") -> None:
        """Fire-and-forget: trigger a meta-loop cycle."""
        def _send():
            try:
                with httpx.Client(timeout=5.0) as client:
                    client.post(f"{self.base_url}/api/meta-loop/trigger", json={"trigger": trigger})
            except Exception as e:
                logger.debug(f"Meta-loop trigger failed (non-fatal): {e}")

        threading.Thread(target=_send, daemon=True).start()

    def switch_cadence(self, aggressive: bool) -> None:
        """Switch meta-loop cadence mode (for Nirmana activation/deactivation)."""
        def _send():
            try:
                with httpx.Client(timeout=5.0) as client:
                    client.post(f"{self.base_url}/api/meta-loop/nirmana", params={"activate": str(aggressive).lower()})
            except Exception as e:
                logger.debug(f"Meta-loop cadence switch failed (non-fatal): {e}")

        threading.Thread(target=_send, daemon=True).start()

    def get_status_cached(self) -> dict | None:
        """Get meta-loop status with 60s TTL cache. Returns None if unavailable."""
        now = time.time()
        if self._status_cache and (now - self._status_cache_time) < STATUS_CACHE_TTL:
            return self._status_cache

        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(f"{self.base_url}/api/meta-loop/status")
                resp.raise_for_status()
                self._status_cache = resp.json()
                self._status_cache_time = now
                return self._status_cache
        except Exception:
            return self._status_cache  # Return stale cache if available

    def format_context_injection(self) -> str:
        """Format meta-loop status for context injection (<500 chars)."""
        status = self.get_status_cached()
        if not status:
            return ""

        parts = [f"[Meta-Loop: {status.get('cadence_mode', 'unknown')} mode"]
        if status.get("running"):
            parts.append(", cycle running")

        last = status.get("last_cycle")
        if last:
            parts.append(f", last cycle #{last.get('cycle_num', '?')}: {last.get('status', '?')}")

        pending = status.get("pending_approvals", 0)
        if pending:
            parts.append(f", {pending} pending approvals")

        scores = status.get("meta_goal_scores")
        if scores and isinstance(scores, dict):
            agg = scores.get("aggregate")
            if agg is not None:
                parts.append(f", aggregate score: {agg}")
            below = scores.get("below_threshold", [])
            if below:
                parts.append(f", below threshold: {', '.join(below)}")

        parts.append("]")
        result = "".join(parts)
        return result[:500]


# Singleton instance
_bridge: MetaLoopBridge | None = None

def get_bridge() -> MetaLoopBridge:
    global _bridge
    if _bridge is None:
        _bridge = MetaLoopBridge()
    return _bridge
