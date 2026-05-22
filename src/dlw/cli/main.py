"""`dlw` CLI (Phase 3 SP4) — argparse front-end over dlw.sdk.

CLI-is-SDK: every handler builds a dlw.sdk.Client and calls it. Tests set
`dlw.cli.main._transport` to an httpx transport; production leaves it
None (real network)."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from dlw.sdk import errors as e
from dlw.sdk.client import Client

_VERSION = "0.1.0-alpha"
_transport: Any = None          # test seam; None in production


def _make_client(args: argparse.Namespace) -> Client:
    return Client(server=args.server, token=args.token,
                  config_path=args.config, transport=_transport)


def _print_err(exc: e.DlwError, as_json: bool) -> None:
    if as_json:
        sys.stderr.write(json.dumps({
            "code": exc.code, "message": exc.message,
            "trace_id": exc.trace_id, "details": exc.details}) + "\n")
        return
    sys.stderr.write(f"Error: {exc.message}\n")
    if exc.code:
        sys.stderr.write(f"Code:  {exc.code}\n")
    if exc.trace_id:
        sys.stderr.write(f"Trace: {exc.trace_id}\n")
    if exc.details:
        sys.stderr.write("Details:\n")
        for k, v in exc.details.items():
            sys.stderr.write(f"  - {k}: {v}\n")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dlw",
        description="dlw — distributed HuggingFace model downloader CLI")
    p.add_argument("--version", action="store_true",
                   help="print version and exit")
    p.add_argument("--server", default=None, help="API base URL")
    p.add_argument("--token", default=None, help="bearer token")
    p.add_argument("-c", "--config", default=None, help="config file path")
    p.add_argument("-o", "--output", choices=["table", "json"],
                   default="table")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("submit", help="create a download task")
    s.add_argument("repo")
    s.add_argument("-r", "--revision", required=True)
    s.add_argument("-s", "--storage", type=int, required=True)
    s.add_argument("--priority", type=int, default=1)
    s.add_argument("--strategy", default="auto_balance")
    s.add_argument("--upgrade-from", default=None)
    s.add_argument("--wait", action="store_true")
    s.add_argument("--timeout", type=float, default=None)

    sub.add_parser("list", help="list tasks").add_argument(
        "--status", default=None)

    g = sub.add_parser("show", help="show one task")
    g.add_argument("task_id")

    cc = sub.add_parser("cancel", help="cancel a task")
    cc.add_argument("task_id")
    cc.add_argument("--reason", default=None)

    sub.add_parser("delete", help="delete a terminal task").add_argument(
        "task_id")

    w = sub.add_parser("watch", help="stream a task until terminal")
    w.add_argument("task_id")
    w.add_argument("--interval", type=float, default=5.0,
                   help="(deprecated, ignored — server drives the 1 Hz stream tick)")
    w.add_argument("--timeout", type=float, default=None)

    ctx = sub.add_parser("context", help="manage CLI contexts")
    ctx_sub = ctx.add_subparsers(dest="context_cmd")
    ctx_sub.add_parser("list", help="list contexts (marks current)")
    ctx_sub.add_parser("current", help="show the current context")
    ctx_use = ctx_sub.add_parser("use", help="switch current context")
    ctx_use.add_argument("name")
    ctx_set = ctx_sub.add_parser("set", help="create/update a context")
    ctx_set.add_argument("name")
    ctx_set.add_argument("--server", default=None)
    ctx_set.add_argument("--token", default=None)
    ctx_set.add_argument("--no-current", action="store_true",
                         help="do not switch current_context to this one")

    sub.add_parser("whoami", help="show the current principal")
    sub.add_parser("quota", help="show current tenant quota usage")

    ex = sub.add_parser("exec", help="executor commands")
    ex_sub = ex.add_subparsers(dest="exec_cmd")
    ex_ls = ex_sub.add_parser("list", help="list executors")
    ex_ls.add_argument("--status", default=None)

    ev = sub.add_parser("events", help="show task events")
    ev.add_argument("task_id")
    ev.add_argument("--limit", type=int, default=50)
    ev.add_argument("--cursor", default=None)
    ev.add_argument("--follow", action="store_true",
                    help="stream events via SSE until Ctrl-C / disconnect")
    ev.add_argument("--max-ticks", type=int, default=None,
                    help=argparse.SUPPRESS)   # test bound for --follow

    au = sub.add_parser("audit", help="search the audit log")
    au.add_argument("--action", default=None)
    au.add_argument("--actor", type=int, default=None)
    au.add_argument("--from", dest="from_", default=None)
    au.add_argument("--to", default=None)
    au.add_argument("--limit", type=int, default=50)
    au.add_argument("--cursor", default=None)
    return p


def _emit(obj: Any, args: argparse.Namespace) -> None:
    if args.output == "json":
        sys.stdout.write(json.dumps(obj, default=str) + "\n")
        return
    rows = obj if isinstance(obj, list) else [obj]
    if not rows:
        sys.stdout.write("(no tasks)\n")
        return
    cols = ["id", "repo_id", "revision", "status", "priority"]
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows))
              for c in cols}
    line = "  ".join(c.ljust(widths[c]) for c in cols)
    sys.stdout.write(line + "\n")
    for r in rows:
        sys.stdout.write("  ".join(
            str(r.get(c, "")).ljust(widths[c]) for c in cols) + "\n")


def _emit_obj(obj: Any, args: argparse.Namespace,
              cols: list[str] | None = None) -> None:
    if args.output == "json":
        sys.stdout.write(json.dumps(obj, default=str) + "\n")
        return
    if isinstance(obj, dict) and isinstance(obj.get("items"), list):
        _emit_rows(obj["items"], cols)
    elif isinstance(obj, list):
        _emit_rows(obj, cols)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            sys.stdout.write(f"{k}: {v}\n")
    else:
        sys.stdout.write(str(obj) + "\n")


def _emit_rows(rows: list, cols: list[str] | None = None) -> None:
    if not rows:
        sys.stdout.write("(none)\n")
        return
    if cols is None:
        cols = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
    widths = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows))
              for c in cols}
    sys.stdout.write("  ".join(c.ljust(widths[c]) for c in cols) + "\n")
    for r in rows:
        sys.stdout.write("  ".join(
            str(r.get(c, "")).ljust(widths[c]) for c in cols) + "\n")


def _dispatch(args: argparse.Namespace) -> int:
    from dlw.cli import handlers
    return handlers.run(args, _make_client, _emit, _emit_obj)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    argv = sys.argv[1:] if argv is None else argv
    if "--version" in argv:
        sys.stdout.write(f"dlw {_VERSION}\n")
        return 0
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    if not args.cmd:
        parser.print_help(sys.stderr)
        return 2
    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nAborted.\n")
        return 8
    except e.DlwError as exc:
        _print_err(exc, args.output == "json")
        return e.exit_code_for(exc)


if __name__ == "__main__":
    raise SystemExit(main())
