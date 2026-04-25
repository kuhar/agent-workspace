"""Tests for the gh CLI wrapper, init --gh-pr, and gh-push / gh-pull.

Stubs out `gh` with a tiny Python shim pointed to via the
`PEANUT_REVIEW_GH_BIN` env var. The shim's behavior (canned response per
argv pattern) is configured per-test via files in a scratch dir.
"""
from __future__ import annotations

import io
import json
import os
import stat
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import pytest

from peanut_review import gh, models
from peanut_review import session as sess
from peanut_review import store
from peanut_review.cli import main


# ---------------- gh shim ----------------

_SHIM_PY = """#!/usr/bin/env python3
import json, os, sys
shim_dir = os.environ["PEANUT_GH_SHIM_DIR"]
calls_path = os.path.join(shim_dir, "calls.jsonl")
fixtures_path = os.path.join(shim_dir, "fixtures.json")

argv = sys.argv[1:]
stdin = ""
if "--input" in argv and argv[argv.index("--input") + 1] == "-":
    stdin = sys.stdin.read()

with open(calls_path, "a") as f:
    f.write(json.dumps({"argv": argv, "stdin": stdin}) + "\\n")

with open(fixtures_path) as f:
    fixtures = json.load(f)

# Match the most-specific fixture by checking each rule's `match` (a list of
# substrings that must all appear as argv elements).
for fx in fixtures:
    if all(m in argv for m in fx["match"]):
        if fx.get("rc"):
            sys.stderr.write(fx.get("stderr", ""))
            sys.exit(fx["rc"])
        sys.stdout.write(fx.get("stdout", ""))
        sys.exit(0)

sys.stderr.write(f"shim: no fixture matched argv={argv}\\n")
sys.exit(127)
"""


@pytest.fixture
def gh_shim(tmp_path: Path, monkeypatch):
    """Install a fake `gh` and yield helpers to set fixtures + read calls."""
    shim_dir = tmp_path / "gh-shim"
    shim_dir.mkdir()
    bin_path = shim_dir / "gh"
    bin_path.write_text(_SHIM_PY)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PEANUT_REVIEW_GH_BIN", str(bin_path))
    monkeypatch.setenv("PEANUT_GH_SHIM_DIR", str(shim_dir))

    fixtures_path = shim_dir / "fixtures.json"
    calls_path = shim_dir / "calls.jsonl"
    fixtures_path.write_text("[]")

    class Shim:
        def set_fixtures(self, fxs: list[dict]) -> None:
            fixtures_path.write_text(json.dumps(fxs))

        def calls(self) -> list[dict]:
            if not calls_path.exists():
                return []
            return [json.loads(line) for line in calls_path.read_text().splitlines() if line]

    return Shim()


# ---------------- parse_pr_spec ----------------


@pytest.mark.parametrize("spec,expect", [
    ("acme/foo#42", ("acme/foo", 42)),
    ("acme/foo/pull/42", ("acme/foo", 42)),
    ("https://github.com/acme/foo/pull/42", ("acme/foo", 42)),
    ("https://github.com/acme/foo/pull/42/", ("acme/foo", 42)),
])
def test_parse_pr_spec_accepts_common_forms(spec, expect):
    assert gh.parse_pr_spec(spec) == expect


@pytest.mark.parametrize("bad", [
    "acme/foo",         # no number
    "acme#42",          # no repo
    "/foo#42",          # missing owner
    "acme/foo#abc",     # non-numeric
    "",
])
def test_parse_pr_spec_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        gh.parse_pr_spec(bad)


# ---------------- fetch_pr_info ----------------


def test_fetch_pr_info_parses_gh_view_output(gh_shim):
    gh_shim.set_fixtures([{
        "match": ["pr", "view", "42"],
        "stdout": json.dumps({
            "number": 42,
            "headRefOid": "abc123",
            "baseRefOid": "def456",
            "url": "https://github.com/acme/foo/pull/42",
            "title": "Add a feature",
        }),
    }])
    info = gh.fetch_pr_info("acme/foo", 42)
    assert info.repo == "acme/foo"
    assert info.number == 42
    assert info.head_sha == "abc123"
    assert info.base_sha == "def456"
    assert info.title == "Add a feature"


