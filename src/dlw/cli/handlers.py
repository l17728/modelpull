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


def _context_cmd(args) -> int:
    from dlw.sdk import _config as cfgmod
    cfgpath = args.config
    sub = getattr(args, "context_cmd", None)
    if sub == "set":
        p = cfgmod.set_context(args.name, server=args.server, token=args.token,
                               make_current=not args.no_current,
                               config_path=cfgpath)
        if not args.quiet:
            sys.stdout.write(f"wrote context '{args.name}' to {p}\n")
        return 0
    if sub == "use":
        cfgmod.use_context(args.name, config_path=cfgpath)   # UsageError→main()→exit 2
        if not args.quiet:
            sys.stdout.write(f"switched to context '{args.name}'\n")
        return 0
    cfg = cfgmod.load_config(cfgpath)
    cur = cfg.get("current_context")
    if sub == "list":
        sys.stdout.write(f"# config: {cfgmod._resolve_write_path(cfgpath)}\n")
        ctxs = cfg.get("contexts") or {}
        if not ctxs:
            sys.stdout.write("(no contexts)\n")
        for name, c in ctxs.items():
            tok = ((cfg.get("auth") or {}).get(name) or {}).get("access_token")
            mark = " (current)" if name == cur else ""
            sys.stdout.write(f"{name}{mark}: server={c.get('server')} "
                             f"token={'set' if tok else 'unset'}\n")
        return 0
    if sub == "current":
        if not cur:
            sys.stdout.write("(no current context)\n")
            return 0
        c = (cfg.get("contexts") or {}).get(cur) or {}
        tok = ((cfg.get("auth") or {}).get(cur) or {}).get("access_token")
        sys.stdout.write(f"{cur}: server={c.get('server')} "
                         f"token={'set' if tok else 'unset'}\n")
        return 0
    sys.stderr.write("usage: dlw context [list|current|use NAME|set NAME ...]\n")
    return 2


def _watch_sse(client, args, emit) -> int:
    import json
    import time

    import httpx

    from dlw.sdk._http import raise_for_status
    from dlw.sdk.errors import Timeout

    # --interval is server-driven now: warn if non-default.
    if getattr(args, "interval", 5.0) != 5.0:
        sys.stderr.write(
            "warning: --interval is deprecated and ignored "
            "(the server drives the 1 Hz stream tick)\n")
    deadline = (time.monotonic() + args.timeout) if args.timeout else None
    last = None
    terminal = {"succeeded", "failed", "cancelled"}
    try:
        with client.tasks.task_stream(args.task_id, timeout=args.timeout) as r:
            if r.status_code != 200:
                r.read()
                raise_for_status(r)        # 404→NotFound→exit 3, etc.
            for line in r.iter_lines():
                if deadline and time.monotonic() > deadline:
                    raise Timeout("watch timed out")
                if not line.startswith("data: "):
                    continue
                detail = json.loads(line[len("data: "):])
                last = detail
                subs = detail.get("subtasks") or []
                done = sum(1 for s in subs if s.get("status") == "succeeded")
                sys.stdout.write(f"{detail.get('status')} {done}/{len(subs)}\n")
                sys.stdout.flush()
                if detail.get("status") in terminal:
                    break
    except httpx.TimeoutException as exc:        # stalled stream → exit 9
        raise Timeout("watch stream timed out") from exc
    # Normalize the emit shape: fall back to GET if no terminal snapshot received.
    if last is None or last.get("status") not in terminal:
        last = client.tasks.get(args.task_id).raw   # safety net
    emit(last, args)
    return 1 if last.get("status") == "failed" else 0


def run(args: argparse.Namespace, make_client: Callable,
        emit: Callable, emit_obj: Callable) -> int:
    if args.cmd == "context":
        return _context_cmd(args)
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
            return _watch_sse(client, args, emit)
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
