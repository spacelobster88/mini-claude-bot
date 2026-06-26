"""Tests for the coupled gateway timeout constants (15-min queue wait).

The module-level constants are read from env at import, and this suite may run
INSIDE the live gateway process (which sets GATEWAY_* overrides). So the
default-value checks run in a clean-env subprocess; the invariant check uses
whatever is actually configured (it must hold in every environment).
"""

import os
import subprocess
import sys
from pathlib import Path

from backend.services import session_manager as sm

REPO = Path(__file__).resolve().parent.parent  # tests/ -> repo root


def _defaults_in_clean_env():
    """Import the module with all GATEWAY_* timeout overrides removed → code defaults."""
    env = dict(os.environ)
    for k in ("GATEWAY_QUEUE_WAIT_TIMEOUT", "GATEWAY_BUSY_STUCK_TIMEOUT", "GATEWAY_CLAUDE_TIMEOUT"):
        env.pop(k, None)
    env["PYTHONPATH"] = str(REPO)
    code = (
        "from backend.services import session_manager as sm;"
        "print(sm.QUEUE_WAIT_TIMEOUT, sm.BUSY_STUCK_TIMEOUT, sm.CLAUDE_TIMEOUT)"
    )
    out = subprocess.check_output([sys.executable, "-c", code], env=env, cwd=str(REPO))
    q, b, c = map(int, out.split())
    return q, b, c


def test_queue_wait_default_is_15_min():
    q, _, _ = _defaults_in_clean_env()
    assert q == 900


def test_busy_stuck_default():
    _, b, _ = _defaults_in_clean_env()
    assert b == 960


def test_default_invariants():
    """Default stuck-reset exceeds default queue-wait AND default claude timeout."""
    q, b, c = _defaults_in_clean_env()
    assert q <= b
    assert b > c


def test_configured_queue_wait_within_stuck():
    """Whatever is actually configured, a queued waiter must not outlive the stuck-reset."""
    assert sm.QUEUE_WAIT_TIMEOUT <= sm.BUSY_STUCK_TIMEOUT