def test_fetch_pr_info_propagates_gh_errors(gh_shim):
    gh_shim.set_fixtures([{
        "match": ["pr", "view"],
        "rc": 1,
        "stderr": "could not find pull request",
    }])
    with pytest.raises(gh.GhError) as ei:
        gh.fetch_pr_info("acme/foo", 99)
    assert "could not find" in ei.value.stderr


# ---------------- post helpers ----------------


def test_post_review_comment_sends_json_via_stdin(gh_shim):
    gh_shim.set_fixtures([{
        "match": ["api", "repos/acme/foo/pulls/42/comments", "-X", "POST"],
        "stdout": json.dumps({"id": 123, "html_url": "https://example/c/123"}),
    }])
    resp = gh.post_review_comment(
        "acme/foo", 42,
        body="bad take", commit_id="abc123",
        path="src/x.py", line=10,
    )
    assert resp["id"] == 123
    [call] = gh_shim.calls()
    payload = json.loads(call["stdin"])
    assert payload["body"] == "bad take"
    assert payload["commit_id"] == "abc123"
    assert payload["path"] == "src/x.py"
    assert payload["line"] == 10
    assert payload["side"] == "RIGHT"
    assert "start_line" not in payload  # single-line, no range


def test_post_review_comment_with_range_includes_start_line(gh_shim):
    gh_shim.set_fixtures([{
        "match": ["api", "repos/acme/foo/pulls/42/comments"],
        "stdout": json.dumps({"id": 124, "html_url": ""}),
    }])
    gh.post_review_comment(
        "acme/foo", 42, body="b", commit_id="abc",
        path="x.py", line=20, start_line=15,
    )
    [call] = gh_shim.calls()
    payload = json.loads(call["stdin"])
    assert payload["start_line"] == 15
    assert payload["start_side"] == "RIGHT"


def test_post_issue_comment_routes_to_issues_endpoint(gh_shim):
    gh_shim.set_fixtures([{
        "match": ["api", "repos/acme/foo/issues/42/comments", "-X", "POST"],
        "stdout": json.dumps({"id": 999, "html_url": "https://example/i/999"}),
    }])
    resp = gh.post_issue_comment("acme/foo", 42, body="overall lgtm")
    assert resp["id"] == 999
    [call] = gh_shim.calls()
    payload = json.loads(call["stdin"])
    assert payload == {"body": "overall lgtm"}


# ---------------- fetch_*_comments paginated ----------------


def test_fetch_review_comments_concatenates_paginated_arrays(gh_shim):
    # gh api --paginate emits sequential JSON arrays back-to-back ([{...}][{...}]);
    # the wrapper must merge them into one list.
    p1 = json.dumps([{"id": 1, "body": "a"}])
    p2 = json.dumps([{"id": 2, "body": "b"}])
    gh_shim.set_fixtures([{
        "match": ["api", "repos/acme/foo/pulls/42/comments", "--paginate"],
        "stdout": p1 + p2,
    }])
    out = gh.fetch_review_comments("acme/foo", 42)
    assert [c["id"] for c in out] == [1, 2]


def test_fetch_review_comments_handles_empty(gh_shim):
    gh_shim.set_fixtures([{
        "match": ["api", "repos/acme/foo/pulls/42/comments"],
        "stdout": "",
    }])
    assert gh.fetch_review_comments("acme/foo", 42) == []


# ---------------- init --gh-pr ----------------


