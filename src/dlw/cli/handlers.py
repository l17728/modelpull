"""CLI subcommand handlers (SP4)."""
from __future__ import annotations

import argparse
from typing import Callable


def run(args: argparse.Namespace, make_client: Callable,
        emit: Callable) -> int:
    client = make_client(args)        # may raise UsageError (missing token)
    try:
        raise NotImplementedError(args.cmd)   # filled in Task 9/10
    finally:
        client.close()
