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
    assert f"/{s.id}" in html
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


def test_server_root_renders_index(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, body = _get(f"http://127.0.0.1:{port}/")
        assert code == 200
        text = body.decode("utf-8")
        assert "<!doctype html>" in text
        # Index page must link to the known session.
        assert f'href="/{session_id}"' in text
        assert "peanut-review" in text
    finally:
        srv.shutdown()


def test_server_session_page(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, body = _get(f"http://127.0.0.1:{port}/{session_id}/")
        assert code == 200
        assert b"<!doctype html>" in body
        assert b"foo.py" in body
    finally:
        srv.shutdown()


def test_server_session_api(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, raw = _get(f"http://127.0.0.1:{port}/{session_id}/api/session")
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
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
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
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
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
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
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
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "bug", "author": "jakub"},
        )
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/resolve",
            {"comment_id": c["id"], "by": "jakub"},
        )
        assert code == 200
        assert data["resolved"] == c["id"]

        comments = store.read_all_comments(session_dir)
        assert comments[0].resolved is True
    finally:
        srv.shutdown()


def test_header_home_link_has_no_trailing_slash_with_base_url(tmp_path: Path, repo: Path):
    """The h1 link back to the index is just `/<base>`, not `/<base>/`."""
    root = tmp_path / "review-root"
    root.mkdir()
    sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        session_dir=str(root / "sess-a"),
    )
    registry = web_app.SessionRegistry([root])
    srv = web_app.make_server("127.0.0.1", 0, registry, base_url="/pr")
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, body = _get(f"http://127.0.0.1:{port}/")
        assert code == 200
        text = body.decode("utf-8")
        # Canonical home link
        assert '<h1><a href="/pr">' in text
        # …and not the trailing-slash variant
        assert '<h1><a href="/pr/">' not in text
    finally:
        srv.shutdown()


def test_header_home_link_is_root_when_no_base_url(session_dir: Path):
    """Without a base_url, the h1 link falls back to `/`."""
    srv, _, port = _start_server(session_dir)
    try:
        _, body = _get(f"http://127.0.0.1:{port}/")
        assert '<h1><a href="/">' in body.decode("utf-8")
    finally:
        srv.shutdown()


def test_server_session_page_accepts_both_slash_and_no_slash(session_dir: Path):
    """Canonical session URL has no trailing slash, but /<id>/ still works."""
    srv, session_id, port = _start_server(session_dir)
    try:
        c1, _ = _get(f"http://127.0.0.1:{port}/{session_id}")
        c2, _ = _get(f"http://127.0.0.1:{port}/{session_id}/")
        assert c1 == 200
        assert c2 == 200
    finally:
        srv.shutdown()


def test_server_reserved_top_level_api_not_a_session(session_dir: Path):
    """`/api/...` must never be interpreted as a session id."""
    srv, _, port = _start_server(session_dir)
    try:
        # /api/sessions is a real route (list) — works.
        code, _ = _get(f"http://127.0.0.1:{port}/api/sessions")
        assert code == 200
        # /api/bogus is not a route; must 404 (not "unknown session: api").
        _get(f"http://127.0.0.1:{port}/api/bogus")
    except urllib.request.HTTPError as e:
        assert e.code == 404
        body = e.read().decode("utf-8")
        # Must not claim "api" is an unknown session.
        assert "unknown session" not in body
    finally:
        srv.shutdown()


def test_server_unknown_session(session_dir: Path):
    srv, _, port = _start_server(session_dir)
    try:
        code, data = _get(f"http://127.0.0.1:{port}/nope/api/session")
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
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "x", "author": "jakub"},
        )
        # Amend to create a new HEAD (with a tree change so the SHA actually shifts)
        (repo / "foo.py").write_text("def greet(name):\n    return f'hello, {name}!'\n")
        _git(repo, "commit", "-q", "--amend", "--no-edit", "-a")

        # Hit /api/session — should trigger migrate
        _, raw = _get(f"http://127.0.0.1:{port}/{session_id}/api/session")
        data = json.loads(raw)
        assert data["head_shifted"] is True

        # Comment should now be stale
        comments = store.read_all_comments(session_dir)
        assert comments[0].stale is True

        # Subsequent hit — HEAD already migrated, no shift this time
        _, raw2 = _get(f"http://127.0.0.1:{port}/{session_id}/api/session")
        data2 = json.loads(raw2)
        assert data2["head_shifted"] is False
    finally:
        srv.shutdown()