def _stage_workspace(tmp_path: Path) -> str:
    """Create a tiny git repo with one committed file and one diff line."""
    import subprocess
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "base", "-q"],
                   cwd=ws, check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    (ws / "foo.py").write_text("a\nb\nc\n")
    subprocess.run(["git", "add", "."], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-m", "topic", "-q"], cwd=ws, check=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    return str(ws)


def test_init_with_gh_pr_stamps_metadata_and_uses_pr_shas(gh_shim, tmp_path):
    ws = _stage_workspace(tmp_path)
    import subprocess
    head = subprocess.run(["git", "-C", ws, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    base = subprocess.run(["git", "-C", ws, "rev-parse", "HEAD~"],
                          capture_output=True, text=True, check=True).stdout.strip()

    gh_shim.set_fixtures([{
        "match": ["pr", "view", "42"],
        "stdout": json.dumps({
            "number": 42,
            "headRefOid": head,
            "baseRefOid": base,
            "url": "https://github.com/acme/foo/pull/42",
            "title": "Add a feature",
        }),
    }])

    sd = str(tmp_path / "sess")
    rc = main([
        "--session", sd, "init",
        "--workspace", ws,
        "--gh-pr", "acme/foo#42",
    ])
    assert rc == 0

    s = sess.load_session(sd)
    assert s.id == "acme-foo-pr-42"  # auto-defaulted from PR spec
    assert s.github is not None
    assert s.github.repo == "acme/foo"
    assert s.github.number == 42
    assert s.github.head_sha == head
    assert s.github.base_sha == base
    assert s.base_ref == base   # defaulted from PR
    assert s.topic_ref == head  # defaulted from PR


def test_init_id_overrides_auto_default(gh_shim, tmp_path):
    ws = _stage_workspace(tmp_path)
    import subprocess
    head = subprocess.run(["git", "-C", ws, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    base = subprocess.run(["git", "-C", ws, "rev-parse", "HEAD~"],
                          capture_output=True, text=True, check=True).stdout.strip()

    gh_shim.set_fixtures([{
        "match": ["pr", "view"],
        "stdout": json.dumps({
            "number": 42, "headRefOid": head, "baseRefOid": base,
            "url": "u", "title": "t",
        }),
    }])

    sd = str(tmp_path / "sess")
    rc = main([
        "--session", sd, "init",
        "--workspace", ws,
        "--gh-pr", "acme/foo#42",
        "--id", "my-review",
    ])
    assert rc == 0
    assert sess.load_session(sd).id == "my-review"


def test_init_id_rejects_reserved_route_and_bad_chars(tmp_path):
    ws = _stage_workspace(tmp_path)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", str(tmp_path / "s1"), "init",
                   "--workspace", ws, "--id", "api"])
    assert rc == 1
    assert "reserved" in err.getvalue()

    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", str(tmp_path / "s2"), "init",
                   "--workspace", ws, "--id", "has/slash"])
    assert rc == 1
    assert "invalid session id" in err.getvalue()


# ---------------- gh-push ----------------


def _make_gh_session(tmp_path: Path) -> str:
    """Build a session with .github populated, no real gh fetch involved."""
    sd = tmp_path / "sess"
    (sd / "comments").mkdir(parents=True)
    (sd / "signals").mkdir()
    s = models.Session(
        id="acme-foo-pr-42",
        workspace=str(tmp_path),
        base_ref="def", topic_ref="abc",
        original_head="abc", current_head="abc",
        github=models.GitHubPR(
            repo="acme/foo", number=42, url="u",
            head_sha="abc", base_sha="def", title="t",
        ),
    )
    sess.save_session(sd, s)
    return str(sd)


def test_gh_push_anchored_and_global(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    store.append_comment(sd, models.Comment(
        author="vera", file="src/x.py", line=10, body="anchored",
        severity="warning",
    ))
    store.append_comment(sd, models.Comment(
        author="felix", file="", line=0, body="global",
        severity="suggestion",
    ))

    gh_shim.set_fixtures([
        {
            "match": ["api", "repos/acme/foo/pulls/42/comments", "-X", "POST"],
            "stdout": json.dumps({"id": 100, "html_url": "https://h/c/100"}),
        },
        {
            "match": ["api", "repos/acme/foo/issues/42/comments", "-X", "POST"],
            "stdout": json.dumps({"id": 200, "html_url": "https://h/i/200"}),
        },
    ])

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "gh-push"])
    assert rc == 0
    assert "Pushed 2" in out.getvalue()

    # Both comments should now carry external_id + url + synced_body.
    cs = {c.body: c for c in store.read_all_comments(sd)}
    assert cs["anchored"].external_id == "100"
    assert cs["anchored"].external_url == "https://h/c/100"
    assert cs["anchored"].external_synced_body == "anchored"
    assert cs["global"].external_id == "200"
    assert cs["global"].external_url == "https://h/i/200"


