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


def _follow_events(client, args) -> int:
    from dlw.sdk._http import raise_for_status
    max_ticks = getattr(args, "max_ticks", None)   # tests pass a bound
    with client.tasks.events_stream(args.task_id, max_ticks=max_ticks) as r:
        if r.status_code != 200:
            r.read()                # materialize the error body before mapping
            raise_for_status(r)     # 404→NotFound→exit 3, etc.
        for line in r.iter_lines():
            if line.startswith("data: "):
                sys.stdout.write(line[len("data: "):] + "\n")
                sys.stdout.flush()
    return 0


def run(args: argparse.Namespace, make_client: Callable,
        emit: Callable, emit_obj: Callable) -> int:
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
        if args.cmd == "whoami":
            emit_obj(client.me(), args)
            return 0
        if args.cmd == "quota":
            emit_obj(client.quota.current(), args)
            return 0
        if args.cmd == "exec":
            if getattr(args, "exec_cmd", None) == "list":
                emit_obj(client.executors.list(status=args.status), args,
                         cols=["id", "status", "health_score", "host_id",
                               "disk_free_gb"])
                return 0
            sys.stderr.write("usage: dlw exec list\n")
            return 2
        if args.cmd == "events":
            if args.follow:
                return _follow_events(client, args)
            emit_obj(client.tasks.events(
                args.task_id, limit=args.limit, cursor=args.cursor), args,
                cols=["ts", "type", "message"])
            return 0
        if args.cmd == "audit":
            emit_obj(client.audit.search(
                action=args.action, actor_user_id=args.actor,
                from_=args.from_, to=args.to, limit=args.limit,
                cursor=args.cursor), args,
                cols=["occurred_at", "actor_user_id", "action",
                      "resource_type", "outcome"])
            return 0
        raise NotImplementedError(args.cmd)
    finally:
        client.close()
