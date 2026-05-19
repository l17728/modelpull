"""CLI subcommand handlers (SP4) — thin SDK calls + render."""
from __future__ import annotations

import argparse
import sys
from typing import Callable


def _task_dict(t) -> dict:
    return {"id": t.id, "repo_id": t.repo_id, "revision": t.revision,
            "status": t.status, "priority": t.priority,
            "created_at": t.created_at, "completed_at": t.completed_at,
            "error_message": t.error_message}


def run(args: argparse.Namespace, make_client: Callable,
        emit: Callable) -> int:
    client = make_client(args)
    try:
        if args.cmd == "submit":
            t = client.tasks.submit(
                repo_id=args.repo, revision=args.revision,
                storage_id=args.storage, priority=args.priority,
                source_strategy=args.strategy,
                upgrade_from_revision=args.upgrade_from)
            if args.wait:
                t = t.wait(timeout=args.timeout)
                if t.status == "failed":
                    sys.stderr.write(
                        f"task {t.id} failed: {t.error_message}\n")
                    emit(_task_dict(t), args)
                    return 1
            emit(_task_dict(t), args)
            return 0
        if args.cmd == "show":
            emit(_task_dict(client.tasks.get(args.task_id)), args)
            return 0
        if args.cmd == "list":
            tasks = client.tasks.list(status=args.status)
            emit([_task_dict(t) for t in tasks], args)
            return 0
        if args.cmd == "cancel":
            client.tasks.cancel(args.task_id, reason=args.reason)
            if not args.quiet:
                sys.stdout.write(f"cancelling {args.task_id}\n")
            return 0
        if args.cmd == "delete":
            client.tasks.delete(args.task_id)
            if not args.quiet:
                sys.stdout.write(f"deleted {args.task_id}\n")
            return 0
        if args.cmd == "watch":
            t = client.tasks.get(args.task_id)
            t = t.wait(timeout=args.timeout, poll_interval=args.interval,
                       on_progress=lambda x: sys.stdout.write(
                           f"{x.status} "
                           f"{x.files_done()[0]}/{x.files_done()[1]}\n"))
            emit(_task_dict(t), args)
            return 1 if t.status == "failed" else 0
        raise NotImplementedError(args.cmd)
    finally:
        client.close()
