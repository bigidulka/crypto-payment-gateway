#!/usr/bin/env python3
"""
Сводка средств: сколько лежит на депозитных адресах и на main-кошельке.

Читает адреса из БД, спрашивает у RPC натив и оба стейбла по каждой сети
через multicall3 и печатает, где деньги. Только чтение.

    python3 scripts/balance_report.py
    python3 scripts/balance_report.py --chains bsc,base --dust 0.01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SECRET_KEY", "x" * 40)
os.environ.setdefault("ENCRYPTION_KEY", "x" * 40)

from src.blockchain.chains import get_chain_config, get_evm_chains  # noqa: E402

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL_GET_ETH_BALANCE = "4d2301cc"  # getEthBalance(address)
SEL_BALANCE_OF = "70a08231"  # balanceOf(address)
SEL_AGGREGATE3 = "82ad56cb"  # aggregate3((address,bool,bytes)[])

HUB_URL = os.environ.get("HUB_URL", "https://hub.arbitron.dev")
HUB_KEY = os.environ.get("HUBKEY", "")


def _word(value: int) -> str:
    return f"{value:064x}"


def _addr_word(address: str) -> str:
    return _word(int(address, 16))


def encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """calls = [(target, calldata_hex_without_0x)] -> calldata for aggregate3."""
    head, tail = [], []
    # Смещение до массива от начала аргументов
    offset_to_array = 32
    tuple_offsets_size = 32 * len(calls)
    running = tuple_offsets_size
    encoded_tuples = []

    for target, data in calls:
        data_bytes = len(data) // 2
        padded = data + "0" * ((32 - data_bytes % 32) % 32 * 2)
        body = (
            _addr_word(target)
            + _word(0)  # allowFailure = false
            + _word(96)  # смещение до bytes внутри кортежа
            + _word(data_bytes)
            + padded
        )
        encoded_tuples.append(body)

    for body in encoded_tuples:
        head.append(_word(running))
        running += len(body) // 2
        tail.append(body)

    return (
        SEL_AGGREGATE3
        + _word(offset_to_array)
        + _word(len(calls))
        + "".join(head)
        + "".join(tail)
    )


def decode_aggregate3(result_hex: str, count: int) -> list[int]:
    """Вернуть list[uint256] из aggregate3, считая все вызовы успешными."""
    raw = result_hex[2:] if result_hex.startswith("0x") else result_hex
    words = [raw[i : i + 64] for i in range(0, len(raw), 64)]
    if not words:
        return [0] * count

    array_offset = int(words[0], 16) // 32
    length = int(words[array_offset], 16)
    tuple_offsets = [
        int(words[array_offset + 1 + i], 16) // 32 for i in range(length)
    ]

    values = []
    base = array_offset + 1
    for offset in tuple_offsets:
        head = base + offset
        success = int(words[head], 16)
        data_offset = int(words[head + 1], 16) // 32
        data_start = head + data_offset
        data_len = int(words[data_start], 16)
        if not success or data_len < 32:
            values.append(0)
        else:
            values.append(int(words[data_start + 1], 16))
    return values


async def rpc(chain: str, method: str, params: list, tries: int = 4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(
        f"{HUB_URL}/rpc/{chain}",
        data=body.encode(),
        headers={
            "X-Hub-Key": HUB_KEY,
            "Content-Type": "application/json",
            "User-Agent": "curl/8.5.0",
        },
    )
    for attempt in range(tries):
        try:
            payload = await asyncio.to_thread(
                lambda: json.loads(urllib.request.urlopen(req, timeout=90).read())
            )
            if "error" in payload:
                raise RuntimeError(payload["error"])
            return payload["result"]
        except Exception:
            if attempt == tries - 1:
                raise
            await asyncio.sleep(2 * (attempt + 1))


async def scan_chain(chain: str, addresses: list[str], batch: int):
    """Вернуть {asset: {address: Decimal}} для одной сети."""
    config = get_chain_config(chain)
    assets: list[tuple[str, str | None, int]] = [
        (config.native_symbol, None, config.native_decimals)
    ]
    for symbol, token in config.tokens.items():
        assets.append((symbol, token.contract_address, token.decimals))

    calls: list[tuple[str, str]] = []
    index: list[tuple[str, str]] = []
    for symbol, contract, _ in assets:
        for address in addresses:
            if contract is None:
                calls.append((MULTICALL3, SEL_GET_ETH_BALANCE + _addr_word(address)))
            else:
                calls.append((contract, SEL_BALANCE_OF + _addr_word(address)))
            index.append((symbol, address))

    decimals = {symbol: dec for symbol, _, dec in assets}
    out: dict[str, dict[str, Decimal]] = defaultdict(dict)

    for start in range(0, len(calls), batch):
        chunk = calls[start : start + batch]
        data = encode_aggregate3(chunk)
        result = await rpc(
            chain, "eth_call", [{"to": MULTICALL3, "data": "0x" + data}, "latest"]
        )
        values = decode_aggregate3(result, len(chunk))
        for (symbol, address), raw in zip(index[start : start + batch], values):
            if raw:
                out[symbol][address] = Decimal(raw) / Decimal(10 ** decimals[symbol])
        await asyncio.sleep(0.2)

    return chain, out


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--addresses", default="/tmp/dep_addrs.csv")
    parser.add_argument("--main", default="")
    parser.add_argument("--chains", default="")
    parser.add_argument("--batch", type=int, default=120)
    parser.add_argument("--dust", type=Decimal, default=Decimal("0"))
    args = parser.parse_args()

    if not HUB_KEY:
        sys.exit("нужен HUBKEY в окружении")

    rows = Path(args.addresses).read_text().splitlines()
    deposits: dict[str, str] = {}
    for row in rows:
        if not row.strip():
            continue
        parts = row.split(",")
        deposits[parts[0].strip().lower()] = (
            parts[1].strip() if len(parts) > 1 else "unknown"
        )

    targets = list(deposits)
    main_address = args.main.lower() if args.main else ""
    if main_address and main_address not in deposits:
        targets.append(main_address)

    chains = (
        [c.strip() for c in args.chains.split(",") if c.strip()]
        if args.chains
        else get_evm_chains()
    )

    print(f"адресов: {len(deposits)} депозитных" + (" + main" if main_address else ""))
    print(f"сетей: {len(chains)} -> {', '.join(chains)}")
    print()

    results = await asyncio.gather(
        *(scan_chain(chain, targets, args.batch) for chain in chains),
        return_exceptions=True,
    )

    totals: dict[str, Decimal] = defaultdict(Decimal)
    main_totals: dict[str, Decimal] = defaultdict(Decimal)
    stuck: list[tuple[str, str, str, Decimal, str]] = []

    for outcome in results:
        if isinstance(outcome, Exception):
            print(f"ОШИБКА: {outcome}")
            continue
        chain, balances = outcome
        for symbol, holders in balances.items():
            for address, amount in holders.items():
                if amount <= args.dust:
                    continue
                if address == main_address:
                    main_totals[symbol] += amount
                else:
                    totals[symbol] += amount
                    stuck.append(
                        (chain, address, symbol, amount, deposits.get(address, "?"))
                    )

    if main_address:
        print(f"MAIN {main_address}")
        print("-" * 78)
        if main_totals:
            for symbol, amount in sorted(main_totals.items()):
                print(f"  {symbol:6s} {amount:>22.8f}")
        else:
            print("  пусто")
        print()

    print("ДЕПОЗИТНЫЕ АДРЕСА")
    print("-" * 78)
    if stuck:
        print(f"{'сеть':10s} {'адрес':44s} {'актив':6s} {'сумма':>16s}  статус")
        for chain, address, symbol, amount, status in sorted(
            stuck, key=lambda r: (-r[3], r[0])
        ):
            print(f"{chain:10s} {address:44s} {symbol:6s} {amount:>16.8f}  {status}")
        print()
        for symbol, amount in sorted(totals.items()):
            print(f"  итого {symbol:6s} {amount:>22.8f}")
    else:
        print("  пусто — все средства выметены")


if __name__ == "__main__":
    asyncio.run(main())
