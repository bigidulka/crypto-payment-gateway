#!/usr/bin/env python3
"""
Онбординг мерчанта одной командой.

Создаёт мерчанта, API-ключ и вебхук через admin API и печатает секреты
ровно один раз - в БД лягут только хеши.

    python3 scripts/onboard_merchant.py --name "My Shop" \
        --email ops@myshop.io \
        [--webhook-url https://myshop.io/webhooks/arbitron] \
        [--api http://127.0.0.1:8123] [--admin-key ...]

ADMIN_SECRET_KEY берётся из .env, если --admin-key не задан.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def call(api: str, path: str, *, admin_key: str, body: dict | None = None):
    req = urllib.request.Request(
        f"{api}{path}",
        data=json.dumps(body).encode() if body else None,
        headers={"Content-Type": "application/json", "X-Admin-Key": admin_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8123")
    parser.add_argument("--admin-key", default="")
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--webhook-url", default="")
    args = parser.parse_args()

    admin_key = args.admin_key
    if not admin_key:
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("ADMIN_SECRET_KEY="):
                admin_key = line.split("=", 1)[1].strip()
                break
    if not admin_key:
        print("нет ADMIN_SECRET_KEY: передайте --admin-key", file=sys.stderr)
        return 1

    merchant = call(
        args.api,
        "/v1/admin/merchants",
        admin_key=admin_key,
        body={"name": args.name, "email": args.email},
    )
    print(f"мерчант:    {merchant['merchant_id']} ({merchant['name']})")
    print(f"API-ключ:   {merchant['api_key']}")
    print(f"префикс:    {merchant['key_prefix']}")

    if args.webhook_url:
        webhook = call(
            args.api,
            f"/v1/admin/merchants/{merchant['merchant_id']}/webhooks",
            admin_key=admin_key,
            body={"url": args.webhook_url},
        )
        print(f"вебхук:     {webhook['webhook_id']} -> {webhook['url']}")
        print(f"webhook secret: {webhook['secret']}")

    print("\nКлючи показаны единственный раз - сохраните сейчас.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
