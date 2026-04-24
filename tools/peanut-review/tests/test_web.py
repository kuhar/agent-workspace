"""Tests for the web subpackage: diff parser, renderer, HTTP server."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

import pytest

from peanut_review import session as sess, store
from peanut_review.models import AgentConfig, Comment
from peanut_review.web import app as web_app
from peanut_review.web import diff as diffmod
from peanut_review.web import render


# ---------------- fixtures ----------------

def _git(cwd: str | Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=True,
    )
    return r.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Two-commit repo with a base and a topic commit touching one file."""
    wd = tmp_path / "repo"
    wd.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(wd)], check=True)
    subprocess.run(["git", "-C", str(wd), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wd), "config", "user.name", "t"], check=True)
    (wd / "foo.py").write_text("def greet(name):\n    return f'hi {name}'\n")
    _git(wd, "add", ".")
    _git(wd, "commit", "-q", "-m", "base")
    (wd / "foo.py").write_text("def greet(name):\n    return f'hello {name}'\n")
    _git(wd, "commit", "-q", "-am", "change greeting")
    return wd


@pytest.fixture
def session_dir(tmp_path: Path, repo: Path) -> Path:
    sd = tmp_path / "sess"
    sess.create_session(
        workspace=str(repo),
        base_ref="main~1",
        topic_ref="main",
        agents=[{"name": "felix", "model": "m", "persona": "felix.md"}],
        session_dir=str(sd),
    )
    return sd


# ---------------- diff parser ----------------

def test_parse_diff_added_modified(repo: Path):
    files = diffmod.parse_diff(str(repo), "main~1", "main")
    assert len(files) == 1
    fd = files[0]
    assert fd.path == "foo.py"
    assert fd.status == "M"
    # One added + one deleted (the `return` line changed) + one context line (def line)
    assert fd.additions == 1
    assert fd.deletions == 1
    # Should contain a context line for the def statement
    kinds = [l.kind for l in fd.lines]
    assert "context" in kinds
    assert "added" in kinds
    assert "deleted" in kinds


def test_parse_diff_empty_range(repo: Path):
    files = diffmod.parse_diff(str(repo), "main", "main")
    assert files == []


def test_parse_diff_new_file(repo: Path, tmp_path: Path):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "new.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "add new")
    files = diffmod.parse_diff(str(repo), base, "HEAD")
    new_files = [f for f in files if f.path == "new.py"]
    assert len(new_files) == 1
    assert new_files[0].status == "A"
    assert new_files[0].additions == 1


# ---------------- renderer ----------------

def test_render_page_smoke(session_dir: Path, repo: Path):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c = Comment(author="felix", file="foo.py", line=2, body="nice", severity="suggestion", round=1)
    store.append_comment(session_dir, c)
    comments = store.read_all_comments(session_dir)

    html = render.render_page(s, s.id, files, comments, head_shifted=False)
    assert "<!doctype html>" in html
    assert "foo.py" in html
    assert "suggestion" in html
    assert f"/sessions/{s.id}" in html
    assert "nice" in html  # comment body rendered
    assert "felix" in html  # author


def test_render_comment_escapes_html(session_dir: Path, repo: Path):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c = Comment(author="felix", file="foo.py", line=1,
                body="<script>alert(1)</script>", severity="critical", round=1)
    store.append_comment(session_dir, c)

    html = render.render_page(s, s.id, files, [c], head_shifted=False)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_stale_and_resolved_classes(session_dir: Path, repo: Path):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c1 = Comment(author="felix", file="foo.py", line=1, body="stale one",
                 severity="nit", round=1, stale=True)
    c2 = Comment(author="vera", file="foo.py", line=2, body="resolved one",
                 severity="nit", round=1, resolved=True)
    html = render.render_page(s, s.id, files, [c1, c2], head_shifted=False)
    assert "comment stale" in html
    assert "comment resolved" in html or "resolved" in html


# ---------------- HTTP server ----------------

def _start_server(session_dir: Path):
    registry = web_app.SessionRegistry()
    session_id = registry.bind(session_dir)
    srv = web_app.make_server("127.0.0.1", 0, registry)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, session_id, port


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url) as r:
        return r.status, r.read()


