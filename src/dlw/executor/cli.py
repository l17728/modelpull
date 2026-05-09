"""CLI entry point for dlw-executor.

Wires up SIGTERM/SIGINT handlers and runs ExecutorRunner.run() until shutdown.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from dlw.executor.client import ControllerClient
from dlw.executor.config import ExecutorSettings
from dlw.executor.downloader import HfS3StreamDownloader
from dlw.executor.runner import ExecutorRunner


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dlw-executor",
        description="modelpull executor — polls controller for download work",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return p.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    settings = ExecutorSettings()
    client = ControllerClient(
        base_url=settings.controller_url, bearer_token=settings.bearer_token,
    )
    downloader = HfS3StreamDownloader(settings=settings)
    runner = ExecutorRunner(settings=settings, client=client, downloader=downloader)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, runner.request_shutdown)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler — Ctrl-C handled in main()
            pass

    async with client:
        await runner.run()
    return 0


def main() -> int:
    """Entry point. Catches KeyboardInterrupt OUTSIDE asyncio.run because on
    Windows asyncio.run catches Ctrl-C internally and re-raises after task
    cancellation — the inner except KeyboardInterrupt is unreachable (W3-B).
    """
    args = _parse_args()
    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        return 0  # graceful exit; asyncio.run already cancelled child tasks


if __name__ == "__main__":
    sys.exit(main())