def test_gh_push_skips_already_pushed_comments(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    store.append_comment(sd, models.Comment(
        author="vera", file="src/x.py", line=10, body="local-only",
        severity="warning",
    ))
    # Pre-pushed: should not POST again.
    pushed = models.Comment(
        author="vera", file="src/x.py", line=11, body="already on github",
        external_source="github", external_id="55", external_synced_body="already on github",
    )
    store.append_comment(sd, pushed)

    gh_shim.set_fixtures([{
        "match": ["api", "repos/acme/foo/pulls/42/comments"],
        "stdout": json.dumps({"id": 101, "html_url": "https://h/c/101"}),
    }])

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "gh-push"])
    assert rc == 0
    # Only the local-only one was POSTed.
    posts = [c for c in gh_shim.calls() if "-X" in c["argv"]]
    assert len(posts) == 1
    assert json.loads(posts[0]["stdin"])["body"] == "local-only"


def test_gh_push_uses_current_head_as_commit_id(gh_shim, tmp_path):
    """Push pins to the SHA agents *actually reviewed* (Session.current_head),
    not Session.github.head_sha which may have moved if the PR got force-pushed."""
    sd = _make_gh_session(tmp_path)
    s = sess.load_session(sd)
    s.current_head = "AGENTS_REVIEWED_THIS_SHA"
    sess.save_session(sd, s)

    store.append_comment(sd, models.Comment(
        author="vera", file="src/x.py", line=10, body="x",
    ))

    gh_shim.set_fixtures([{
        "match": ["api", "repos/acme/foo/pulls/42/comments"],
        "stdout": json.dumps({"id": 1, "html_url": ""}),
    }])

    main(["--session", sd, "gh-push"])

    [call] = gh_shim.calls()
    assert json.loads(call["stdin"])["commit_id"] == "AGENTS_REVIEWED_THIS_SHA"


def test_gh_push_skips_replies_for_now(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    parent = models.Comment(
        author="vera", file="src/x.py", line=10, body="parent",
    )
    store.append_comment(sd, parent)
    store.append_comment(sd, models.Comment(
        author="felix", file="src/x.py", line=10, body="reply",
        reply_to=parent.id,
    ))

    gh_shim.set_fixtures([{
        "match": ["api", "repos/acme/foo/pulls/42/comments"],
        "stdout": json.dumps({"id": 1, "html_url": ""}),
    }])

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "gh-push"])
    assert rc == 0
    posts = [c for c in gh_shim.calls() if "-X" in c["argv"]]
    assert len(posts) == 1  # parent only
    assert "skipped 1" in out.getvalue()


def test_gh_push_dry_run_does_not_call_gh(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    store.append_comment(sd, models.Comment(
        author="vera", file="src/x.py", line=10, body="x",
    ))

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "gh-push", "--dry-run"])
    assert rc == 0
    assert "[dry-run]" in out.getvalue()
    assert gh_shim.calls() == []
    # Comment is still local-only.
    assert store.read_all_comments(sd)[0].external_id is None


def test_gh_push_refuses_session_without_github_field(tmp_path):
    sd = tmp_path / "sess"
    (sd / "comments").mkdir(parents=True)
    (sd / "signals").mkdir()
    s = models.Session(id="x", workspace=str(tmp_path),
                       base_ref="m", topic_ref="HEAD",
                       original_head="abc", current_head="abc")
    sess.save_session(sd, s)

    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", str(sd), "gh-push"])
    assert rc == 1
    assert "not GitHub-backed" in err.getvalue()


# ---------------- gh-pull ----------------


