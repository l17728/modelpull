"""dlw-seed CLI — insert reproducible dev/demo data into the controller DB.

Usage:
  dlw-seed --default     # 4-row Phase 1 default seed (idempotent)
  dlw-seed --demo        # seed_default + 1 pending DownloadTask
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker

from dlw.db.session import get_engine, reset_engine
from dlw.fixtures import seed_default, seed_demo_data

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dlw-seed",
        description="Insert reproducible dev/demo data into the modelpull DB.",
    )
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--default",
        action="store_true",
        help="Insert the 4-row Phase 1 default seed (idempotent)",
    )
    grp.add_argument(
        "--demo",
        action="store_true",
        help="seed_default + 1 demo task pointing to alpha demo model (idempotent)",
    )
    return p.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    try:
        async with factory() as session:
            if args.default:
                await seed_default(session)
                action = "default seed"
            else:  # args.demo
                await seed_demo_data(session)
                action = "demo seed"
            await session.commit()
        print(f"[dlw-seed] {action} applied.")
        return 0
    finally:
        await reset_engine()


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_async_main(args))
    except Exception as e:
        logger.exception("dlw-seed failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
