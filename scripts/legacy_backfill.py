#!/usr/bin/env python3
"""Run fixed-range legacy persistent address backfill."""

import argparse
import asyncio
import json

from src.db.session import get_session_context
from src.maintenance.legacy_backfill import backfill_legacy_chain


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Re-scan legacy persistent wallet addresses for a fixed block range.",
    )
    parser.add_argument("--chain", required=True, help="Chain name from chains.toml")
    parser.add_argument("--from-block", type=int, required=True)
    parser.add_argument("--to-block", type=int, required=True)
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include inactive user wallets and wallet addresses.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write missing deposits. Omit for dry run.",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    async with get_session_context() as session:
        result = await backfill_legacy_chain(
            session,
            args.chain,
            args.from_block,
            args.to_block,
            execute=args.execute,
            only_active=not args.include_inactive,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
