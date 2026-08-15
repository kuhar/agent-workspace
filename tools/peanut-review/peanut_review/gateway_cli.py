"""Remote-session dispatch for the normal peanut-review CLI."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from . import gateway, models, session as sess

SESSION_PREFIX = "peanut://"
SUPPORTED_COMMANDS = {
    "status", "comments", "notes", "add-comment", "add-global-comment",
    "note", "signal",
}


def session_id_from_locator(value: str | None) -> str | None:
    if not value or not value.startswith(SESSION_PREFIX):
        return None
    session_id = value[len(SESSION_PREFIX):]
    if not session_id or "/" in session_id:
        raise gateway.GatewayError("invalid remote session locator")
    return session_id


def _body_from_args(args) -> str:
    if getattr(args, "body_file", None):
        try:
            return Path(args.body_file).read_text()
        except OSError as exc:
            raise gateway.GatewayError(f"could not read --body-file: {exc}") from exc
    if getattr(args, "body", None) is not None:
        return args.body
    raise gateway.GatewayError("--body or --body-file is required")


def _note_body(args) -> str:
    has_message = args.message is not None
    has_file = args.file is not None
    if has_message == has_file:
        raise gateway.GatewayError("exactly one of --message or --file is required")
    if has_message:
        return args.message
    if args.file == "-":
        return sys.stdin.read()
    try:
        return Path(args.file).read_text()
    except OSError as exc:
        raise gateway.GatewayError(f"could not read --file: {exc}") from exc


def _query(args, names: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        value = getattr(args, name, None)
        if isinstance(value, bool):
            result[name] = "true" if value else None
        else:
            result[name] = value
    return result


def _print_comments(rows: list[dict[str, Any]], args) -> None:
    if args.format == "json":
        print(json.dumps(rows, indent=2))
        return
    comments = [models.Comment.from_json(json.dumps(row)) for row in rows]
    if not comments:
        print("No comments found.")
        return
    header = f"{'ID':<14} {'Agent':<10} {'Sev':<10} {'Cat':<15} {'File':<30} {'Line':>5}    {'Body'}"
    print(header)
    print("-" * len(header))
    for comment in comments:
        if comment.deleted:
            flag = "X"
        elif comment.resolved:
            flag = "R"
        elif comment.stale:
            flag = "*"
        elif comment.edited_at:
            flag = "E"
        else:
            flag = " "
        body = comment.body[:60].replace("\n", " ")
        file_column = "[global]" if comment.file == sess.GLOBAL_FILE else comment.file
        line_column = "" if comment.file == sess.GLOBAL_FILE else str(comment.line)
        print(f"{comment.id:<14} {comment.author:<10} {comment.severity:<10} {comment.category:<15} {file_column:<30} {line_column:>5} {flag}  {body}")


def _print_notes(rows: list[dict[str, Any]], args) -> None:
    if args.format == "json":
        print(json.dumps(rows, indent=2))
        return
    notes = [models.Note.from_json(json.dumps(row)) for row in rows]
    if not notes:
        print("No reports found.")
        return
    header = f"{'ID':<14} {'Agent':<10} {'Timestamp':<32} {'Body'}"
    print(header)
    print("-" * len(header))
    for note in notes:
        body = note.body[:80].replace("\n", " ")
        print(f"{note.id:<14} {note.author:<10} {note.timestamp:<32} {body}")


def _print_status(payload: dict[str, Any]) -> None:
    session = payload["session"]
    print(f"Session:  {session['id']}")
    print(f"Base:     {session['base']}")
    print(f"Head:     {session['head'][:12]}")
    print(f"Workspace: {session['workspace']}")
    if session["repo"] != session["workspace"]:
        print(f"Repo:      {session['repo']}")
    print()
    print("Agents:")
    for agent in payload["agents"]:
        transport = f" ssh={agent['ssh_target']}" if agent.get("ssh_target") else " local"
        details = " ".join(agent.get("details") or [])
        print(
            f"  {agent['name']:<12} {agent['status']:<8} {agent['model']:<22} "
            f"process={agent['process_state']:<9} review={agent['protocol_status']:<7}"
            f"{transport} {details}"
        )
    counts = payload["comments"]
    if counts["total"] or counts["deleted"]:
        print()
        print(
            "Comments: "
            f"{counts['total']} total, {counts['critical']} critical, "
            f"{counts['resolved']} resolved, {counts['stale']} stale, "
            f"{counts['deleted']} deleted"
        )
    if payload["notes"]:
        print()
        print(f"Reports: {payload['notes']}")
    if payload["signals"]:
        print()
        print(f"Signals: {', '.join(payload['signals'])}")


def maybe_dispatch(args) -> int | None:
    locator = getattr(args, "session", None) or os.environ.get("PEANUT_SESSION")
    session_id = session_id_from_locator(locator)
    if session_id is None:
        return None
    if args.command not in SUPPORTED_COMMANDS:
        print(
            f"Error: {args.command} is not available through a reviewer capability",
            file=sys.stderr,
        )
        return 1
    url = os.environ.get("PEANUT_REVIEW_GATEWAY_URL")
    token = os.environ.get("PEANUT_REVIEW_GATEWAY_TOKEN")
    if not url or not token:
        print(
            "Error: remote sessions require PEANUT_REVIEW_GATEWAY_URL and "
            "PEANUT_REVIEW_GATEWAY_TOKEN",
            file=sys.stderr,
        )
        return 1
    client = gateway.GatewayClient(url, token, session_id)
    try:
        hello = client.request("GET", "hello")
        if hello.get("protocol") != gateway.PROTOCOL_VERSION:
            raise gateway.GatewayError(
                f"gateway protocol mismatch: local={gateway.PROTOCOL_VERSION} "
                f"remote={hello.get('protocol')}"
            )
        if args.command == "status":
            _print_status(client.request("GET", "status"))
        elif args.command == "comments":
            rows = client.request("GET", "comments", query=_query(args, [
                "agent", "file", "severity", "category", "since",
                "unresolved", "include_deleted",
            ]))
            _print_comments(rows, args)
        elif args.command == "notes":
            rows = client.request("GET", "notes", query=_query(args, ["agent", "since"]))
            _print_notes(rows, args)
        elif args.command in {"add-comment", "add-global-comment"}:
            payload = {
                "body": _body_from_args(args),
                "severity": args.severity,
                "category": args.category,
                "file": getattr(args, "file", None),
                "line": getattr(args, "line", None),
                "end_line": getattr(args, "end_line", None),
                "reply_to": getattr(args, "reply_to", None),
                "global": args.command == "add-global-comment" or getattr(args, "global_", False),
            }
            print(client.request("POST", "comments", payload=payload)["message"])
        elif args.command == "note":
            print(client.request("POST", "notes", payload={"body": _note_body(args)})["message"])
        elif args.command == "signal":
            print(client.request("POST", "signals", payload={"event": args.event})["message"])
        return 0
    except gateway.GatewayError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
