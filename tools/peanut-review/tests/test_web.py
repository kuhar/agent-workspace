"""Tests for the web subpackage: diff parser, renderer, HTTP server."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from peanut_review import session as sess, store
from peanut_review.models import AgentConfig, Comment, GitHubPR, Note
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


def _long_repo(tmp_path: Path, *, line_count: int, changed_line: int) -> Path:
    wd = tmp_path / "long-repo"
    wd.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(wd)], check=True)
    subprocess.run(["git", "-C", str(wd), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wd), "config", "user.name", "t"], check=True)
    base_lines = [
        f"value_{i:03d} = {i}\n"
        for i in range(1, line_count + 1)
    ]
    (wd / "long.py").write_text("".join(base_lines))
    _git(wd, "add", ".")
    _git(wd, "commit", "-q", "-m", "base")
    changed_lines = list(base_lines)
    changed_lines[changed_line - 1] = f"value_{changed_line:03d} = 'changed'\n"
    (wd / "long.py").write_text("".join(changed_lines))
    _git(wd, "commit", "-q", "-am", "change long file")
    return wd


def _session_for_repo(tmp_path: Path, repo: Path) -> Path:
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
    c = Comment(author="felix", file="foo.py", line=2, body="nice", severity="suggestion")
    store.append_comment(session_dir, c)
    comments = store.read_all_comments(session_dir)

    html = render.render_page(s, s.id, files, comments, head_shifted=False)
    assert "<!doctype html>" in html
    assert "foo.py" in html
    assert "suggestion" in html
    assert f"/{s.id}" in html
    assert "nice" in html  # comment body rendered
    assert "felix" in html  # author
    assert 'id="theme-toggle"' in html
    assert 'localStorage.getItem("pr.theme")' in html


def test_render_page_folds_long_unchanged_context(tmp_path: Path):
    repo = _long_repo(tmp_path, line_count=120, changed_line=60)
    session_dir = _session_for_repo(tmp_path, repo)
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)

    html = render.render_page(s, s.id, files, [], head_shifted=False)

    assert 'class="line fold-gap"' in html
    assert 'data-folded-lines="27"' in html
    assert 'class="fold-toggle"' in html
    assert 'data-fold-expand=' in html
    assert 'class="fold-payload"' in html
    assert "MAX_FOLD_EXPAND_LINES = 100" in html
    assert "27 unchanged lines hidden" in html
    assert "28 unchanged lines hidden" in html
    assert html.count('class="line context"') < 80
    assert "value_028" in html
    assert "value_092" in html


def test_render_page_keeps_comment_anchor_visible_when_context_folded(
    tmp_path: Path,
):
    repo = _long_repo(tmp_path, line_count=160, changed_line=120)
    session_dir = _session_for_repo(tmp_path, repo)
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c = Comment(author="vera", file="long.py", line=5, body="look here",
                severity="warning")

    html = render.render_page(s, s.id, files, [c], head_shifted=False)

    assert "look here" in html
    assert "value_005" in html
    assert 'data-line="5"' in html


def test_render_page_keeps_file_header_sticky(session_dir: Path, repo: Path):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    html = render.render_page(s, s.id, files, [], head_shifted=False)

    assert ".file-header" in html
    assert "position: sticky;" in html
    assert "top: var(--sticky-file-top);" in html
    assert "border-radius: 0;" in html
    assert "--sticky-target-offset" in html
    assert '<span class="path" title="foo.py">foo.py</span>' in html


def test_render_index_includes_theme_toggle():
    html = render.render_index([], roots=["/tmp/reviews"])

    assert 'id="theme-toggle"' in html
    assert 'localStorage.getItem("pr.theme")' in html


def test_render_page_labels_round_state_as_in_review(
    session_dir: Path, repo: Path
):
    sess.transition_state(session_dir, "round")
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    html = render.render_page(s, s.id, files, [], head_shifted=False)
    header = html[html.index("<header>"):html.index("</header>")]

    assert 'class="badge session-state state-round"' in header
    assert 'data-session-state="round"' in header
    assert ">in review</span>" in header
    assert ">round</span>" not in header


def test_render_page_uses_github_title_for_change_label(
    session_dir: Path, repo: Path
):
    s = sess.load_session(session_dir)
    s.base_ref = "base-sha"
    s.topic_ref = "topic-sha"
    s.current_head = "topic-sha"
    s.github = GitHubPR(
        repo="acme/foo",
        number=42,
        url="https://github.com/acme/foo/pull/42",
        head_sha="topic-sha",
        base_sha="base-sha",
        title="Add a feature",
    )
    files = diffmod.parse_diff(str(repo), "main~1", "main")

    html = render.render_page(s, s.id, files, [], head_shifted=False)
    header = html[html.index("<header>"):html.index("</header>")]
    sidebar = html[html.index("<aside"):html.index("</aside>")]

    assert "Add a feature" in header
    assert "base-sha" not in header
    assert "topic-sha" not in header
    assert '<li data-k="change"><span>change</span>' not in sidebar
    assert "Add a feature" not in sidebar
    assert '<li data-k="head"><span>head</span><span class="v mono">topic-sha</span></li>' in sidebar
    assert '<li data-k="base"><span>base</span><span class="v mono">base-sha</span></li>' in sidebar
    assert '<li data-k="pr"><span>pr</span><span class="v mono">acme/foo#42</span></li>' in sidebar


def test_render_comment_escapes_html(session_dir: Path, repo: Path):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c = Comment(author="felix", file="foo.py", line=1,
                body="<script>alert(1)</script>", severity="critical")
    store.append_comment(session_dir, c)

    html = render.render_page(s, s.id, files, [c], head_shifted=False)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_comment_includes_relative_timestamp(session_dir: Path, repo: Path):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c = Comment(
        author="felix",
        timestamp="2020-01-01T00:00:00+00:00",
        file="foo.py",
        line=1,
        body="old note",
        severity="nit",
    )

    html = render.render_page(s, s.id, files, [c], head_shifted=False)

    assert (
        '<time class="comment-time" datetime="2020-01-01T00:00:00+00:00" '
        'title="2020-01-01T00:00:00+00:00">'
    ) in html
    assert "ago</time>" in html


def test_relative_time_label_uses_github_style_units():
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

    assert render._relative_time_label(
        "2026-05-04T11:57:00+00:00", now=now,
    ) == "3 minutes ago"
    assert render._relative_time_label(
        "2026-05-04T10:20:00+00:00", now=now,
    ) == "1 hour ago"
    assert render._relative_time_label(
        "2026-05-03T10:00:00+00:00", now=now,
    ) == "yesterday"


def test_render_sidebar_files_list_with_counts(session_dir: Path, repo: Path):
    """Sidebar lists each changed file with unresolved/total counts and an anchor."""
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    # Two comments on foo.py: one open + one resolved → 1 open / 2 total.
    c_open = Comment(author="felix", file="foo.py", line=1, body="a", severity="nit")
    c_done = Comment(author="vera", file="foo.py", line=2, body="b", severity="nit",
                     resolved=True)
    store.append_comment(session_dir, c_open)
    store.append_comment(session_dir, c_done)
    html = render.render_page(s, s.id, files, store.read_all_comments(session_dir),
                              head_shifted=False)

    assert "<h3>Files " in html, "sidebar should have a Files heading"
    assert 'class="files"' in html
    # Anchor id on file section + matching href in sidebar.
    assert 'id="f-foo-py"' in html
    assert 'href="#f-foo-py"' in html
    # The file's per-file count cell should carry open and muted/total spans.
    assert '<span class="count open">1</span>' in html
    assert '<span class="count muted">/2</span>' in html


def test_render_sidebar_files_dash_when_no_comments(session_dir: Path, repo: Path):
    """Files without any live comments show an em-dash placeholder, not a zero."""
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    html = render.render_page(s, s.id, files, [], head_shifted=False)
    assert '<span class="count empty">—</span>' in html


def test_render_sidebar_agents_show_model_and_hides_kill_controls_when_idle(
    session_dir: Path,
    repo: Path,
):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    html = render.render_page(s, s.id, files, [], head_shifted=False)

    assert '<span class="agent-name">felix</span>' in html
    assert '<span class="agent-model mono" title="m">m</span>' in html
    assert 'class="agent-main"' in html
    assert '<span class="agent-state-label">status</span> <span class="agent-state-value">pending</span>' in html
    agent_list = html[html.index('<ul id="agent-list"'):html.index("</ul>", html.index('<ul id="agent-list"'))]
    assert " p:" not in agent_list
    assert " r:" not in agent_list
    assert 'id="kill-all-agents-btn"' in html
    assert 'id="kill-all-agents-btn" type="button" class="agent-kill-all" title="Stop all agents" hidden' in html
    assert 'data-agent-kill="felix"' not in html


def test_render_sidebar_agents_show_kill_controls_when_running(
    session_dir: Path,
    repo: Path,
):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    html = render.render_page(
        s,
        s.id,
        files,
        [],
        head_shifted=False,
        agent_runtime={"felix": {"process_status": "running", "protocol_status": "pending"}},
    )

    assert 'id="kill-all-agents-btn" type="button" class="agent-kill-all" title="Stop all agents" hidden' not in html
    assert 'data-agent-kill="felix"' in html
    assert 'class="agent-state-row"' in html
    assert '<span class="agent-state-label">process</span> <span class="agent-state-value">running</span>' in html
    assert '<span class="agent-state-label">review</span> <span class="agent-state-value">pending</span>' in html


def test_render_sidebar_action_shortcuts_use_namespaced_bindings(
    session_dir: Path,
    repo: Path,
):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    html = render.render_page(s, s.id, files, [], head_shifted=False)

    assert '<kbd class="prefix">␣</kbd><kbd>c</kbd><kbd>a</kbd>' in html
    assert '<kbd class="prefix">␣</kbd><kbd>c</kbd><kbd>c</kbd>' in html
    assert '<kbd class="prefix">␣</kbd><kbd>a</kbd><kbd>K</kbd>' in html
    assert '<kbd class="prefix">⌃␣</kbd><kbd>a</kbd><kbd>b</kbd>' in html
    assert '<kbd class="prefix">␣</kbd><kbd>a</kbd></span><span class="desc">add global comment' not in html


def test_render_global_section_appears_above_files(session_dir: Path, repo: Path):
    """The high-level feedback section is rendered, contains the add button,
    and includes any file=='' comment in its own block."""
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    g = Comment(author="vera", file="", line=0, body="scope concern",
                severity="warning")
    a = Comment(author="felix", file="foo.py", line=1, body="anchored",
                severity="nit")
    store.append_comment(session_dir, g)
    store.append_comment(session_dir, a)
    html = render.render_page(s, s.id, files,
                              store.read_all_comments(session_dir),
                              head_shifted=False)
    # The section exists with the expected anchor and add button.
    assert 'id="global"' in html
    assert 'id="add-global-btn"' in html
    assert "High-level feedback" in html
    # Global comment renders inside the global container.
    g_idx = html.index('id="global-comments"')
    g_close = html.index("</section>", g_idx)
    assert "scope concern" in html[g_idx:g_close]
    # Anchored comment is still in its file thread, not the global section.
    assert "anchored" in html
    assert "anchored" not in html[g_idx:g_close]
    # Sidebar gets a high-level row that links to #global.
    assert 'href="#global"' in html
    assert "High-level feedback" in html


def test_render_global_review_category_badge(session_dir: Path, repo: Path):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c = Comment(author="vera", file="", line=0, body="must fix",
                category="request-changes")
    store.append_comment(session_dir, c)

    html = render.render_page(s, s.id, files, store.read_all_comments(session_dir),
                              head_shifted=False)
    assert 'class="category request-changes"' in html
    assert "blocking" in html


def test_render_global_section_excludes_globals_from_per_file_counts(
    session_dir: Path, repo: Path
):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    # 2 globals (1 open + 1 resolved), 0 per-file → file row shows em-dash.
    g1 = Comment(author="vera", file="", line=0, body="A", severity="warning")
    g2 = Comment(author="vera", file="", line=0, body="B", severity="suggestion",
                 resolved=True)
    store.append_comment(session_dir, g1)
    store.append_comment(session_dir, g2)
    html = render.render_page(s, s.id, files,
                              store.read_all_comments(session_dir),
                              head_shifted=False)
    # Per-file count cell for foo.py is empty (em-dash placeholder).
    assert 'data-file="foo.py"' in html
    # Global sidebar row reports 1 open / 2 total.
    assert '<span class="count open">1</span>' in html
    assert '<span class="count muted">/2</span>' in html


def test_server_post_global_comment(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"scope": "global", "body": "missing rollback plan",
             "severity": "warning", "author": "jakub"},
        )
        assert code == 201
        assert data["file"] == ""
        assert data["line"] == 0
        assert data["body"] == "missing rollback plan"

        cs = store.read_all_comments(session_dir)
        assert len(cs) == 1
        assert cs[0].file == "" and cs[0].line == 0
    finally:
        srv.shutdown()


def test_server_post_global_review_category(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"scope": "global", "body": "lgtm", "category": "approve"},
        )
        assert code == 201
        assert data["category"] == "approve"

        [comment] = store.read_all_comments(session_dir)
        assert comment.category == "approve"
    finally:
        srv.shutdown()


def test_server_rejects_anchored_review_category(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "lgtm", "category": "approve"},
        )
        assert code == 400
        assert "only valid on global comments" in data["error"]
    finally:
        srv.shutdown()


def test_server_post_global_via_omitted_file_and_line(session_dir: Path):
    """Posting with neither `file` nor `line` is treated as a global comment."""
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"body": "high-level concern", "severity": "suggestion"},
        )
        assert code == 201
        assert data["file"] == ""
    finally:
        srv.shutdown()


def test_render_thread_includes_reply_button_and_replies_inset(
    session_dir: Path, repo: Path
):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    parent = Comment(author="vera", file="foo.py", line=1, body="parent",
                     severity="warning")
    store.append_comment(session_dir, parent)
    reply = Comment(author="felix", file="foo.py", line=1, body="agreed",
                    severity="suggestion", reply_to=parent.id)
    store.append_comment(session_dir, reply)
    html_out = render.render_page(s, s.id, files,
                                  store.read_all_comments(session_dir),
                                  head_shifted=False)
    assert f'data-thread-id="{parent.id}"' in html_out
    assert 'class="reply-btn"' in html_out
    assert f'data-reply-to="{parent.id}"' in html_out
    assert f'data-resolve="{parent.id}"' in html_out
    # Reply renders with .reply class and no severity badge of its own.
    cid_idx = html_out.index(f'data-cid="{reply.id}"')
    div_open = html_out.rfind("<div ", 0, cid_idx)
    assert "comment reply" in html_out[div_open:cid_idx]
    # The reply-block body contains its meta but no severity span.
    body_end = html_out.index("</div>", cid_idx)
    assert "sev suggestion" not in html_out[cid_idx:body_end]


def test_render_thread_swaps_to_unresolve_when_resolved(
    session_dir: Path, repo: Path
):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    parent = Comment(author="vera", file="foo.py", line=1, body="x",
                     severity="warning", resolved=True)
    store.append_comment(session_dir, parent)
    html_out = render.render_page(s, s.id, files,
                                  store.read_all_comments(session_dir),
                                  head_shifted=False)
    assert f'data-unresolve="{parent.id}"' in html_out
    assert f'data-resolve="{parent.id}"' not in html_out


def test_render_resolved_thread_collapsed_by_default(
    session_dir: Path, repo: Path
):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    parent = Comment(author="vera", file="foo.py", line=1, body="x",
                     severity="warning", resolved=True)
    store.append_comment(session_dir, parent)
    store.append_comment(session_dir, Comment(
        author="felix", file="foo.py", line=1, body="reply",
        severity="suggestion", reply_to=parent.id,
    ))
    html_out = render.render_page(s, s.id, files,
                                  store.read_all_comments(session_dir),
                                  head_shifted=False)

    assert 'class="thread resolved collapsed"' in html_out
    assert 'data-default-collapsed="1"' in html_out
    assert f'data-thread-collapse="{parent.id}"' in html_out
    assert 'aria-expanded="false"' in html_out
    assert "comment hidden, 1 reply hidden" in html_out


def test_render_unresolved_thread_has_expanded_collapse_button(
    session_dir: Path, repo: Path
):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    parent = Comment(author="vera", file="foo.py", line=1, body="x",
                     severity="warning")
    store.append_comment(session_dir, parent)
    html_out = render.render_page(s, s.id, files,
                                  store.read_all_comments(session_dir),
                                  head_shifted=False)

    assert f'data-thread-collapse="{parent.id}"' in html_out
    assert 'aria-expanded="true"' in html_out
    assert 'data-default-collapsed="0"' in html_out
    assert 'class="thread collapsed"' not in html_out


def test_sidebar_counts_exclude_replies(session_dir: Path, repo: Path):
    """A chatty thread of replies must not inflate the open count."""
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    parent = Comment(author="vera", file="foo.py", line=1, body="P",
                     severity="warning")
    store.append_comment(session_dir, parent)
    for i in range(5):
        store.append_comment(session_dir, Comment(
            author="felix", file="foo.py", line=1, body=f"r{i}",
            severity="suggestion", reply_to=parent.id,
        ))
    html_out = render.render_page(s, s.id, files,
                                  store.read_all_comments(session_dir),
                                  head_shifted=False)
    # foo.py file row in sidebar should report 1 open / 1 total — replies don't count.
    assert '<span class="count open">1</span>' in html_out
    assert '<span class="count muted">/1</span>' in html_out


def test_server_post_reply_and_unresolve(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        # Post parent
        _, parent = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "p", "author": "vera"},
        )
        assert parent["reply_to"] is None
        # Post reply
        code, child = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"reply_to": parent["id"], "body": "r", "author": "felix"},
        )
        assert code == 201
        assert child["reply_to"] == parent["id"]
        assert child["file"] == "foo.py"
        assert child["line"] == 1

        # Resolve then unresolve via API
        _post(
            f"http://127.0.0.1:{port}/{session_id}/api/resolve",
            {"comment_id": parent["id"], "by": "jakub"},
        )
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/unresolve",
            {"comment_id": parent["id"]},
        )
        assert code == 200
        assert data["unresolved"] == parent["id"]

        cs = store.read_all_comments(session_dir)
        parent_stored = next(c for c in cs if c.id == parent["id"])
        assert parent_stored.resolved is False
    finally:
        srv.shutdown()


def test_server_post_reply_unknown_parent_returns_404(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"reply_to": "c_nonexistent", "body": "r"},
        )
        assert code == 404
        assert "not found" in data["error"]
    finally:
        srv.shutdown()


def test_server_inbox_endpoint_and_render(session_dir: Path, tmp_path: Path):
    """Posting an ask + reply via polling.write_question/write_reply lands
    in /api/inbox and in the rendered transcript section."""
    from peanut_review import polling
    polling.write_question(session_dir, "vera", "python isn't on PATH")
    polling.write_reply(session_dir, "vera", "q_001",
                        "source .venv/bin/activate first")

    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _get(f"http://127.0.0.1:{port}/{session_id}/api/inbox")
        assert code == 200
        entries = json.loads(data)
        assert len(entries) == 1
        e = entries[0]
        assert e["agent"] == "vera"
        assert e["id"] == "q_001"
        assert e["question"].startswith("python isn't")
        assert e["reply"] is not None
        assert e["reply"]["answer"].startswith("source .venv")
        qts = e["timestamp"]
        ats = e["reply"]["timestamp"]

        # Page render must include the inbox section + a data-key per entry.
        code, body = _get(f"http://127.0.0.1:{port}/{session_id}")
        assert code == 200
        text = body.decode("utf-8")
        assert 'id="inbox"' in text
        assert 'Agent help inbox' in text
        assert 'data-key="vera/q_001"' in text
        assert 'data-replied="1"' in text
        assert f'<time class="comment-time ix-time" datetime="{qts}" title="{qts}">' in text
        assert f'<time class="comment-time ix-time" datetime="{ats}" title="{ats}">' in text
        assert f'<span class="ts mono">{qts}</span>' not in text
    finally:
        srv.shutdown()


def test_server_notes_endpoint_and_render(session_dir: Path):
    store.append_note(session_dir, Note(
        author="petra",
        timestamp="2020-01-01T00:00:00+00:00",
        body="## Test Execution\n`llvm-lit` passed",
    ))

    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _get(f"http://127.0.0.1:{port}/{session_id}/api/notes")
        assert code == 200
        notes = json.loads(data)
        assert len(notes) == 1
        assert notes[0]["author"] == "petra"
        assert notes[0]["body"].startswith("## Test Execution")

        code, body = _get(f"http://127.0.0.1:{port}/{session_id}")
        assert code == 200
        text = body.decode("utf-8")
        assert 'id="inbox"' in text
        assert "Agent activity" in text
        assert 'data-key="note/' in text
        assert (
            '<time class="comment-time ix-time" datetime="2020-01-01T00:00:00+00:00" '
            'title="2020-01-01T00:00:00+00:00">'
        ) in text
        assert "ago</time>" in text
        assert '<span class="ts mono">2020-01-01T00:00:00+00:00</span>' not in text
        assert "Test Execution" in text
        assert "llvm-lit" in text
    finally:
        srv.shutdown()


def test_render_stale_and_resolved_classes(session_dir: Path, repo: Path):
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c1 = Comment(author="felix", file="foo.py", line=1, body="stale one",
                 severity="nit", stale=True)
    c2 = Comment(author="vera", file="foo.py", line=2, body="resolved one",
                 severity="nit", resolved=True)
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


def _mark_github_backed(session_dir: Path) -> None:
    s = sess.load_session(session_dir)
    s.github = GitHubPR(
        repo="acme/foo",
        number=42,
        url="https://github.com/acme/foo/pull/42",
        head_sha=s.current_head,
        base_sha=s.original_head,
        title="t",
    )
    sess.save_session(session_dir, s)


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
        assert data["agents"][0]["model"] == "m"
        assert data["agents"][0]["process_status"] == "pending"
        assert data["agents"][0]["protocol_status"] == "pending"
    finally:
        srv.shutdown()


def test_server_kill_agents_endpoint(session_dir: Path, monkeypatch):
    calls = []

    def fake_kill_agents(session_dir_arg, **kwargs):
        calls.append((Path(session_dir_arg), kwargs))
        return [{
            "name": "felix",
            "status": "skipped",
            "reason": "not running",
            "signals": [],
        }]

    monkeypatch.setattr(web_app.agent_control, "kill_agents", fake_kill_agents)
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/agents/kill",
            {"agent": "felix"},
        )
        assert code == 200
        assert calls[-1] == (session_dir, {"agent_names": ["felix"]})
        assert data["results"][0]["status"] == "skipped"
        assert data["agents"][0]["name"] == "felix"
        assert data["agents"][0]["model"] == "m"

        code, _ = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/agents/kill",
            {},
        )
        assert code == 200
        assert calls[-1] == (session_dir, {"agent_names": None})
    finally:
        srv.shutdown()


def test_server_gh_preview_defaults_humans_on_agents_off(session_dir: Path):
    _mark_github_backed(session_dir)
    agent_comment = Comment(author="felix", file="foo.py", line=2, body="agent")
    human_comment = Comment(author="jakub", file="foo.py", line=2, body="human")
    store.append_comment(session_dir, agent_comment)
    store.append_comment(session_dir, human_comment)

    srv, session_id, port = _start_server(session_dir)
    try:
        code, raw = _get(f"http://127.0.0.1:{port}/{session_id}/api/gh/preview")
        assert code == 200
        data = json.loads(raw)
        items = {item["id"]: item for item in data["new_top"]}

        assert items[agent_comment.id]["is_agent"] is True
        assert items[agent_comment.id]["default_included"] is False
        assert items[human_comment.id]["is_agent"] is False
        assert items[human_comment.id]["default_included"] is True
    finally:
        srv.shutdown()


def test_server_gh_push_filters_to_selected_comment_ids(
    session_dir: Path,
    monkeypatch,
):
    _mark_github_backed(session_dir)
    agent_comment = Comment(author="felix", file="foo.py", line=2, body="agent")
    human_comment = Comment(author="jakub", file="foo.py", line=2, body="human")
    store.append_comment(session_dir, agent_comment)
    store.append_comment(session_dir, human_comment)
    captured_ids = []

    def fake_execute_push(session_dir_arg, session_arg, ghpr_arg, plan):
        del session_dir_arg, session_arg, ghpr_arg
        selected = [*plan.new_top, *plan.new_replies, *plan.edits]
        captured_ids.append([c.id for c in selected])
        return web_app.gh_push.PushResult(pushed=plan.total)

    monkeypatch.setattr(web_app.gh_push, "execute_push", fake_execute_push)

    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/gh/push",
            {"comment_ids": [agent_comment.id]},
        )
        assert code == 200
        assert data["summary"] == "Pushed 1."
        assert captured_ids[-1] == [agent_comment.id]
    finally:
        srv.shutdown()


def test_server_gh_push_default_excludes_agent_comments(
    session_dir: Path,
    monkeypatch,
):
    _mark_github_backed(session_dir)
    agent_comment = Comment(author="felix", file="foo.py", line=2, body="agent")
    human_comment = Comment(author="jakub", file="foo.py", line=2, body="human")
    store.append_comment(session_dir, agent_comment)
    store.append_comment(session_dir, human_comment)
    captured_ids = []

    def fake_execute_push(session_dir_arg, session_arg, ghpr_arg, plan):
        del session_dir_arg, session_arg, ghpr_arg
        selected = [*plan.new_top, *plan.new_replies, *plan.edits]
        captured_ids.append([c.id for c in selected])
        return web_app.gh_push.PushResult(pushed=plan.total)

    monkeypatch.setattr(web_app.gh_push, "execute_push", fake_execute_push)

    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/gh/push",
            {},
        )
        assert code == 200
        assert data["summary"] == "Pushed 1."
        assert captured_ids[-1] == [human_comment.id]
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


def test_server_post_delete_and_undelete(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, c = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "bad", "severity": "nit",
             "author": "felix"},
        )
        assert code == 201
        cid = c["id"]

        # Delete
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/delete",
            {"comment_id": cid, "by": "jakub"},
        )
        assert code == 200
        assert data["deleted"] == cid

        # Default comment list hides it
        _, raw = _get(f"http://127.0.0.1:{port}/{session_id}/api/comments")
        assert json.loads(raw) == []

        # ?include_deleted=1 brings it back with metadata
        _, raw = _get(
            f"http://127.0.0.1:{port}/{session_id}/api/comments?include_deleted=1"
        )
        listed = json.loads(raw)
        assert len(listed) == 1
        assert listed[0]["deleted"] is True
        assert listed[0]["deleted_by"] == "jakub"

        # Rendered page must not include the deleted comment — look for the
        # specific data-cid marker, not just the body text (which can appear
        # in CSS/JS as a substring, e.g. "badge").
        _, body = _get(f"http://127.0.0.1:{port}/{session_id}/")
        assert f'data-cid="{cid}"'.encode() not in body

        # Undelete
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/undelete",
            {"comment_id": cid},
        )
        assert code == 200
        _, raw = _get(f"http://127.0.0.1:{port}/{session_id}/api/comments")
        assert len(json.loads(raw)) == 1
    finally:
        srv.shutdown()


def test_server_delete_missing_comment_returns_404(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/delete",
            {"comment_id": "c_missing"},
        )
        assert code == 404
        assert "not found" in data["error"]
    finally:
        srv.shutdown()


def test_server_delete_button_rendered_on_each_comment(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        _post(
            f"http://127.0.0.1:{port}/{session_id}/api/comments",
            {"file": "foo.py", "line": 1, "body": "x", "severity": "nit",
             "author": "felix"},
        )
        _, body = _get(f"http://127.0.0.1:{port}/{session_id}/")
        text = body.decode("utf-8")
        assert 'data-delete=' in text
        assert 'class="danger"' in text
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
        new_head = _git(repo, "rev-parse", "HEAD").strip()
        migrated = sess.load_session(session_dir)
        assert migrated.current_head == new_head
        assert migrated.topic_ref == new_head
        assert migrated.diff_commands == [f"git diff {migrated.base_ref}...{new_head}"]

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
    import sys
    import time as _t

    # Root = session's parent (which holds this single session).
    root = session_dir.parent

    pidfile = web_app.pidfile_path(root)
    assert not pidfile.exists()

    # Port 0 means OS-assigned. Don't pre-pick a port via bind/close — under
    # parallel xdist runs another worker can grab it in the gap, and serve()
    # then exits before writing the pidfile, leaving the test to time out
    # without ever surfacing the bind error.
    proc = subprocess.Popen(
        [sys.executable, "-m", "peanut_review", "serve",
         "--root", str(root),
         "--host", "127.0.0.1", "--port", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = _t.monotonic() + 10.0
        while _t.monotonic() < deadline and not pidfile.exists():
            # Fail fast (with stderr) if the subprocess died before writing
            # the pidfile, instead of waiting for the full deadline.
            if proc.poll() is not None:
                stdout, stderr = proc.communicate(timeout=1.0)
                raise AssertionError(
                    f"serve subprocess exited with rc={proc.returncode} "
                    f"before writing pidfile.\n"
                    f"stdout: {stdout.decode(errors='replace')}\n"
                    f"stderr: {stderr.decode(errors='replace')}"
                )
            _t.sleep(0.05)
        assert pidfile.exists(), "serve didn't write pidfile within 10s"
        payload = json.loads(pidfile.read_text())
        assert payload["pid"] == proc.pid
        assert payload["port"] > 0
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


def test_index_and_api_sessions_use_github_title(tmp_path: Path, repo: Path):
    root = tmp_path / "review-root"
    root.mkdir()
    s, sd = sess.create_session(
        workspace=str(repo), base_ref="main~1", topic_ref="main",
        session_dir=str(root / "sess-a"),
    )
    s.base_ref = "base-sha"
    s.topic_ref = "topic-sha"
    s.github = GitHubPR(
        repo="acme/foo",
        number=42,
        url="https://github.com/acme/foo/pull/42",
        head_sha="topic-sha",
        base_sha="base-sha",
        title="Add a feature",
    )
    sess.save_session(sd, s)

    registry = web_app.SessionRegistry([root])
    srv = web_app.make_server("127.0.0.1", 0, registry)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        code, body = _get(f"http://127.0.0.1:{port}/")
        assert code == 200
        text = body.decode("utf-8")
        assert "Add a feature" in text
        assert "acme/foo#42" in text
        assert "base-sha … topic-sha" not in text

        code, raw = _get(f"http://127.0.0.1:{port}/api/sessions")
        assert code == 200
        [item] = json.loads(raw)
        assert item["change_label"] == "Add a feature"
        assert item["github_title"] == "Add a feature"
        assert item["session_subtitle"] == "acme/foo#42"
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
        body="spans two lines", severity="warning",
    ))
    store.append_comment(session_dir, Comment(
        author="vera", file="foo.py", line=1,
        body="single line", severity="nit",
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


def test_group_threads_by_anchor_uses_end_line_for_ranges():
    from peanut_review.web.render import _group_threads_by_anchor
    comments = [
        Comment(author="a", file="foo.py", line=5, body="single"),
        Comment(author="b", file="foo.py", line=5, end_line=10, body="range"),
        Comment(author="c", file="foo.py", line=10, end_line=10, body="degenerate"),
    ]
    g = _group_threads_by_anchor(comments)
    # Range-ending-at-10 and the line=10 single-line both anchor at 10 →
    # two distinct top-level threads at that key.
    assert len(g[("foo.py", 10)]) == 2
    # Single at line 5 has its own anchor with one thread.
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


def test_client_global_composer_includes_category_selector():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("function openGlobalForm()")
    end = text.index("function setThreadResolved", start)
    block = text[start:end]

    assert 'select class="category"' in block
    assert 'form.querySelector(".category")?.value || "comment"' in block


def test_client_composer_chord_sets_global_review_category():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("function startPendingComposerActions")
    end = text.index("function handlePending", start)
    block = text[start:end]

    assert 'const category = composer.querySelector(".category")' in block
    assert 'map.a = { label: "approve"' in block
    assert 'setCategory("approve", "approve")' in block
    assert 'map.b = { label: "blocking"' in block
    assert 'setCategory("request-changes", "blocking")' in block


def test_client_comment_renderer_includes_relative_time():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("function relativeTimeLabel")
    end = text.index("function renderThreadActions", start)
    block = text[start:end]

    assert "function timeTag" in block
    assert "function commentTime" in block
    assert '"comment-time"' in block
    assert "${commentTime(c)}" in block


def test_client_comment_renderer_supports_thread_collapse():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("function collapseSummary")
    end = text.index("function ensureThread", start)
    block = text[start:end]

    assert "function collapseButton" in block
    assert 'class="thread-collapse"' in block
    assert "THREAD_COLLAPSE_KEY" in block
    assert "localStorage" in block
    assert "applyThreadCollapsePreferences()" in block
    assert 'data-default-collapsed="${defaultCollapsed}"' in block


def test_client_agent_renderer_uses_labeled_two_line_state_fields():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("function agentStateField")
    end = text.index("function updateAgentList", start)
    block = text[start:end]

    assert "function agentStateField" in block
    assert '"status"' in block
    assert '"process"' in block
    assert '"review"' in block
    assert "agent-main" in block
    assert "agent-state-row" in block
    assert "agent-state-label" in block
    assert "p:${process}" not in block
    assert "r:${protocol}" not in block


def test_client_edit_forms_size_textarea_to_existing_body_height():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("function fitEditTextarea")
    end = text.index("function toggleHistory", start)
    block = text[start:end]

    assert "function fitEditTextarea" in block
    assert "ta.style.minHeight" in block
    assert "ta.scrollHeight" in block
    assert "const bodyHeight = body.getBoundingClientRect().height" in block
    assert "fitEditTextarea(ta, bodyHeight)" in block
    assert 'ta.addEventListener("input", () => fitEditTextarea(ta, bodyHeight))' in block


def test_client_click_handler_toggles_thread_collapse():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("// Resolve / Unresolve / Delete / Reply")
    end = text.index("  // --- Live comment merge ---", start)
    block = text[start:end]

    assert 'ev.target.closest("[data-thread-collapse]")' in block
    assert "setThreadCollapsed(threadEl, !threadEl.classList.contains(\"collapsed\"))" in block
    assert "resetCollapsePreference: true" in block


def test_client_keymap_has_comment_collapse_chord():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("const KEYMAP =")
    end = text.index("  let pendingMap", start)
    block = text[start:end]

    assert 'c: { label: "comment…", submap:' in block
    assert 'c: { label: "toggle collapse"' in block
    assert 'clickInFocused("[data-thread-collapse]")' in block


def test_client_theme_toggle_cycles_between_system_dark_plus_and_light():
    asset_dir = Path(web_app.__file__).parent / "assets"
    for name in ("app.js", "index.js"):
        text = (asset_dir / name).read_text()
        assert 'const THEME_KEY = "pr.theme"' in text
        assert '{ value: "system", label: "system" }' in text
        assert '{ value: "dark-plus", label: "Dark+" }' in text
        assert '{ value: "light", label: "light" }' in text
        assert 'document.documentElement.dataset.theme = theme.value' in text
        assert 'setStoredTheme(THEMES[(idx + 1) % THEMES.length].value)' in text


def test_client_agent_activity_renderer_uses_relative_time():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("function renderNoteEntry")
    end = text.index("function activityEntry", start)
    block = text[start:end]

    assert "activityTime(note.timestamp" in block
    assert "activityTime(entry.timestamp" in block
    assert "activityTime(entry.reply.timestamp" in block
    assert "ix-time" in text
    assert 'class="ts mono"' not in block


def test_client_gh_push_modal_includes_selection_controls():
    text = (Path(web_app.__file__).parent / "assets" / "app.js").read_text()
    start = text.index("// --- GitHub push modal ---")
    end = text.index("  // --- Keyboard navigation ---", start)
    block = text[start:end]

    assert 'id="gh-include-agents"' in block
    assert 'class="push-select"' in block
    assert "{ comment_ids: commentIds }" in block
    assert 'class="push-delete"' in block
    assert 'data-push-delete="' in block
    assert 'data-push-edit="' in block
    assert '>Edit</button>' in block
    assert '>Delete</button>' in block
    assert "const targetHeight = target.getBoundingClientRect().height" in block
    assert "fitEditTextarea(ta, targetHeight)" in block
    assert 'api("POST", "/api/edit", { comment_id: cid, body: newBody })' in block
    assert 'api("POST", "/api/delete", { comment_id: cid })' in block


def test_server_edit_endpoint_updates_body_and_history(session_dir: Path):
    c = Comment(author="vera", file="foo.py", line=1, body="v1", severity="nit")
    store.append_comment(session_dir, c)
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/edit",
            {"comment_id": c.id, "body": "v2", "severity": "warning",
             "author": "jakub"},
        )
        assert code == 200
        assert data["body"] == "v2"
        assert data["severity"] == "warning"
        assert data["edited_by"] == "jakub"
        assert len(data["versions"]) == 1
        assert data["versions"][0]["body"] == "v1"
    finally:
        srv.shutdown()


def test_server_edit_endpoint_unknown_comment_returns_404(session_dir: Path):
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/edit",
            {"comment_id": "c_missing", "body": "x"},
        )
        assert code == 404
        assert "not found" in data["error"]
    finally:
        srv.shutdown()


def test_server_edit_endpoint_requires_body_or_severity(session_dir: Path):
    c = Comment(author="vera", file="foo.py", line=1, body="x")
    store.append_comment(session_dir, c)
    srv, session_id, port = _start_server(session_dir)
    try:
        code, data = _post(
            f"http://127.0.0.1:{port}/{session_id}/api/edit",
            {"comment_id": c.id},
        )
        assert code == 400
        assert "body or severity" in data["error"]
    finally:
        srv.shutdown()


def test_render_edited_indicator_appears_after_edit(
    session_dir: Path, repo: Path
):
    """The page render shows the 'edited' badge with a data-history hook so
    the JS can pop the version history without an extra round-trip."""
    s = sess.load_session(session_dir)
    files = diffmod.parse_diff(str(repo), s.base_ref, s.topic_ref)
    c = Comment(author="vera", file="foo.py", line=1, body="v1",
                severity="nit")
    store.append_comment(session_dir, c)
    store.edit_comment(session_dir, c.id, body="v2", edited_by="jakub")
    html_out = render.render_page(s, s.id, files,
                                   store.read_all_comments(session_dir),
                                   head_shifted=False)
    assert "edited-badge" in html_out
    assert f'data-history="{c.id}"' in html_out
    assert f'data-edit="{c.id}"' in html_out


def test_server_filter_comments_since_id(session_dir: Path):
    """The `--since <id>` cursor (replaces the old `--round N` filter) lets
    the orchestrator poll for new activity since they last looked."""
    c1 = Comment(author="felix", file="foo.py", line=1, body="r1", severity="nit")
    c2 = Comment(author="felix", file="foo.py", line=2, body="r2", severity="nit")
    store.append_comment(session_dir, c1)
    store.append_comment(session_dir, c2)
    srv, session_id, port = _start_server(session_dir)
    try:
        code, raw = _get(
            f"http://127.0.0.1:{port}/{session_id}/api/comments?since={c1.id}",
        )
        assert code == 200
        data = json.loads(raw)
        assert len(data) == 1
        assert data[0]["body"] == "r2"
    finally:
        srv.shutdown()