def test_gh_pull_appends_anchored_and_global_comments(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)

    review = [{
        "id": 100,
        "user": {"login": "octocat"},
        "path": "src/x.py",
        "line": 10,
        "body": "anchored from github",
        "html_url": "https://h/c/100",
        "commit_id": "abc",
    }]
    issue = [{
        "id": 200,
        "user": {"login": "ghost"},
        "body": "global from github",
        "html_url": "https://h/i/200",
    }]
    gh_shim.set_fixtures([
        {
            "match": ["api", "repos/acme/foo/pulls/42/comments"],
            "stdout": json.dumps(review),
        },
        {
            "match": ["api", "repos/acme/foo/issues/42/comments"],
            "stdout": json.dumps(issue),
        },
    ])

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "gh-pull"])
    assert rc == 0
    assert "Pulled 1 anchored + 1 global" in out.getvalue()

    cs = store.read_all_comments(sd)
    assert {c.author for c in cs} == {"gh:octocat", "gh:ghost"}
    by_author = {c.author: c for c in cs}
    assert by_author["gh:octocat"].file == "src/x.py"
    assert by_author["gh:octocat"].line == 10
    assert by_author["gh:octocat"].external_id == "100"
    assert by_author["gh:ghost"].file == ""
    assert by_author["gh:ghost"].external_id == "200"


def test_gh_pull_dedupes_by_external_id(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    # Existing comment that already has external_id=100; pull should skip it.
    store.append_comment(sd, models.Comment(
        author="gh:octocat", file="src/x.py", line=10, body="prior",
        external_source="github", external_id="100",
    ))

    gh_shim.set_fixtures([
        {
            "match": ["api", "repos/acme/foo/pulls/42/comments"],
            "stdout": json.dumps([{
                "id": 100, "user": {"login": "octocat"},
                "path": "src/x.py", "line": 10, "body": "fresh body",
                "html_url": "https://h/c/100", "commit_id": "abc",
            }, {
                "id": 101, "user": {"login": "octocat"},
                "path": "src/x.py", "line": 11, "body": "new",
                "html_url": "https://h/c/101", "commit_id": "abc",
            }]),
        },
        {
            "match": ["api", "repos/acme/foo/issues/42/comments"],
            "stdout": "[]",
        },
    ])

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "gh-pull"])
    assert rc == 0
    assert "Pulled 1 anchored + 0 global" in out.getvalue()
    assert "1 already local" in out.getvalue()
    # Existing comment kept its body — not re-imported.
    by_id = {c.external_id: c for c in store.read_all_comments(sd)}
    assert by_id["100"].body == "prior"
    assert by_id["101"].body == "new"


def test_gh_pull_dry_run_does_not_write(gh_shim, tmp_path):
    sd = _make_gh_session(tmp_path)
    gh_shim.set_fixtures([
        {
            "match": ["api", "repos/acme/foo/pulls/42/comments"],
            "stdout": json.dumps([{
                "id": 1, "user": {"login": "x"}, "path": "a.py",
                "line": 1, "body": "b", "html_url": "", "commit_id": "abc",
            }]),
        },
        {
            "match": ["api", "repos/acme/foo/issues/42/comments"],
            "stdout": "[]",
        },
    ])

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "gh-pull", "--dry-run"])
    assert rc == 0
    assert "[dry-run]" in out.getvalue()
    assert store.read_all_comments(sd) == []


def test_gh_pull_records_in_reply_to_for_later_threading(gh_shim, tmp_path):
    """Stage 2A doesn't thread replies, but it records the GitHub
    in_reply_to_id so 2B can resolve it without re-fetching."""
    sd = _make_gh_session(tmp_path)
    gh_shim.set_fixtures([
        {
            "match": ["api", "repos/acme/foo/pulls/42/comments"],
            "stdout": json.dumps([{
                "id": 5, "user": {"login": "x"}, "path": "a.py",
                "line": 1, "body": "reply on github",
                "html_url": "", "commit_id": "abc",
                "in_reply_to_id": 4,
            }]),
        },
        {
            "match": ["api", "repos/acme/foo/issues/42/comments"],
            "stdout": "[]",
        },
    ])
    main(["--session", sd, "gh-pull"])
    [c] = store.read_all_comments(sd)
    assert c.external_in_reply_to == "4"
