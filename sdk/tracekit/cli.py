"""Tracekit command line interface."""

from __future__ import annotations

import argparse
import asyncio
import errno
import inspect
import json
import secrets
import sys
import webbrowser
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Sequence

from backend.apps.db.sqllite import SQLiteBackend
from tracekit.core.auth import (
    AuthError,
    generate_api_key,
    list_api_keys,
    revoke_api_key,
    validate_api_key,
)
from tracekit.core.db import TraceStore
from tracekit.core.projects import (
    archive_project,
    create_project,
    delete_project,
    get_project,
    list_projects,
)

PROJECT_COLUMNS = ("project_id", "name", "description", "archived", "created_at", "updated_at")
API_KEY_COLUMNS = ("key", "name", "project_id", "environment", "revoked", "expires_at", "created_at")


def _render_table(
    rows: Sequence[dict[str, Any]],
    columns: Sequence[str] | None = None,
) -> str:
    """Render rows as a simple fixed-width text table."""

    if not rows:
        return ""
    if columns is None:
        columns = list(rows[0].keys())
    columns = list(columns)
    if not columns:
        return ""

    def cell(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, default=str, sort_keys=True)
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    body = [[cell(row.get(column)) for column in columns] for row in rows]
    widths = [
        max(len(str(column)), *(len(line[index]) for line in body))
        for index, column in enumerate(columns)
    ]
    header = "  ".join(str(column).ljust(widths[index]) for index, column in enumerate(columns))
    divider = "  ".join("-" * width for width in widths)
    lines = [header, divider]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(line))
        for line in body
    )
    return "\n".join(lines)


def _print_rows(rows: Sequence[dict[str, Any]], output: str, columns: Sequence[str] | None = None) -> None:
    if output == "json":
        print(json.dumps(list(rows), indent=2, default=str))
    else:
        rendered = _render_table(rows, columns=columns)
        print(rendered if rendered else "No results")


