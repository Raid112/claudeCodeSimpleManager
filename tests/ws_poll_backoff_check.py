"""Idle PTY polling backs off instead of waking 100 times a second per tab.

Run: python tests/ws_poll_backoff_check.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from terminal.ws_server import IDLE_POLL_MAX, IDLE_POLL_MIN, idle_poll_delay  # noqa: E402

# The first empty read keeps the old 10 ms, so a tab that is streaming is not slowed down.
assert idle_poll_delay(0) == IDLE_POLL_MIN == 0.01
delays = [idle_poll_delay(n) for n in range(12)]
assert delays == sorted(delays), delays
# The cap bounds the delay before the first echo after the tab sat idle.
assert max(delays) == IDLE_POLL_MAX == 0.05, delays
assert idle_poll_delay(10_000) == IDLE_POLL_MAX

print("ws_poll_backoff_check: ok")
