#!/usr/bin/env python3
"""Report/create missing sweep jobs for legacy persistent deposits."""

import argparse
import asyncio
import json

from src.db.session import get_session_context
from src.maintenance.legacy_sweep_drain import (
    build_legacy_sweep_drain_report,
    create_missing_persistent_sweep_jobs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect legacy persistent deposit sweep drain state.",
    )
    parser.add_argument("--chain", help="Optional chain filter from chains.toml")
    parser.add_argument(
        "--create-missing-jobs",
        action="store_true",
        help="Create missing UnifiedSweepJob rows for confirmed legacy deposits.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Allow writes when --create-missing-jobs is set. Omit for dry run.",
    )
    parser.add_argument("--limit", type=int, help="Max missing deposits to process.")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    async with get_session_context() as session:
        report = await build_legacy_sweep_drain_report(session, chain=args.chain)
        output = {"report": report.to_dict()}
        if args.create_missing_jobs:
            result = await create_missing_persistent_sweep_jobs(
                session,
                chain=args.chain,
                execute=args.execute,
                limit=args.limit,
            )
            output["create_missing_jobs"] = result.to_dict()

    print(json.dumps(output, ensure_ascii=False, sort_keys=True))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