def _serialize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _serialize(asdict(value))
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _to_row(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        data = asdict(value)
    elif isinstance(value, dict):
        data = dict(value)
    else:
        return {"result": value}
    return {key: _serialize(item) for key, item in data.items()}


def _print_result(row: Any, output: str, columns: Sequence[str] | None = None) -> None:
    _print_rows([_to_row(row)], output, columns=columns)


def _parse_datetime(value: str | None) -> datetime | str | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return value


def _filter_rows(rows: Iterable[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    """Portable fallback filtering for backends without search_traces."""

    result: list[dict[str, Any]] = []
    start = _parse_datetime(getattr(args, "start", None))
    end = _parse_datetime(getattr(args, "end", None))

    # Falls back to lexical timestamp comparison because stored values may be strings, not datetimes.
    def as_text(value: Any) -> str:
        return "" if value is None else str(value)

    for row in rows:
        if args.status and row.get("status") != args.status:
            continue
        if args.name and row.get("name") != args.name:
            continue
        project_id = getattr(args, "project_id", None)
        if project_id and row.get("project_id") != project_id:
            continue
        environment = getattr(args, "environment", None)
        if environment and row.get("environment") != environment:
            continue
        error_type = getattr(args, "error_type", None)
        if error_type:
            stored_type = row.get("error_type")
            if not stored_type and isinstance(row.get("error"), dict):
                stored_type = row["error"].get("type")
            if str(stored_type or "").lower() != error_type.lower():
                continue
        start_time = row.get("start_time")
        if start and start_time is not None and as_text(start_time) < as_text(start):
            continue
        if end and start_time is not None and as_text(start_time) > as_text(end):
            continue
        error = getattr(args, "error", None)
        if error is True and not row.get("error"):
            continue
        if error is False and row.get("error"):
            continue
        if isinstance(error, str) and error and error.lower() not in as_text(row.get("error")).lower():
            continue
        latency = row.get("latency_ms")
        if latency is None:
            latency = row.get("duration_ms")
        if latency is None:
            latency = row.get("latency")
        latency_value: float | None = None
        if latency is not None:
            try:
                latency_value = float(latency)
            except (TypeError, ValueError):
                latency_value = None
        min_latency = getattr(args, "min_latency_ms", None)
        max_latency = getattr(args, "max_latency_ms", None)
        if min_latency is not None and (
            latency_value is None or latency_value < min_latency
        ):
            continue
        if max_latency is not None and (
            latency_value is None or latency_value > max_latency
        ):
            continue
        result.append(row)
    return result


async def _search(store: TraceStore, args: argparse.Namespace) -> list[dict[str, Any]]:
    filters = {
        "status": args.status,
        "span_type": getattr(args, "span_type", None),
        "name": args.name,
        "start_time": _parse_datetime(getattr(args, "start", None)),
        "end_time": _parse_datetime(getattr(args, "end", None)),
        "error": getattr(args, "error", None),
        "error_type": getattr(args, "error_type", None),
        "project_id": getattr(args, "project_id", None),
        "environment": getattr(args, "environment", None),
        "min_latency_ms": getattr(args, "min_latency_ms", None),
        "max_latency_ms": getattr(args, "max_latency_ms", None),
        "limit": args.limit,
        "offset": getattr(args, "offset", 0),
    }
    # Prefer backend search; only stores without it pay for a broad fetch plus local filtering.
    search = getattr(store, "search_traces", None)
    if search is not None:
        return await search(**filters)
    rows = await store.list_traces(limit=max(args.limit + getattr(args, "offset", 0), 1000))
    rows = _filter_rows(rows, args)
    offset = getattr(args, "offset", 0)
    return rows[offset : offset + args.limit]


async def _open_store(args: argparse.Namespace) -> tuple[SQLiteBackend, TraceStore]:
    backend = SQLiteBackend(db_path=args.db)
    await backend.initialize()
    return backend, TraceStore(backend)


async def _watch_list(
    store: TraceStore,
    args: argparse.Namespace,
    *,
    iterations: int | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    # Trace ids are remembered across polls so an unchanged trace is not reprinted every interval.
    seen: set[str] = set()
    count = 0
    while iterations is None or count < iterations:
        rows = await _search(store, args)
        fresh = [row for row in rows if str(row.get("trace_id")) not in seen]
        for row in fresh:
            seen.add(str(row.get("trace_id")))
        if fresh or count == 0:
            _print_rows(fresh, args.output)
        count += 1
        if iterations is not None and count >= iterations:
            break
        await sleep(args.interval)


async def _cmd_list(args: argparse.Namespace) -> int:
    backend, store = await _open_store(args)
    try:
        if args.watch:
            await _watch_list(store, args)
        else:
            _print_rows(await _search(store, args), args.output)
    finally:
        await backend.close()
    return 0


async def _cmd_show(args: argparse.Namespace) -> int:
    backend, store = await _open_store(args)
    try:
        trace = await store.get_trace(args.trace_id)
        if trace is None:
            print(f"Trace not found: {args.trace_id}", file=sys.stderr)
            return 1
        spans = await store.get_spans_for_trace(args.trace_id)
        if args.output == "json":
            print(json.dumps({"trace": trace, "spans": spans}, indent=2, default=str))
        else:
            trace_row = {"trace_id": trace.get("trace_id"), "name": trace.get("name")}
            for key in ("status", "start_time", "end_time", "error"):
                if key in trace:
                    trace_row[key] = trace.get(key)
            print(_render_table([trace_row]))
            rendered = _render_table(spans)
            if rendered:
                print()
                print(rendered)
    finally:
        await backend.close()
    return 0


async def _cmd_replay(args: argparse.Namespace) -> int:
    backend, store = await _open_store(args)
    try:
        # The store reconstructs a parent/child tree from saved spans; nothing is re-executed here.
        replay = await store.get_trace_replay(args.trace_id)
        if replay is None:
            print(f"Trace not found: {args.trace_id}", file=sys.stderr)
            return 1
        if args.output == "json":
            print(json.dumps(replay, indent=2, default=str))
        else:
            print(_render_table(replay.get("spans", [])))
    finally:
        await backend.close()
    return 0


def _parse_days(value: str) -> float:
    text = value.strip().lower()
    multiplier = 1.0
    for suffix, factor in (("days", 1.0), ("day", 1.0), ("d", 1.0), ("weeks", 7.0), ("week", 7.0), ("w", 7.0)):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            multiplier = factor
            break
    try:
        days = float(text) * multiplier
    except ValueError as exc:
        raise ValueError(f"invalid duration: {value}") from exc
    if days < 0:
        raise ValueError("duration must be non-negative")
    return days


async def _cmd_delete(args: argparse.Namespace) -> int:
    trace_id = getattr(args, "trace_id", None) or getattr(args, "trace_id_opt", None)
    older_than = getattr(args, "older_than", None)
    if bool(trace_id) == bool(older_than):
        print("Provide either a trace id or --older-than", file=sys.stderr)
        return 2
    if not args.yes:
        print("Refusing to delete without --yes", file=sys.stderr)
        return 1
    if older_than is not None:
        try:
            days = _parse_days(older_than)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    backend, store = await _open_store(args)
    try:
        # Single-trace deletion removes the trace and its spans; --older-than deletes spans only.
        if trace_id:
            deleted = await store.delete_trace(trace_id)
            if not deleted:
                print(f"Trace not found: {trace_id}", file=sys.stderr)
                return 1
            message = f"Deleted trace {trace_id}"
            count = 1
        else:
            count = await store.delete_spans_older_than(days)
            message = f"Deleted {count} spans older than {older_than}"
        if args.output == "json":
            print(json.dumps({"deleted": count, "message": message}, indent=2))
        else:
            print(message)
    finally:
        await backend.close()
    return 0


async def _cmd_projects(args: argparse.Namespace) -> int:
    backend, _ = await _open_store(args)
    try:
        action = args.project_action
        if action == "list":
            projects = await list_projects(
                backend,
                include_archived=args.include_archived,
                limit=args.limit,
            )
            rows = [_to_row(project) for project in projects]
            _print_rows(rows, args.output, columns=PROJECT_COLUMNS)
        elif action == "show":
            project = await get_project(backend, args.project_id)
            if project is None:
                print(f"Project not found: {args.project_id}", file=sys.stderr)
                return 1
            _print_result(project, args.output, columns=PROJECT_COLUMNS)
        elif action == "create":
            project = await create_project(
                backend,
                args.name,
                project_id=args.project_id,
                description=args.description,
            )
            _print_result(project, args.output, columns=PROJECT_COLUMNS)
        elif action == "archive":
            archived = await archive_project(backend, args.project_id)
            if not archived:
                print(f"Project not found: {args.project_id}", file=sys.stderr)
                return 1
            _print_result(
                {"project_id": args.project_id, "archived": True},
                args.output,
                columns=("project_id", "archived"),
            )
        elif action == "delete":
            if not args.yes:
                print("Refusing to delete without --yes", file=sys.stderr)
                return 1
            # Without --cascade, deleting a project leaves its traces and spans in the store.
            deleted = await delete_project(
                backend,
                args.project_id,
                cascade_traces=args.cascade,
            )
            if not deleted:
                print(f"Project not found: {args.project_id}", file=sys.stderr)
                return 1
            _print_result(
                {"project_id": args.project_id, "deleted": True},
                args.output,
                columns=("project_id", "deleted"),
            )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        await backend.close()
    return 0


async def _cmd_api_keys(args: argparse.Namespace) -> int:
    backend, _ = await _open_store(args)
    try:
        action = args.api_key_action
        if action == "create":
            raw_key, record = await generate_api_key(
                backend,
                name=args.name,
                project_id=args.project_id,
                environment=args.environment,
                expires_at=_parse_datetime(args.expires_at),
            )
            row = _to_row(record)
            # Only the key's hash is persisted, so this response is the one chance to show the raw key.
            row["raw_key"] = raw_key
            _print_rows(
                [row],
                args.output,
                columns=("raw_key", *API_KEY_COLUMNS),
            )
        elif action == "validate":
            record = await validate_api_key(backend, args.key)
            _print_result(record, args.output, columns=API_KEY_COLUMNS)
        elif action == "list":
            records = await list_api_keys(
                backend,
                project_id=args.project_id,
                environment=args.environment,
                include_revoked=args.include_revoked,
            )
            _print_rows(
                [_to_row(record) for record in records],
                args.output,
                columns=API_KEY_COLUMNS,
            )
        elif action == "revoke":
            revoked = await revoke_api_key(backend, args.key_id)
            if not revoked:
                print(f"API key not found: {args.key_id}", file=sys.stderr)
                return 1
            _print_result(
                {"key": args.key_id, "revoked": True},
                args.output,
                columns=("key", "revoked"),
            )
    except AuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        await backend.close()
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    from tracekit.dashboard.server import start_dashboard_server, stop_dashboard_server

    # No --token means a random secret is minted and embedded in the printed dashboard URL.
    token = args.token or secrets.token_urlsafe(32)
    if args.host != "127.0.0.1":
        print(
            f"WARNING: Binding to {args.host} exposes the dashboard on your local network.",
            file=sys.stderr,
        )

    try:
        server, loop, loop_thread = start_dashboard_server(
            db_path=args.db,
            host=args.host,
            port=args.port,
            token=token,
        )
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(
                f"Error: port {args.port} is already in use. Try --port <other-port>.",
                file=sys.stderr,
            )
            return 2
        raise

    url = f"http://{args.host}:{args.port}/?token={token}"
    print(f"Dashboard running at: {url}", flush=True)
    if args.open:
        webbrowser.open(url)

    try:
        server.dashboard_http_thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        stop_dashboard_server(server, loop, loop_thread)
    return 0


def _add_filter_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--status")
    parser.add_argument("--span-type", "--span_type", dest="span_type")
    parser.add_argument("--name")
    parser.add_argument("--project-id", "--project_id", dest="project_id")
    parser.add_argument("--environment")
    parser.add_argument("--error-type", "--error_type", dest="error_type")
    parser.add_argument("--min-latency-ms", type=float, dest="min_latency_ms")
    parser.add_argument("--max-latency-ms", type=float, dest="max_latency_ms")
    parser.add_argument("--start", "--since", dest="start")
    parser.add_argument("--end", "--until", dest="end")
    parser.add_argument("--error", nargs="?", const=True, default=None)


def _add_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", choices=("json", "table"), default="table")


# Subparsers suppress their own --db default so the top-level value is not overwritten.
def _add_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", dest="db", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tracekit", description="Tracekit CLI")
    p.add_argument("--db", default=None, help="Path to SQLite database (default: ~/.tracellm/traces.db)")
    p.add_argument("--output", dest="global_output", choices=("json", "table"), default=None)
    sub = p.add_subparsers(dest="command", required=True)

    # list, search and watch share one handler; watch merely presets the polling flag.
    lp = sub.add_parser("list", help="List recent traces")
    _add_filter_arguments(lp)
    _add_output_argument(lp)
    _add_db_argument(lp)
    lp.add_argument("--watch", action="store_true")
    lp.add_argument("--interval", type=float, default=2.0)
    lp.set_defaults(func=_cmd_list)

    search = sub.add_parser("search", help="Search traces")
    _add_filter_arguments(search)
    _add_output_argument(search)
    _add_db_argument(search)
    search.add_argument("--watch", action="store_true")
    search.add_argument("--interval", type=float, default=2.0)
    search.set_defaults(func=_cmd_list)

    wp = sub.add_parser("watch", help="Watch new traces")
    _add_filter_arguments(wp)
    _add_output_argument(wp)
    _add_db_argument(wp)
    wp.add_argument("--interval", type=float, default=2.0)
    wp.set_defaults(func=_cmd_list, watch=True)

    sp = sub.add_parser("show", help="Show trace and spans")
    sp.add_argument("trace_id")
    _add_output_argument(sp)
    _add_db_argument(sp)
    sp.set_defaults(func=_cmd_show)

    rp = sub.add_parser("replay", help="Replay trace as tree")
    rp.add_argument("trace_id")
    _add_output_argument(rp)
    _add_db_argument(rp)
    rp.set_defaults(func=_cmd_replay)

    dp = sub.add_parser("delete", help="Delete a trace and its spans")
    dp.add_argument("trace_id", nargs="?")
    dp.add_argument("--trace-id", dest="trace_id_opt")
    dp.add_argument("--older-than")
    dp.add_argument("--yes", "--force", dest="yes", action="store_true")
    _add_output_argument(dp)
    _add_db_argument(dp)
    dp.set_defaults(func=_cmd_delete)

    projects = sub.add_parser("projects", help="Manage projects")
    project_actions = projects.add_subparsers(dest="project_action", required=True)

    pl = project_actions.add_parser("list", help="List projects")
    pl.add_argument("--include-archived", action="store_true")
    pl.add_argument("--limit", type=int, default=100)
    _add_output_argument(pl)
    _add_db_argument(pl)
    pl.set_defaults(func=_cmd_projects)

    pshow = project_actions.add_parser("show", help="Show a project")
    pshow.add_argument("project_id")
    _add_output_argument(pshow)
    _add_db_argument(pshow)
    pshow.set_defaults(func=_cmd_projects)

    pcreate = project_actions.add_parser("create", help="Create a project")
    pcreate.add_argument("name")
    pcreate.add_argument("--project-id", dest="project_id")
    pcreate.add_argument("--description", default="")
    _add_output_argument(pcreate)
    _add_db_argument(pcreate)
    pcreate.set_defaults(func=_cmd_projects)

    parchive = project_actions.add_parser("archive", help="Archive a project")
    parchive.add_argument("project_id")
    _add_output_argument(parchive)
    _add_db_argument(parchive)
    parchive.set_defaults(func=_cmd_projects)

    pdelete = project_actions.add_parser("delete", help="Delete a project")
    pdelete.add_argument("project_id")
    pdelete.add_argument("--cascade", action="store_true")
    pdelete.add_argument("--yes", "--force", dest="yes", action="store_true")
    _add_output_argument(pdelete)
    _add_db_argument(pdelete)
    pdelete.set_defaults(func=_cmd_projects)

    keys = sub.add_parser("api-keys", help="Manage API keys")
    key_actions = keys.add_subparsers(dest="api_key_action", required=True)

    kcreate = key_actions.add_parser("create", help="Create an API key")
    kcreate.add_argument("--name", default="")
    kcreate.add_argument("--project-id", dest="project_id")
    kcreate.add_argument("--environment")
    kcreate.add_argument("--expires-at")
    _add_output_argument(kcreate)
    _add_db_argument(kcreate)
    kcreate.set_defaults(func=_cmd_api_keys)

    kvalidate = key_actions.add_parser("validate", help="Validate an API key")
    kvalidate.add_argument("key")
    _add_output_argument(kvalidate)
    _add_db_argument(kvalidate)
    kvalidate.set_defaults(func=_cmd_api_keys)

    klist = key_actions.add_parser("list", help="List API key metadata")
    klist.add_argument("--project-id", dest="project_id")
    klist.add_argument("--environment")
    klist.add_argument("--include-revoked", action="store_true")
    _add_output_argument(klist)
    _add_db_argument(klist)
    klist.set_defaults(func=_cmd_api_keys)

    krevoke = key_actions.add_parser("revoke", help="Revoke an API key")
    krevoke.add_argument("key_id")
    _add_output_argument(krevoke)
    _add_db_argument(krevoke)
    krevoke.set_defaults(func=_cmd_api_keys)

    dashboard = sub.add_parser("dashboard", help="Serve a local trace dashboard")
    _add_db_argument(dashboard)
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8766)
    dashboard.add_argument("--open", action="store_true")
    dashboard.add_argument("--token", default=None)
    dashboard.set_defaults(func=_cmd_dashboard)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Global flags are copied onto the namespace because subcommand parsers own their own defaults.
    if getattr(args, "global_output", None):
        args.output = args.global_output
    if getattr(args, "db", None):
        args.db = str(Path(args.db).expanduser())
    elif not getattr(args, "db", None):
        from backend.apps.db.sqllite import default_db_path

        args.db = str(default_db_path())
    # Parsers bind a handler via set_defaults(func=...), including async ones that need a loop here.
    if inspect.iscoroutinefunction(args.func):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
