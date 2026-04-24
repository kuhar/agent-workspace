"""HTTP server for peanut-review — human review UI.

One server on one port can serve every session it discovers under one or more
review roots. Routes are session-indexed: each session is reachable at
`/sessions/<id>/...` and the root `/` renders a picker of recent sessions.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import store
from ..models import Comment, Severity
from ..session import (
    load_session,
    refresh_agent_statuses,
    save_session,
    validate_comment_location,
)
from . import diff as diffmod
from .render import render_index, render_page


DEFAULT_ROOT = Path("/tmp/peanut-review")


class SessionRegistry:
    """Map session-id → session-dir. Backed by filesystem scans of review roots.

    Sessions can also be bound explicitly (used by tests and by `serve` when the
    caller wants to attach a session that isn't under any configured root).
    """

    def __init__(self, roots: Iterable[str | Path] = ()) -> None:
        self._roots: list[Path] = [Path(r) for r in roots]
        self._by_id: dict[str, Path] = {}
        if self._roots:
            self.rescan()

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    def bind(self, session_dir: str | Path) -> str:
        """Attach a session by directory and return its id."""
        sdir = Path(session_dir)
        s = load_session(sdir)
        self._by_id[s.id] = sdir
        return s.id

    def rescan(self) -> None:
        """Re-discover sessions under each root, preserving explicitly-bound orphans."""
        root_resolved = [r.resolve() for r in self._roots if r.is_dir()]

        def _is_under_root(sd: Path) -> bool:
            try:
                sd_res = sd.resolve()
            except OSError:
                return False
            for r in root_resolved:
                try:
                    sd_res.relative_to(r)
                    return True
                except ValueError:
                    continue
            return False

        # Keep previously-bound sessions that live outside any root.
        new_by_id: dict[str, Path] = {
            sid: sd for sid, sd in self._by_id.items() if not _is_under_root(sd)
        }
        for root in self._roots:
            if not root.is_dir():
                continue
            for sub in sorted(root.iterdir()):
                if not sub.is_dir():
                    continue
                if not (sub / "session.json").is_file():
                    continue
                try:
                    s = load_session(sub)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                new_by_id[s.id] = sub
        self._by_id = new_by_id

    def get(self, session_id: str) -> Path | None:
        if session_id in self._by_id:
            return self._by_id[session_id]
        if self._roots:
            self.rescan()
        return self._by_id.get(session_id)

    def only(self) -> str | None:
        return next(iter(self._by_id)) if len(self._by_id) == 1 else None

    def list_sessions(self) -> list[dict]:
        """Summaries for every known session, newest first."""
        if self._roots:
            self.rescan()
        summaries: list[dict] = []
        for sid, sdir in self._by_id.items():
            try:
                s = load_session(sdir)
                comments = store.read_all_comments(sdir)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            live = [c for c in comments if not c.deleted]
            summaries.append({
                "id": sid,
                "session_dir": str(sdir),
                "state": s.state,
                "base_ref": s.base_ref,
                "topic_ref": s.topic_ref,
                "created_at": s.created_at,
                "workspace": s.workspace,
                "current_head": (s.current_head or "")[:12],
                "comment_count": len(live),
                "unresolved_count": sum(1 for c in live if not c.resolved),
                "stale_count": sum(1 for c in live if c.stale),
                "critical_count": sum(1 for c in live if c.severity == "critical"),
                "deleted_count": len(comments) - len(live),
                "agent_count": len(s.agents),
            })
        summaries.sort(key=lambda d: d["created_at"], reverse=True)
        return summaries


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


ROUTE_RE = re.compile(r"^/([^/]+)(/.*)?$")
# Top-level path segments that are NOT session ids — reserved for future and
# current global routes. Guards against a session-id slug called "api".
RESERVED_ROOTS = {"api"}
VALID_SEVERITIES = {s.value for s in Severity}


class _Handler(BaseHTTPRequestHandler):
    # Injected at construction — see make_server.
    registry: SessionRegistry
    # Path prefix the app is mounted under (e.g. "/pr"). Empty = root-mounted.
    # Caddy `handle_path /pr/*` strips the prefix before forwarding, so the
    # router never sees it — this string is only used when emitting URLs.
    base_url: str = ""

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

    def log_message(self, fmt, *args):  # noqa: A003
        import sys
        sys.stderr.write("[pr-web] " + fmt % args + "\n")

    # -------- routing --------

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("", "/"):
            sessions = self.registry.list_sessions()
            self._html(200, render_index(
                sessions, roots=[str(r) for r in self.registry.roots],
                base_url=self.base_url,
            ))
            return
        if url.path == "/api/sessions":
            self._json(200, self.registry.list_sessions())
            return

        m = ROUTE_RE.match(url.path)
        if not m or m.group(1) in RESERVED_ROOTS:
            self._error(404, f"no route for {url.path}")
            return
        session_id, tail = m.group(1), (m.group(2) or "/")
        session_dir = self._resolve_session(session_id)
        if session_dir is None:
            self._error(404, f"unknown session: {session_id}")
            return

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
                head_shifted=shifted, base_url=self.base_url,
            )
            self._html(200, html_out)
            return

        if tail == "/api/session":
            session = load_session(session_dir)
            comments = store.read_all_comments(session_dir)
            live = [c for c in comments if not c.deleted]
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
                "comment_count": len(live),
                "stale_count": sum(1 for c in live if c.stale),
                "critical_count": sum(1 for c in live if c.severity == "critical"),
                "deleted_count": len(comments) - len(live),
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
                include_deleted="include_deleted" in q,
            )
            self._json(200, [_comment_to_dict(c) for c in filtered])
            return

        self._error(404, f"no route for {tail}")

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        m = ROUTE_RE.match(url.path)
        if not m or m.group(1) in RESERVED_ROOTS:
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
        if tail == "/api/delete":
            self._post_delete(session_dir, data)
            return
        if tail == "/api/undelete":
            self._post_undelete(session_dir, data)
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
        author = str(data.get("author") or _default_author())

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
        by = str(data.get("by") or _default_author())
        if not store.resolve_comment(session_dir, cid, resolved_by=by):
            return self._error(404, f"comment not found: {cid}")
        self._json(200, {"resolved": cid})

    def _post_delete(self, session_dir: Path, data: dict) -> None:
        cid = str(data.get("comment_id") or "")
        if not cid:
            return self._error(400, "missing comment_id")
        by = str(data.get("by") or _default_author())
        if not store.delete_comment(session_dir, cid, deleted_by=by):
            return self._error(404, f"comment not found: {cid}")
        self._json(200, {"deleted": cid, "by": by})

    def _post_undelete(self, session_dir: Path, data: dict) -> None:
        cid = str(data.get("comment_id") or "")
        if not cid:
            return self._error(400, "missing comment_id")
        if not store.undelete_comment(session_dir, cid):
            return self._error(404, f"comment not found: {cid}")
        self._json(200, {"undeleted": cid})


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
        "deleted": c.deleted,
        "deleted_by": c.deleted_by,
        "deleted_at": c.deleted_at,
        "head_sha": c.head_sha,
    }


def _default_author() -> str:
    """Default author for UI-posted comments — the human running the browser.

    Uses the global git identity so we never inherit whatever reviewer agent
    (felix, vera, …) happens to be set as `user.name` in the session's
    workspace/submodule git dir.
    """
    try:
        out = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "human"


def _normalize_base_url(base_url: str) -> str:
    """Normalize to empty or `/foo[/bar]` (leading slash, no trailing slash)."""
    b = (base_url or "").rstrip("/")
    if not b:
        return ""
    if not b.startswith("/"):
        b = "/" + b
    return b


def make_server(
    host: str, port: int, registry: SessionRegistry, *, base_url: str = "",
) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer that serves `registry`.

    `base_url` is the path prefix the app is served under (e.g. `/pr`). The
    router never sees it — assume an upstream like caddy's `handle_path` has
    already stripped it. The prefix only shapes URLs emitted in HTML/JS.
    """
    handler_cls = type("Handler", (_Handler,), {
        "registry": registry,
        "base_url": _normalize_base_url(base_url),
    })
    return ThreadingHTTPServer((host, port), handler_cls)


def pidfile_path(root: str | Path) -> Path:
    """Server pidfile lives at `<root>/web.pid` — one server per root."""
    return Path(root) / "web.pid"


def _read_pidfile(path: Path) -> tuple[int | None, dict]:
    """Parse a pidfile. Returns (pid_if_alive, raw_payload)."""
    if not path.exists():
        return None, {}
    try:
        payload = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return None, {}
    pid = int(payload.get("pid", 0))
    if pid <= 0:
        return None, payload
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None, payload
    except PermissionError:
        # Exists but owned elsewhere — report alive; we likely can't signal it.
        return pid, payload
    return pid, payload


def serve(
    roots: Iterable[str | Path],
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    extra_sessions: Iterable[str | Path] = (),
    base_url: str = "",
) -> None:
    """Blocking multi-session server. Port 0 → OS-assigned.

    Pidfile is written to `<roots[0]>/web.pid` so `peanut-review stop` can find
    us. Refuses to start if a live server already holds that pidfile.
    """
    root_list = [Path(r) for r in roots]
    if not root_list:
        raise ValueError("serve() requires at least one root")

    primary = root_list[0]
    primary.mkdir(parents=True, exist_ok=True)
    pidfile = pidfile_path(primary)
    existing_pid, _ = _read_pidfile(pidfile)
    if existing_pid is not None:
        raise RuntimeError(
            f"server already running (pid {existing_pid}) for root {primary}. "
            f"Stop it first with `peanut-review stop` or remove {pidfile}."
        )

    registry = SessionRegistry(root_list)
    for sd in extra_sessions:
        registry.bind(sd)

    srv = make_server(host, port, registry, base_url=base_url)
    bound_port = srv.server_address[1]
    normalized = _normalize_base_url(base_url)
    url = f"http://{host}:{bound_port}{normalized}/"

    pidfile.write_text(json.dumps({
        "pid": os.getpid(),
        "host": host,
        "port": bound_port,
        "url": url,
        "base_url": normalized,
        "roots": [str(r) for r in root_list],
    }) + "\n")

    session_count = len(registry.list_sessions())
    print(
        f"peanut-review web UI: {url} "
        f"({session_count} session{'s' if session_count != 1 else ''})",
        flush=True,
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            pidfile.unlink()
        except FileNotFoundError:
            pass


def stop(root: str | Path, timeout: float = 5.0) -> dict:
    """Signal the running serve() at `<root>/web.pid` to shut down.

    Returns the last-known pidfile metadata. Raises RuntimeError if no running
    server is found (stale pidfile gets cleaned up first).
    """
    primary = Path(root)
    pidfile = pidfile_path(primary)
    pid, payload = _read_pidfile(pidfile)
    if pid is None:
        if pidfile.exists():
            pidfile.unlink()
            raise RuntimeError(f"no running server (stale pidfile removed at {pidfile})")
        raise RuntimeError(f"no running server (no pidfile at {pidfile})")

    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError as e:
        raise RuntimeError(f"cannot signal pid {pid}: {e}") from e

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    if pidfile.exists():
        pidfile.unlink()
    return payload
