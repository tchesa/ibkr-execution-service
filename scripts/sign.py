"""Sign and send one request to the service, the way the strategy bot would.

    uv run python scripts/sign.py bot  POST /signals '{"signal_id": ...}'
    uv run python scripts/sign.py admin POST /halt
    uv run python scripts/sign.py bot  GET  /positions

Secrets come from HMAC_BOT_SECRET / HMAC_ADMIN_SECRET; the base URL from SERVICE_URL
(default http://127.0.0.1:8000).
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

from ibkr_exec.api.auth import sign


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    key, method, path = argv[0], argv[1].upper(), argv[2]
    body = argv[3].encode() if len(argv) > 3 else b""
    secret = os.environ.get(f"HMAC_{key.upper()}_SECRET")
    if not secret:
        print(f"HMAC_{key.upper()}_SECRET is not set", file=sys.stderr)
        return 2
    ts = str(int(time.time()))
    headers = {
        "X-Key-Id": key,
        "X-Timestamp": ts,
        "X-Signature": sign(secret, method, path, ts, body),
        "Content-Type": "application/json",
    }
    base = os.environ.get("SERVICE_URL", "http://127.0.0.1:8000")
    r = httpx.request(method, base + path, content=body, headers=headers, timeout=30)
    print(r.status_code)
    try:
        print(json.dumps(r.json(), indent=2))
    except ValueError:
        print(r.text)
    return 0 if r.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