def test_serve_writes_pidfile_and_stop_removes_it(session_dir: Path, tmp_path: Path):
    """End-to-end: spawn serve() in a subprocess, verify pidfile, then stop."""
    import socket
    import sys
    import time as _t

    # Root = session's parent (which holds this single session).
    root = session_dir.parent

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    pidfile = web_app.pidfile_path(root)
    assert not pidfile.exists()

    proc = subprocess.Popen(
        [sys.executable, "-m", "peanut_review", "serve",
         "--root", str(root),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = _t.monotonic() + 5.0
        while _t.monotonic() < deadline and not pidfile.exists():
            _t.sleep(0.05)
        assert pidfile.exists(), "serve didn't write pidfile"
        payload = json.loads(pidfile.read_text())
        assert payload["pid"] == proc.pid
        assert payload["port"] == port
        assert payload["roots"] == [str(root)]

        returned = web_app.stop(root, timeout=5.0)
        assert returned["pid"] == proc.pid

        assert not pidfile.exists()
        assert proc.wait(timeout=2.0) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_stop_without_running_server_errors(tmp_path: Path):
    with pytest.raises(RuntimeError, match="no running server"):
        web_app.stop(tmp_path)


def test_stop_cleans_stale_pidfile(tmp_path: Path):
    pidfile = web_app.pidfile_path(tmp_path)
    pidfile.write_text(json.dumps({"pid": 999999999, "port": 1}) + "\n")
    with pytest.raises(RuntimeError, match="stale pidfile removed"):
        web_app.stop(tmp_path)
    assert not pidfile.exists()


def test_serve_refuses_second_instance(tmp_path: Path):
    pidfile = web_app.pidfile_path(tmp_path)
    pidfile.write_text(json.dumps({"pid": os.getpid(), "port": 1}) + "\n")
    try:
        with pytest.raises(RuntimeError, match="already running"):
            web_app.serve([tmp_path], port=0)
    finally:
        pidfile.unlink()


def test_serve_requires_a_root():
    with pytest.raises(ValueError, match="at least one root"):
        web_app.serve([], port=0)


def test_registry_discovers_sessions_under_root(tmp_path: Path, repo: Path):
    root = tmp_path / "review-root"
    root.mkdir()
    # Two sessions under the same root.
    s1, _ = sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        session_dir=str(root / "sess-a"),
    )
    s2, _ = sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        session_dir=str(root / "sess-b"),
    )
    # Plus a non-session directory that must be ignored.
    (root / "not-a-session").mkdir()

    reg = web_app.SessionRegistry([root])
    assert reg.get(s1.id) == root / "sess-a"
    assert reg.get(s2.id) == root / "sess-b"
    ids = {s["id"] for s in reg.list_sessions()}
    assert ids == {s1.id, s2.id}


def test_registry_picks_up_sessions_added_later(tmp_path: Path, repo: Path):
    root = tmp_path / "review-root"
    root.mkdir()
    reg = web_app.SessionRegistry([root])
    assert reg.list_sessions() == []

    # Add a session after the registry was created — get() triggers rescan.
    s, _ = sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        session_dir=str(root / "late"),
    )
    assert reg.get(s.id) == root / "late"


def test_index_and_api_sessions_list_all(tmp_path: Path, repo: Path):
    root = tmp_path / "review-root"
    root.mkdir()
    s1, _ = sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        session_dir=str(root / "sess-a"),
    )
    s2, _ = sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        session_dir=str(root / "sess-b"),
    )

    registry = web_app.SessionRegistry([root])
    srv = web_app.make_server("127.0.0.1", 0, registry)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, body = _get(f"http://127.0.0.1:{port}/")
        assert code == 200
        text = body.decode("utf-8")
        assert f'href="/{s1.id}"' in text
        assert f'href="/{s2.id}"' in text

        code, raw = _get(f"http://127.0.0.1:{port}/api/sessions")
        assert code == 200
        data = json.loads(raw)
        ids = {d["id"] for d in data}
        assert ids == {s1.id, s2.id}
        # Newest first
        assert data[0]["created_at"] >= data[1]["created_at"]
        # Each session still reachable at its own URL
        c1, _ = _get(f"http://127.0.0.1:{port}/{s1.id}/")
        c2, _ = _get(f"http://127.0.0.1:{port}/{s2.id}/")
        assert c1 == 200 and c2 == 200
    finally:
        srv.shutdown()


def test_index_empty_state(tmp_path: Path):
    root = tmp_path / "empty-root"
    root.mkdir()
    registry = web_app.SessionRegistry([root])
    srv = web_app.make_server("127.0.0.1", 0, registry)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, body = _get(f"http://127.0.0.1:{port}/")
        assert code == 200
        assert b"No review sessions found" in body
    finally:
        srv.shutdown()


