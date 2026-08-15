"""Capability-scoped reviewer gateway for SSH-launched agents.

This listener is intentionally separate from the human web application.  It
offers only the small reviewer command surface and binds every credential to
one session, one agent, one launch, an expiry, and an operation set.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import models, polling, runtime, session as sess, store

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 256 * 1024
DEFAULT_ALLOWED_OPS = frozenset({
    "hello",
    "status:read",
    "comments:read",
    "notes:read",
    "comments:write",
    "notes:write",
    "round-done:write",
})


class GatewayError(RuntimeError):
    """A gateway request or authorization failure."""


def _capability_dir(session_dir: str | Path) -> Path:
    return Path(session_dir) / "runtime" / "gateway" / "capabilities"


def _capability_path(session_dir: str | Path, launch_id: str) -> Path:
    return _capability_dir(session_dir) / f"{launch_id}.json"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_capability(
    session_dir: str | Path,
    *,
    agent: str,
    launch_id: str,
    ttl_seconds: int,
    allowed_ops: set[str] | frozenset[str] = DEFAULT_ALLOWED_OPS,
) -> str:
    """Create a short-lived capability and return its bearer token."""
    if ttl_seconds <= 0:
        raise ValueError("capability TTL must be positive")
    session = sess.load_session(session_dir)
    configured = {item.name for item in session.agents}
    if agent not in configured:
        raise ValueError(f"unknown session agent: {agent}")
    if not launch_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in launch_id):
        raise ValueError("launch id must contain only letters, digits, '_' or '-'")
    unknown = set(allowed_ops) - set(DEFAULT_ALLOWED_OPS)
    if unknown:
        raise ValueError(f"unsupported capability operations: {', '.join(sorted(unknown))}")

    token = f"{launch_id}.{secrets.token_urlsafe(32)}"
    record = {
        "version": PROTOCOL_VERSION,
        "session_id": session.id,
        "agent": agent,
        "launch_id": launch_id,
        "token_hash": _token_hash(token),
        "allowed_ops": sorted(allowed_ops),
        "issued_at": time.time(),
        "expires_at": time.time() + ttl_seconds,
        "revoked": False,
    }
    directory = _capability_dir(session_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    path = _capability_path(session_dir, launch_id)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return token


def revoke_capability(session_dir: str | Path, launch_id: str) -> bool:
    import fcntl

    path = _capability_path(session_dir, launch_id)
    if not path.exists():
        return False
    lock_path = path.with_suffix(".lock")
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            return False
        record["revoked"] = True
        record["revoked_at"] = time.time()
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
        return True
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _load_capability(session_dir: Path, token: str, operation: str) -> dict[str, Any]:
    launch_id, separator, _secret = token.partition(".")
    if not separator:
        raise GatewayError("invalid capability")
    try:
        record = json.loads(_capability_path(session_dir, launch_id).read_text())
    except (FileNotFoundError, ValueError) as exc:
        raise GatewayError("invalid capability") from exc
    if not hmac.compare_digest(str(record.get("token_hash", "")), _token_hash(token)):
        raise GatewayError("invalid capability")
    if record.get("revoked"):
        raise GatewayError("capability has been revoked")
    if float(record.get("expires_at", 0)) <= time.time():
        raise GatewayError("capability has expired")
    session = sess.load_session(session_dir)
    if record.get("session_id") != session.id:
        raise GatewayError("capability session mismatch")
    if operation not in record.get("allowed_ops", []):
        raise GatewayError("operation is not allowed by this capability")
    return record


def _find_session(roots: list[Path], session_id: str) -> Path:
    if not session_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in session_id):
        raise GatewayError("invalid session id")
    matches: list[Path] = []
    for root in roots:
        candidate = root / session_id
        if (candidate / "session.json").is_file():
            matches.append(candidate)
        elif root.name == session_id and (root / "session.json").is_file():
            matches.append(root)
    unique = {path.resolve() for path in matches}
    if not unique:
        raise GatewayError("session not found")
    if len(unique) != 1:
        raise GatewayError("session id is ambiguous across gateway roots")
    return unique.pop()


def _query_flag(query: dict[str, list[str]], name: str) -> bool:
    return query.get(name, [""])[0].lower() in {"1", "true", "yes"}


def _comment_result(session_dir: Path, author: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = payload.get("body")
    if not isinstance(body, str):
        raise GatewayError("comment body must be a string")
    severity = payload.get("severity", models.Severity.SUGGESTION.value)
    if severity not in {item.value for item in models.Severity}:
        raise GatewayError(f"invalid severity: {severity!r}")
    try:
        category = models.normalize_comment_category(payload.get("category"))
    except ValueError as exc:
        raise GatewayError(str(exc)) from exc

    session = sess.load_session(session_dir)
    reply_arg = payload.get("reply_to")
    reply_to: str | None = None
    file_lines: list[str] | None = None
    if reply_arg:
        comments = store.read_all_comments(session_dir)
        reply_to = store.normalize_reply_to(comments, str(reply_arg))
        if reply_to is None:
            raise GatewayError(f"reply comment not found: {reply_arg}")
        if models.category_is_review_decision(category):
            raise GatewayError("review decisions cannot be used on replies")
        parent = next(comment for comment in comments if comment.id == reply_to)
        if parent.file == sess.GLOBAL_FILE:
            raise GatewayError("replies to global comments are not supported")
        file = parent.file
        line = parent.line
        end_line = None
        is_global = False
    else:
        file_value = payload.get("file")
        line_value = payload.get("line")
        is_global = bool(payload.get("global")) or (file_value is None and line_value is None)
        if is_global:
            if file_value is not None or line_value is not None or payload.get("end_line") is not None:
                raise GatewayError("global comments cannot include a source anchor")
            file = sess.GLOBAL_FILE
            line = 0
            end_line = None
        else:
            if not isinstance(file_value, str) or not file_value or not isinstance(line_value, int):
                raise GatewayError("anchored comments require file and integer line")
            if models.category_is_review_decision(category):
                raise GatewayError("review decisions are only valid on global comments")
            file = file_value
            line = line_value
            end_line = payload.get("end_line")
            file_lines, error = sess.validate_comment_location(
                sess.repo_path(session), file, line,
                head_ref=session.current_head,
                require_pinned=True,
            )
            if error:
                raise GatewayError(error)

    comment = models.Comment(
        author=author,
        file=file,
        line=line,
        end_line=end_line,
        body=body,
        severity=severity,
        category=category,
        head_sha=session.current_head,
        reply_to=reply_to,
    )
    try:
        store.append_comment(session_dir, comment)
    except ValueError as exc:
        raise GatewayError(str(exc)) from exc
    if reply_to:
        message = f"{comment.id} (reply to {reply_to})"
    elif is_global:
        message = f"{comment.id} (global)"
    elif file_lines and line >= 1:
        message = f"{file}:{line}: {file_lines[line - 1]}"
    else:
        message = comment.id
    return {"message": message, "comment": json.loads(comment.to_json())}


def _status_result(session_dir: Path) -> dict[str, Any]:
    session = sess.load_session(session_dir)
    sess.refresh_agent_statuses(session_dir, session)
    agents = []
    for agent in session.agents:
        snapshot = runtime.inspect_agent_runtime(session_dir, agent)
        agents.append({
            "name": agent.name,
            "status": runtime.derive_status_from_snapshot(agent, snapshot),
            "model": runtime.compact_model(agent.model),
            "ssh_target": agent.ssh_target,
            "process_state": snapshot["process_state"],
            "protocol_status": snapshot["protocol_status"],
            "details": runtime.status_detail_parts(snapshot, runtime.derive_status_from_snapshot(agent, snapshot)),
        })
    comments = store.read_all_comments(session_dir)
    live = [comment for comment in comments if not comment.deleted]
    signals_dir = session_dir / "signals"
    return {
        "session": {
            "id": session.id,
            "base": session.base_ref,
            "head": session.current_head,
            "workspace": session.workspace,
            "repo": sess.repo_path(session),
        },
        "agents": agents,
        "comments": {
            "total": len(live),
            "deleted": len(comments) - len(live),
            "critical": sum(comment.severity == "critical" for comment in live),
            "resolved": sum(comment.resolved for comment in live),
            "stale": sum(comment.stale for comment in live),
        },
        "notes": len(store.read_all_notes(session_dir)),
        "signals": sorted(path.name for path in signals_dir.iterdir() if path.is_file()) if signals_dir.is_dir() else [],
    }


class GatewayClient:
    """Small standard-library client used by the remote CLI."""

    def __init__(self, base_url: str, token: str, session_id: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session_id = session_id
        self.timeout = timeout

    def request(self, method: str, endpoint: str, *, query: dict[str, Any] | None = None, payload: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/v1/sessions/{quote(self.session_id, safe='')}/{endpoint}"
        if query:
            url += "?" + urlencode({key: value for key, value in query.items() if value is not None})
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(url, data=body, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", str(exc))
            except (ValueError, OSError):
                detail = str(exc)
            raise GatewayError(detail) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GatewayError(f"gateway unavailable: {exc}") from exc


class _GatewayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, roots: list[Path]):
        self.roots = roots
        super().__init__(address, _GatewayHandler)


class _GatewayHandler(BaseHTTPRequestHandler):
    server: _GatewayServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Do not put authorization headers or request bodies in logs.
        super().log_message(format, *args)

    def _reply(self, status: HTTPStatus, payload: dict[str, Any] | list[Any]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._reply(status, {"error": message})

    def _body(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise GatewayError("invalid content length") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise GatewayError(f"request body exceeds {MAX_REQUEST_BYTES} bytes")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            raise GatewayError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise GatewayError("request body must be a JSON object")
        return payload

    def _route(self) -> tuple[Path, dict[str, Any], str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 4 or parts[:2] != ["v1", "sessions"]:
            raise GatewayError("unknown gateway route")
        session_id, endpoint = parts[2], parts[3]
        operation = {
            ("GET", "hello"): "hello",
            ("GET", "status"): "status:read",
            ("GET", "comments"): "comments:read",
            ("GET", "notes"): "notes:read",
            ("POST", "comments"): "comments:write",
            ("POST", "notes"): "notes:write",
            ("POST", "signals"): "round-done:write",
        }.get((self.command, endpoint))
        if operation is None:
            raise GatewayError("operation is not supported by the reviewer gateway")
        session_dir = _find_session(self.server.roots, session_id)
        authorization = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not authorization.startswith(prefix):
            raise GatewayError("bearer capability required")
        capability = _load_capability(session_dir, authorization[len(prefix):], operation)
        return session_dir, capability, endpoint, parse_qs(parsed.query)

    def _handle(self) -> None:
        try:
            session_dir, capability, endpoint, query = self._route()
            author = str(capability["agent"])
            if self.command == "GET" and endpoint == "hello":
                payload: Any = {
                    "protocol": PROTOCOL_VERSION,
                    "session_id": capability["session_id"],
                    "agent": author,
                    "launch_id": capability["launch_id"],
                    "allowed_ops": capability["allowed_ops"],
                }
            elif self.command == "GET" and endpoint == "status":
                payload = _status_result(session_dir)
            elif self.command == "GET" and endpoint == "comments":
                if _query_flag(query, "include_deleted"):
                    raise GatewayError(
                        "deleted comments are not available to reviewer capabilities"
                    )
                comments = store.filter_comments(
                    store.read_all_comments(session_dir),
                    agent=query.get("agent", [None])[0],
                    file=query.get("file", [None])[0],
                    severity=query.get("severity", [None])[0],
                    category=query.get("category", [None])[0],
                    since=query.get("since", [None])[0],
                    unresolved=_query_flag(query, "unresolved"),
                    include_deleted=False,
                )
                payload = [json.loads(comment.to_json()) for comment in comments]
            elif self.command == "GET" and endpoint == "notes":
                notes = store.filter_notes(
                    store.read_all_notes(session_dir),
                    agent=query.get("agent", [None])[0],
                    since=query.get("since", [None])[0],
                )
                payload = [json.loads(note.to_json()) for note in notes]
            elif self.command == "POST" and endpoint == "comments":
                payload = _comment_result(session_dir, author, self._body())
            elif self.command == "POST" and endpoint == "notes":
                body = self._body().get("body")
                if not isinstance(body, str):
                    raise GatewayError("note body must be a string")
                note = store.append_note(session_dir, models.Note(author=author, body=body))
                payload = {"message": note.id, "note": json.loads(note.to_json())}
            elif self.command == "POST" and endpoint == "signals":
                event = self._body().get("event")
                if event != runtime.ROUND_DONE_EVENT:
                    raise GatewayError("reviewer capabilities may only signal round-done")
                polling.write_signal(session_dir, author, event)
                payload = {"message": f"Signaled {author}.{event}"}
            else:
                raise GatewayError("operation is not supported")
            self._reply(HTTPStatus.OK, payload)
        except GatewayError as exc:
            message = str(exc)
            status = HTTPStatus.UNAUTHORIZED if any(word in message for word in ("capability", "bearer", "allowed")) else HTTPStatus.BAD_REQUEST
            self._error(status, message)
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "gateway request failed")

    do_GET = _handle
    do_POST = _handle


def serve(roots: list[str | Path], host: str = "127.0.0.1", port: int = 0) -> None:
    """Run a foreground reviewer gateway."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("reviewer gateway must bind to loopback")
    resolved_roots = [Path(root).resolve() for root in roots]
    for root in resolved_roots:
        root.mkdir(parents=True, exist_ok=True)
    server = _GatewayServer((host, port), resolved_roots)
    print(f"Reviewer gateway listening on http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()


def start_test_server(roots: list[str | Path]) -> tuple[_GatewayServer, threading.Thread]:
    """Start an ephemeral loopback server for integration tests."""
    server = _GatewayServer(("127.0.0.1", 0), [Path(root).resolve() for root in roots])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