def _post(url: str, body: dict) -> tuple[int, dict]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.request.HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_root_redirects_to_session(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/")
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        with opener.open(req) as r:
            assert r.status == 200
            assert f"/sessions/{session_id}/" in r.geturl()
    finally:
        srv.shutdown()


def test_server_session_page(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, body = _get(f"http://127.0.0.1:{port}/sessions/{session_id}/")
        assert code == 200
        assert b"<!doctype html>" in body
        assert b"foo.py" in body
    finally:
        srv.shutdown()


def test_server_session_api(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, raw = _get(f"http://127.0.0.1:{port}/sessions/{session_id}/api/session")
        assert code == 200
        data = json.loads(raw)
        assert data["id"] == session_id
        assert data["state"] == "init"
        assert data["comment_count"] == 0
        assert "agents" in data
    finally:
        srv.shutdown()


def test_server_post_comment(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/sessions/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "looks good",
             "severity": "suggestion", "author": "jakub"},
        )
        assert code == 201
        assert data["body"] == "looks good"
        assert data["author"] == "jakub"

        # Read-back via store
        comments = store.read_all_comments(session_dir)
        assert len(comments) == 1
        assert comments[0].body == "looks good"
    finally:
        srv.shutdown()


def test_server_post_comment_validates_line(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/sessions/{session_id}/api/comments",
            {"file": "foo.py", "line": 999, "body": "x"},
        )
        assert code == 400
        assert "out of range" in data["error"]
    finally:
        srv.shutdown()


def test_server_post_comment_invalid_severity(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/sessions/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "x", "severity": "bogus"},
        )
        assert code == 400
        assert "severity" in data["error"]
    finally:
        srv.shutdown()


def test_server_resolve(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        _, c = _post(
            f"http://127.0.0.1:{port}/sessions/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "bug", "author": "jakub"},
        )
        code, data = _post(
            f"http://127.0.0.1:{port}/sessions/{session_id}/api/resolve",
            {"comment_id": c["id"], "by": "jakub"},
        )
        assert code == 200
        assert data["resolved"] == c["id"]

        comments = store.read_all_comments(session_dir)
        assert comments[0].resolved is True
    finally:
        srv.shutdown()


def test_server_unknown_session(session_dir: Path):
    srv, _, port = _start_server(session_dir)
    try:
        code, data = _get(f"http://127.0.0.1:{port}/sessions/nope/api/session")
        # urllib raises on 4xx, so we need HTTPError handling
    except urllib.request.HTTPError as e:
        assert e.code == 404
    else:
        assert False, "expected 404"
    finally:
        srv.shutdown()


def test_amend_auto_migrate(session_dir: Path, repo: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        # Seed a comment
        _post(
            f"http://127.0.0.1:{port}/sessions/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "x", "author": "jakub"},
        )
        # Amend to create a new HEAD (with a tree change so the SHA actually shifts)
        (repo / "foo.py").write_text("def greet(name):\n    return f'hello, {name}!'\n")
        _git(repo, "commit", "-q", "--amend", "--no-edit", "-a")

        # Hit /api/session — should trigger migrate
        _, raw = _get(f"http://127.0.0.1:{port}/sessions/{session_id}/api/session")
        data = json.loads(raw)
        assert data["head_shifted"] is True

        # Comment should now be stale
        comments = store.read_all_comments(session_dir)
        assert comments[0].stale is True

        # Subsequent hit — HEAD already migrated, no shift this time
        _, raw2 = _get(f"http://127.0.0.1:{port}/sessions/{session_id}/api/session")
        data2 = json.loads(raw2)
        assert data2["head_shifted"] is False
    finally:
        srv.shutdown()


def test_serve_writes_pidfile_and_stop_removes_it(session_dir: Path):
    """End-to-end: spawn serve() in a subprocess, verify pidfile, then stop."""
    import socket
    import sys
    import time as _t

    # Pick a free port so the subprocess can bind deterministically.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    pidfile = web_app.pidfile_path(session_dir)
    assert not pidfile.exists()

    proc = subprocess.Popen(
        [sys.executable, "-m", "peanut_review", "--session", str(session_dir),
         "serve", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # Wait for pidfile + reachable server.
        deadline = _t.monotonic() + 5.0
        while _t.monotonic() < deadline and not pidfile.exists():
            _t.sleep(0.05)
        assert pidfile.exists(), "serve didn't write pidfile"
        payload = json.loads(pidfile.read_text())
        assert payload["pid"] == proc.pid
        assert payload["port"] == port

        # Stop via the API
        returned = web_app.stop(session_dir, timeout=5.0)
        assert returned["pid"] == proc.pid

        # Pidfile gone, process dead
        assert not pidfile.exists()
        assert proc.wait(timeout=2.0) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stop_without_running_server_errors(session_dir: Path):
    with pytest.raises(RuntimeError, match="no running server"):
        web_app.stop(session_dir)


def test_stop_cleans_stale_pidfile(session_dir: Path):
    # Write a pidfile pointing at a PID that doesn't exist.
    pidfile = web_app.pidfile_path(session_dir)
    pidfile.write_text(json.dumps({"pid": 999999999, "port": 1}) + "\n")
    with pytest.raises(RuntimeError, match="stale pidfile removed"):
        web_app.stop(session_dir)
    assert not pidfile.exists()


def test_serve_refuses_second_instance(session_dir: Path):
    # Fake a live pidfile by pointing at our own PID.
    pidfile = web_app.pidfile_path(session_dir)
    pidfile.write_text(json.dumps({"pid": os.getpid(), "port": 1}) + "\n")
    try:
        with pytest.raises(RuntimeError, match="already running"):
            web_app.serve(session_dir, port=0)
    finally:
        pidfile.unlink()


def test_server_filter_comments_by_round(session_dir: Path):
    store.append_comment(session_dir, Comment(
        author="felix", file="foo.py", line=1, body="r1", severity="nit", round=1,
    ))
    store.append_comment(session_dir, Comment(
        author="felix", file="foo.py", line=2, body="r2", severity="nit", round=2,
    ))
    srv, session_id, port = _start_server(session_dir)
    try:
        code, raw = _get(
            f"http://127.0.0.1:{port}/sessions/{session_id}/api/comments?round=2",
        )
        assert code == 200
        data = json.loads(raw)
        assert len(data) == 1
        assert data[0]["body"] == "r2"
    finally:
        srv.shutdown()
