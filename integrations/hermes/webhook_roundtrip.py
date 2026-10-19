"""Operator diagnostic: POST a signed event to the Hermes webhook route.

Exists because `hermes webhook test` cannot exercise a STATIC route (it only
reads dynamic subscriptions and signs in the GitHub format, not the generic V2
one). See `.hermes/plans/20260803-hermes-webhook-contract.md` sections 1 and 12.

It deliberately reuses `terminal.hermes_events.sign_v2` and the same secret
accessor the publisher uses: a diagnostic that signs with its own copy of the
logic would prove nothing about the publisher.

    python integrations/hermes/webhook_roundtrip.py --mode filtered   # no LLM turn
    python integrations/hermes/webhook_roundtrip.py --mode accepted   # wakes Hermes
    python integrations/hermes/webhook_roundtrip.py --mode badsig     # expect 401
    python integrations/hermes/webhook_roundtrip.py --mode badroute   # expect 404

`filtered` sends `hermes_initiated: true`, which the route's declarative filter
rejects — that still proves HMAC, route resolution, profile binding and filter
evaluation, without spending a token. Every outcome is HTTP 200/202, so the
RESPONSE BODY is the real signal:

    {"status": "accepted"}                      -> agent turn started
    {"status": "ignored", "reason": "filter"}   -> declarative filter rejected
    {"status": "ignored", "reason": "script"}   -> filter script rejected (dedup/cooldown)
    {"status": "ignored", "event": "..."}       -> `events:` allowlist rejected
    {"status": "duplicate"}                     -> gateway-side delivery idempotency

Read `%LOCALAPPDATA%\\hermes\\logs\\gateway.log` after a rejection: that is where
a filter or script decision explains itself.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from terminal import secrets_store
from terminal.hermes_events import (
    DEFAULT_HEALTH_URL,
    DEFAULT_WEBHOOK_URL,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    event_id_for,
    sign_v2,
)


def _payload(hermes_initiated: bool) -> dict:
    """A realistic `task_completed` body. Optional fields are "" never null:
    Hermes' prompt template renders a null as the literal "None"."""
    now = time.time()
    return {
        "event_id": event_id_for("roundtrip-session", "Stop", now, None),
        "event_type": "task_completed",
        "session_key": "roundtrip-session",
        "work_item_id": "",
        "claude_session_id": "",
        "status": "ready",
        "tool_name": "",
        "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "source": "claude_hook",
        "hermes_initiated": hermes_initiated,
        "last_hermes_write_age_s": "",
    }


def _post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError as exc:
        return 0, f"connection failed: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="filtered",
                        choices=("filtered", "accepted", "badsig", "badroute"))
    parser.add_argument("--url", default=DEFAULT_WEBHOOK_URL)
    args = parser.parse_args()

    secret = secrets_store.hermes_webhook_secret()
    if not secret:
        print("no hermes_webhook.secret in secrets.json -- publisher would stay off")
        return 2

    try:
        with urllib.request.urlopen(DEFAULT_HEALTH_URL, timeout=5) as response:
            print(f"health: {response.status} {response.read().decode('utf-8')}")
    except OSError as exc:
        print(f"health: unreachable ({exc}) -- is `hermes gateway run` up?")
        return 2

    url = args.url
    if args.mode == "badroute":
        url = url.rsplit("/", 1)[0] + "/route-that-does-not-exist"

    payload = _payload(hermes_initiated=(args.mode == "filtered"))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    signature = sign_v2(secret, ts, body)
    if args.mode == "badsig":
        signature = "0" * 64

    print(f"mode={args.mode} event_id={payload['event_id'][:12]} "
          f"hermes_initiated={payload['hermes_initiated']}")
    status, text = _post(url, body, {
        "Content-Type": "application/json",
        SIGNATURE_HEADER: signature,
        TIMESTAMP_HEADER: ts,
    })
    print(f"-> HTTP {status} {text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
