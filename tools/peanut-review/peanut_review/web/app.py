"""HTTP server for a peanut-review session — human review UI.

Routes are session-indexed from day 1 (`/sessions/<id>/...`). Multi-session
hosting is a single-line change: drop the one-session redirect in
`handle_root` and mount additional sessions via `SessionRegistry`.
"""
from __future__ import annotations

import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import store
from ..models import AgentStatus, Comment, Severity
from ..session import (
    load_session,
    refresh_agent_statuses,
    save_session,
    validate_comment_location,
)
from . import diff as diffmod
from .render import render_page


class SessionRegistry:
    """Map session-id → session-dir. Single-entry today; multi-session ready."""

    def __init__(self) -> None:
        self._by_id: dict[str, Path] = {}

    def bind(self, session_dir: str | Path) -> str:
        sdir = Path(session_dir)
        s = load_session(sdir)
        self._by_id[s.id] = sdir
        return s.id

    def get(self, session_id: str) -> Path | None:
        return self._by_id.get(session_id)

    def only(self) -> str | None:
        return next(iter(self._by_id)) if len(self._by_id) == 1 else None


def _git_head(workspace: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", workspace, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return None


def _auto_migrate_if_shifted(session_dir: Path) -> tuple[bool, str | None]:
    """If the workspace HEAD moved, mark stale + update current_head.

    Returns (shifted, new_head). `shifted` is True only when we actually
    migrated on this call.
    """
    s = load_session(session_dir)
    live = _git_head(s.workspace)
    if not live or live == s.current_head:
        return False, live
    store.mark_stale(session_dir)
    s.current_head = live
    save_session(session_dir, s)
    return True, live


ROUTE_RE = re.compile(r"^/sessions/([^/]+)(/.*)?$")
VALID_SEVERITIES = {s.value for s in Severity}


class _Handler(BaseHTTPRequestHandler):
    # Server attribute injected at construction — see make_server.
    registry: SessionRegistry

    # -------- helpers --------

    def _json(self, code: int, data) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None

    def _resolve_session(self, session_id: str) -> Path | None:
        return self.registry.get(session_id)

    def log_message(self, fmt, *args):  # noqa: A003  (shadowing base method by design)
        # Match BaseHTTPRequestHandler format, but route through stderr only —
        # we're a dev server, don't need request logs on stdout.
        import sys
        sys.stderr.write("[pr-web] " + fmt % args + "\n")

    # -------- routing --------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("", "/"):
            sid = self.registry.only()
            if sid is None:
                self._text(404, "No session bound; see /sessions/<id>/")
                return
            self.send_response(302)
            self.send_header("Location", f"/sessions/{sid}/")
            self.end_headers()
            return

        m = ROUTE_RE.match(url.path)
        if not m:
            self._error(404, f"no route for {url.path}")
            return
        session_id, tail = m.group(1), (m.group(2) or "/")
        session_dir = self._resolve_session(session_id)
        if session_dir is None:
            self._error(404, f"unknown session: {session_id}")
            return

        # Refresh agent statuses and auto-migrate on any session-scoped GET.
        session = load_session(session_dir)
        refresh_agent_statuses(session_dir, session)
        shifted, _ = _auto_migrate_if_shifted(session_dir)

        if tail in ("/", ""):
            comments = store.read_all_comments(session_dir)
            files = diffmod.parse_diff(
                session.workspace, session.base_ref, session.topic_ref,
            )
            html_out = render_page(
                load_session(session_dir), session_id, files, comments,
                head_shifted=shifted,
            )
            self._html(200, html_out)
            return

        if tail == "/api/session":
            session = load_session(session_dir)
            comments = store.read_all_comments(session_dir)
            payload = {
                "id": session.id,
                "state": session.state,
                "base_ref": session.base_ref,
                "topic_ref": session.topic_ref,
                "original_head": session.original_head,
                "current_head": session.current_head,
                "workspace": session.workspace,
                "agents": [
                    {"name": a.name, "model": a.model, "status": a.status,
                     "runner": a.runner}
                    for a in session.agents
                ],
                "comment_count": len(comments),
                "stale_count": sum(1 for c in comments if c.stale),
                "critical_count": sum(1 for c in comments if c.severity == "critical"),
                "head_shifted": shifted,
            }
            self._json(200, payload)
            return

        if tail == "/api/comments":
            q = parse_qs(url.query)
            comments = store.read_all_comments(session_dir)
            filtered = store.filter_comments(
                comments,
                agent=(q.get("agent", [None])[0]),
                file=(q.get("file", [None])[0]),
                severity=(q.get("severity", [None])[0]),
                round_num=int(q["round"][0]) if "round" in q else None,
                unresolved="unresolved" in q,
            )
            self._json(200, [_comment_to_dict(c) for c in filtered])
            return

        self._error(404, f"no route for {tail}")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        m = ROUTE_RE.match(url.path)
        if not m:
            self._error(404, f"no route for {url.path}")
            return
        session_id, tail = m.group(1), (m.group(2) or "/")
        session_dir = self._resolve_session(session_id)
        if session_dir is None:
            self._error(404, f"unknown session: {session_id}")
            return

        data = self._read_json()
        if data is None:
            self._error(400, "invalid JSON body")
            return

        if tail == "/api/comments":
            self._post_comment(session_dir, data)
            return
        if tail == "/api/resolve":
            self._post_resolve(session_dir, data)
            return
        self._error(404, f"no route for {tail}")

    # -------- endpoints --------

    def _post_comment(self, session_dir: Path, data: dict) -> None:
        required = ("file", "line", "body")
        for k in required:
            if k not in data:
                return self._error(400, f"missing field: {k}")
        file = str(data["file"])
        try:
            line = int(data["line"])
        except (TypeError, ValueError):
            return self._error(400, "line must be an integer")
        body = str(data["body"])
        severity = str(data.get("severity") or "suggestion")
        if severity not in VALID_SEVERITIES:
            return self._error(400, f"invalid severity: {severity}")
        author = str(data.get("author") or _default_author(session_dir))

        session = load_session(session_dir)
        _, err = validate_comment_location(session.workspace, file, line)
        if err:
            return self._error(400, err)

        round_num = 2 if session.state == "round2" else 1
        comment = Comment(
            author=author,
            file=file,
            line=line,
            end_line=data.get("end_line"),
            body=body,
            severity=severity,
            round=round_num,
            head_sha=session.current_head,
        )
        store.append_comment(session_dir, comment)
        self._json(201, _comment_to_dict(comment))

    def _post_resolve(self, session_dir: Path, data: dict) -> None:
        cid = str(data.get("comment_id") or "")
        if not cid:
            return self._error(400, "missing comment_id")
        by = str(data.get("by") or _default_author(session_dir))
        if not store.resolve_comment(session_dir, cid, resolved_by=by):
            return self._error(404, f"comment not found: {cid}")
        self._json(200, {"resolved": cid})


def _comment_to_dict(c: Comment) -> dict:
    return {
        "id": c.id,
        "author": c.author,
        "timestamp": c.timestamp,
        "file": c.file,
        "line": c.line,
        "end_line": c.end_line,
        "body": c.body,
        "severity": c.severity,
        "round": c.round,
        "resolved": c.resolved,
        "resolved_by": c.resolved_by,
        "resolved_at": c.resolved_at,
        "stale": c.stale,
        "head_sha": c.head_sha,
    }


def _default_author(session_dir: Path) -> str:
    """Use git config user.name as the human author for UI-posted comments."""
    try:
        out = subprocess.run(
            ["git", "-C", str(load_session(session_dir).workspace),
             "config", "user.name"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "human"


def make_server(host: str, port: int, registry: SessionRegistry) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer that serves `registry`."""
    handler_cls = type("Handler", (_Handler,), {"registry": registry})
    return ThreadingHTTPServer((host, port), handler_cls)


def serve(session_dir: str | Path, host: str = "127.0.0.1", port: int = 0) -> None:
    """Blocking server for a single session. Port 0 → OS-assigned."""
    registry = SessionRegistry()
    session_id = registry.bind(session_dir)
    srv = make_server(host, port, registry)
    bound_port = srv.server_address[1]
    print(f"peanut-review web UI: http://{host}:{bound_port}/sessions/{session_id}/",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