def test_server_post_range_comment_persists_end_line(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "end_line": 2,
             "body": "range comment", "severity": "nit"},
        )
        assert code == 201
        assert data["line"] == 1
        assert data["end_line"] == 2

        comments = store.read_all_comments(session_dir)
        assert len(comments) == 1
        assert comments[0].line == 1
        assert comments[0].end_line == 2
    finally:
        srv.shutdown()


def test_render_range_comment_anchored_at_end_line(session_dir: Path, repo: Path):
    """A comment with end_line must appear in the thread anchored at end_line."""
    store.append_comment(session_dir, Comment(
        author="vera", file="foo.py", line=1, end_line=2,
        body="spans two lines", severity="warning", round=1,
    ))
    store.append_comment(session_dir, Comment(
        author="vera", file="foo.py", line=1,
        body="single line", severity="nit", round=1,
    ))
    s = sess.load_session(session_dir)
    from peanut_review.web import diff as diffmod
    files = diffmod.parse_diff(s.workspace, s.base_ref, s.topic_ref)
    html_out = render.render_page(s, "sid", files, store.read_all_comments(session_dir))

    # Range comment must carry the L1–L2 badge.
    assert "L1–L2" in html_out
    # The range comment's thread is keyed at end_line (2); the single-line
    # comment's thread is keyed at line (1). Both must be present as separate
    # threads.
    assert 'data-line="2"' in html_out  # range thread anchor
    assert 'data-line="1"' in html_out  # single-line thread anchor


def test_group_comments_anchors_range_at_end_line():
    from peanut_review.web.render import _group_comments
    comments = [
        Comment(author="a", file="foo.py", line=5, body="single"),
        Comment(author="b", file="foo.py", line=5, end_line=10, body="range"),
        Comment(author="c", file="foo.py", line=10, end_line=10, body="degenerate"),
    ]
    g = _group_comments(comments)
    # Single-line and range-ending-at-10 share an anchor at line 10.
    assert len(g[("foo.py", 10)]) == 2
    # Single at line 5 has its own anchor.
    assert len(g[("foo.py", 5)]) == 1


def test_normalize_base_url():
    n = web_app._normalize_base_url
    assert n("") == ""
    assert n(None) == ""
    assert n("/") == ""
    assert n("/pr") == "/pr"
    assert n("/pr/") == "/pr"
    assert n("pr") == "/pr"
    assert n("pr/") == "/pr"
    assert n("/pr/review/") == "/pr/review"


def test_index_emits_prefixed_hrefs_and_base_url_global(tmp_path: Path, repo: Path):
    """Index page links honour base_url and window.PR_BASE_URL is injected."""
    root = tmp_path / "review-root"
    root.mkdir()
    s, _ = sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        session_dir=str(root / "sess-a"),
    )
    registry = web_app.SessionRegistry([root])
    srv = web_app.make_server("127.0.0.1", 0, registry, base_url="/pr")
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, body = _get(f"http://127.0.0.1:{port}/")
        assert code == 200
        text = body.decode("utf-8")
        # Server-rendered link carries the prefix.
        assert f'href="/pr/{s.id}"' in text
        # Client-side JS can read the same prefix.
        assert 'window.PR_BASE_URL = "/pr"' in text
        # No bare-root session hrefs.
        assert f'href="/{s.id}"' not in text

        # Router still accepts the stripped path (caddy strips /pr before us).
        c, _ = _get(f"http://127.0.0.1:{port}/{s.id}/")
        assert c == 200
    finally:
        srv.shutdown()


def test_session_page_emits_prefixed_session_url(session_dir: Path):
    registry = web_app.SessionRegistry()
    session_id = registry.bind(session_dir)
    srv = web_app.make_server("127.0.0.1", 0, registry, base_url="/pr")
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, body = _get(f"http://127.0.0.1:{port}/{session_id}/")
        assert code == 200
        text = body.decode("utf-8")
        # app.js API calls are rooted at window.PR_SESSION_URL.
        assert f'window.PR_SESSION_URL = "/pr/{session_id}"' in text
        assert 'window.PR_BASE_URL = "/pr"' in text
    finally:
        srv.shutdown()


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
            f"http://127.0.0.1:{port}/{session_id}/api/comments?round=2",
        )
        assert code == 200
        data = json.loads(raw)
        assert len(data) == 1
        assert data[0]["body"] == "r2"
    finally:
        srv.shutdown()
