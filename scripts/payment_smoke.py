#!/usr/bin/env python3
"""
Смоук платёжных сценариев: выставить счета, дождаться, замерить.

Скрипт не двигает средства. Он готовит счета, печатает точный список
переводов и потом сам проверяет исход каждого сценария и время зачисления.
Отправку подписывает оператор.

    python scripts/payment_smoke.py plan --chains bsc,base --amount 0.10
    python scripts/payment_smoke.py watch

Состояние между запусками лежит в tmp/payment_smoke_state.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "tmp" / "payment_smoke_state.json"
DEFAULT_API = "http://127.0.0.1:8123"

# Сценарий -> (множитель к сумме счёта, чем платить, ожидаемый исход)
SCENARIOS = {
    "exact": (Decimal("1"), "invoice_token", "оплачен"),
    "overpay": (Decimal("1.05"), "invoice_token", "оплачен, зачислено больше суммы счёта"),
    "underpay_ok": (Decimal("0.995"), "invoice_token", "оплачен, недоплата в допуске"),
    "underpay_big": (Decimal("0.80"), "invoice_token", "не оплачен, mismatch_reason=underpaid"),
    "wrong_token": (Decimal("1"), "other_token", "не оплачен, mismatch_reason=wrong_token"),
}


@dataclass
class Case:
    scenario: str
    chain: str
    token: str
    public_id: str
    deposit_address: str
    invoice_amount: str
    send_amount: str
    send_token: str
    expectation: str
    created_at: str
    resolved: bool = False
    outcome: str = ""


def api_call(api: str, method: str, path: str, *, key: str = "", body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        f"{api}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {exc.read()[:300]!r}")


def read_env_key() -> str:
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("ARBITRON_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("ARBITRON_API_KEY не найден в .env")


def load_state() -> list[Case]:
    if not STATE_PATH.exists():
        return []
    return [Case(**row) for row in json.loads(STATE_PATH.read_text())]


def save_state(cases: list[Case]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps([asdict(c) for c in cases], indent=2))


def chain_tokens(api: str, chain: str) -> list[str]:
    info = api_call(api, "GET", f"/v1/public/chain/{chain}")
    return [t["symbol"] for t in info["tokens"]]


def cmd_plan(args) -> None:
    key = read_env_key()
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = set(scenarios) - set(SCENARIOS)
    if unknown:
        raise SystemExit(f"неизвестные сценарии: {sorted(unknown)}")

    cases: list[Case] = [] if args.reset else load_state()

    for chain in [c.strip() for c in args.chains.split(",") if c.strip()]:
        tokens = chain_tokens(api=args.api, chain=chain)
        if not tokens:
            print(f"{chain}: нет токенов, пропуск")
            continue
        invoice_token = args.token if args.token in tokens else tokens[0]
        other_token = next((t for t in tokens if t != invoice_token), "")

        for scenario in scenarios:
            multiplier, pay_with, expectation = SCENARIOS[scenario]
            if pay_with == "other_token" and not other_token:
                print(f"{chain}: второго токена нет, пропускаю {scenario}")
                continue

            created = api_call(
                args.api,
                "POST",
                "/v1/invoices",
                key=key,
                body={
                    "amount": str(args.amount),
                    "asset": invoice_token,
                    "allowed_chains": [chain],
                    "ttl_minutes": args.ttl,
                },
            )
            public_id = created["public_id"]
            selected = api_call(
                args.api,
                "POST",
                f"/pay/{public_id}/select",
                body={"chain": chain, "token": invoice_token},
            )

            send_amount = (Decimal(str(args.amount)) * multiplier).quantize(
                Decimal("0.000001")
            )
            cases.append(
                Case(
                    scenario=scenario,
                    chain=chain,
                    token=invoice_token,
                    public_id=public_id,
                    deposit_address=selected["deposit_address"],
                    invoice_amount=str(args.amount),
                    send_amount=str(send_amount),
                    send_token=(
                        invoice_token if pay_with == "invoice_token" else other_token
                    ),
                    expectation=expectation,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )

    save_state(cases)
    print_plan([c for c in cases if not c.resolved])


def print_plan(cases: list[Case]) -> None:
    if not cases:
        print("нечего отправлять")
        return
    print()
    print("ОТПРАВЬ С ФАНДЕР-КОШЕЛЬКА:")
    print("-" * 100)
    for case in cases:
        print(
            f"{case.chain:9s} {case.send_amount:>12s} {case.send_token:5s} "
            f"-> {case.deposit_address}"
        )
        print(f"{'':9s} сценарий={case.scenario}  счёт={case.public_id}")
        print(f"{'':9s} ожидание: {case.expectation}")
    print("-" * 100)
    print("после отправки:  python scripts/payment_smoke.py watch")
    print()
    print("Сценарий topup проверяется вручную: заплати по счёту underpay_big")
    print("остаток тем же токеном на тот же адрес — счёт должен закрыться.")


def cmd_watch(args) -> None:
    cases = load_state()
    if not cases:
        raise SystemExit("нет запланированных сценариев, запусти plan")

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        pending = [c for c in cases if not c.resolved]
        if not pending:
            break
        for case in pending:
            status = api_call(args.api, "GET", f"/pay/{case.public_id}/status")
            outcome = evaluate(case, status)
            if outcome:
                case.resolved = True
                case.outcome = outcome
                elapsed = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(case.created_at)
                ).total_seconds()
                print(f"[{case.chain}/{case.scenario}] {outcome}  ({elapsed:.0f}с)")
                save_state(cases)
        time.sleep(args.interval)

    print()
    print("ИТОГ")
    print("-" * 100)
    for case in cases:
        mark = "ok" if case.resolved else "ждём"
        print(f"{mark:5s} {case.chain:9s} {case.scenario:14s} {case.outcome or case.expectation}")
    save_state(cases)


def evaluate(case: Case, status: dict) -> str:
    """Вернуть текст исхода, если сценарий уже разрешился."""
    state = str(status.get("status", "")).upper()
    if state == "CONFIRMED":
        received = status.get("received_amount")
        return f"оплачен, зачислено {received or status.get('amount')}"
    if status.get("mismatch_reason") == "underpaid":
        return (
            f"недоплата: пришло {status.get('received_amount')}, "
            f"не хватает {status.get('missing_amount')}"
        )
    if status.get("mismatch_reason") == "wrong_token":
        return (
            f"чужой токен: пришло {status.get('received_amount')} "
            f"{status.get('mismatch_token')}"
        )
    if state == "EXPIRED":
        return "истёк без оплаты"
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="выставить счета под сценарии")
    plan.add_argument("--chains", default="bsc")
    plan.add_argument("--token", default="USDT")
    plan.add_argument("--amount", default="0.10")
    plan.add_argument("--ttl", type=int, default=60)
    plan.add_argument("--scenarios", default=",".join(SCENARIOS))
    plan.add_argument("--reset", action="store_true", help="забыть прошлый прогон")
    plan.set_defaults(func=cmd_plan)

    watch = sub.add_parser("watch", help="дождаться исходов и замерить время")
    watch.add_argument("--interval", type=int, default=5)
    watch.add_argument("--timeout", type=int, default=1800)
    watch.set_defaults(func=cmd_watch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
