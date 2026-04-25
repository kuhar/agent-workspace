"""Integration tests for the CLI — init→add-comment→verdict flow."""
import io
import json
import os
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from peanut_review.cli import main
from peanut_review import session as sess, models


def _make_workspace(files: dict[str, str] | None = None) -> str:
    """Create a temp workspace with optional files. Returns workspace path."""
    ws = tempfile.mkdtemp(prefix="pr-ws-")
    if files:
        for name, content in files.items():
            p = Path(ws) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    return ws


def _mock_git(workspace, *args):
    if args == ("rev-parse", "HEAD"):
        return "abc123def456789"
    if args[0] == "diff" and "--stat" in args:
        return "+42 -10 3 files"
    return ""


def _mock_git_empty_diff(workspace, *args):
    if args == ("rev-parse", "HEAD"):
        return "abc123def456789"
    if args[0] == "diff" and "--stat" in args:
        return ""
    return ""


def _init_session(sd, workspace="/tmp/repo", agents=None, mock_git_fn=None):
    """Helper: init a session, returns session dir."""
    mock_fn = mock_git_fn or _mock_git
    with patch("peanut_review.session._run_git", side_effect=mock_fn):
        args = ["--session", sd, "init", "--workspace", workspace]
        if agents:
            args += ["--agents", json.dumps(agents)]
        main(args)
    return sd


# ── Existing tests (updated for real workspaces) ──────────────────────


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_init_creates_session(mock_git):
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    rc = main(["--session", sd, "init", "--workspace", "/tmp/repo",
               "--agents", json.dumps([
                   {"name": "vera", "model": "opus", "persona": "vera.md"},
               ])])
    assert rc == 0
    assert (Path(sd) / "session.json").exists()


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_add_comment_and_list(mock_git):
    ws = _make_workspace({
        "src/foo.cpp": "\n".join(f"line {i}" for i in range(1, 50)),
        "src/bar.cpp": "\n".join(f"line {i}" for i in range(1, 20)),
    })
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    main(["--session", sd, "init", "--workspace", ws])

    # Add comment
    rc = main(["--session", sd, "add-comment",
               "--file", "src/foo.cpp", "--line", "42",
               "--body", "Null check needed", "--severity", "critical",
               "--author", "vera"])
    assert rc == 0

    # Add another
    main(["--session", sd, "add-comment",
          "--file", "src/bar.cpp", "--line", "10",
          "--body", "Consider refactoring",
          "--author", "felix"])

    # List all
    rc = main(["--session", sd, "comments", "--format", "json"])
    assert rc == 0


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_resolve_comment(mock_git):
    ws = _make_workspace({"a.py": "line1\nline2\nline3\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    main(["--session", sd, "init", "--workspace", ws])

    f = io.StringIO()
    with redirect_stdout(f):
        main(["--session", sd, "add-comment",
              "--file", "a.py", "--line", "1", "--body", "Fix",
              "--author", "vera"])
    # Output is now "a.py:1: line1" instead of comment ID
    output = f.getvalue().strip()
    assert "a.py:1:" in output

    # Read comment ID from store
    from peanut_review.store import read_all_comments
    comments = read_all_comments(sd)
    cid = comments[0].id

    rc = main(["--session", sd, "resolve", cid, "--by", "jakub"])
    assert rc == 0


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_signal_and_wait(mock_git):
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    main(["--session", sd, "init", "--workspace", "/tmp/repo",
          "--agents", json.dumps([{"name": "vera", "model": "opus", "persona": "vera.md"}])])

    # Signal
    with patch.dict(os.environ, {"GIT_AUTHOR_NAME": "Vera"}):
        rc = main(["--session", sd, "signal", "round-done"])
    assert rc == 0

    # Wait (should return immediately since already signaled)
    with patch.dict(os.environ, {"GIT_AUTHOR_NAME": "Vera"}):
        rc = main(["--session", sd, "wait", "round-done", "--timeout", "1"])
    assert rc == 0


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_verdict_flow(mock_git):
    ws = _make_workspace({
        "a.py": "x = 1\ny = 2\nz = 3\n",
        "b.py": "a\nb\nc\nd\ne\nf\n",
    })
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    main(["--session", sd, "init", "--workspace", ws,
          "--agents", json.dumps([{"name": "vera", "model": "opus", "persona": "vera.md"}])])

    main(["--session", sd, "add-comment",
          "--file", "a.py", "--line", "1", "--body", "Critical bug",
          "--severity", "critical", "--author", "vera"])

    rc = main(["--session", sd, "verdict", "--approve", "--body", "LGTM"])
    assert rc == 0
    assert (Path(sd) / "result.json").exists()
    result = json.loads((Path(sd) / "result.json").read_text())
    assert result["decision"] == "approve"


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_status(mock_git):
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    main(["--session", sd, "init", "--workspace", "/tmp/repo",
          "--agents", json.dumps([{"name": "vera", "model": "opus", "persona": "vera.md"}])])

    rc = main(["--session", sd, "status"])
    assert rc == 0


@patch("peanut_review.session._run_git", side_effect=_mock_git)
def test_ask_and_reply(mock_git):
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    main(["--session", sd, "init", "--workspace", "/tmp/repo"])

    # Write a question manually (simulating agent)
    from peanut_review.polling import write_question
    write_question(sd, "vera", "Where is the build dir?")

    # Check inbox
    rc = main(["--session", sd, "inbox"])
    assert rc == 0

    # Reply
    rc = main(["--session", sd, "reply", "--agent", "vera", "--id", "q_001",
               "It's in ../build-release/"])
    assert rc == 0

    # Inbox should be empty now
    from peanut_review.polling import list_unanswered
    assert len(list_unanswered(sd)) == 0


# ── Multi-pass wake-up + cursor tests ─────────────────────────────────


def test_signal_all_next_round_writes_wake_signals_and_clears_stale():
    """Round-bound signals (next-round, round-done) from prior passes must
    be cleared, otherwise a fresh `wait next-round` in the next pass would
    auto-satisfy on the leftover file. There is no round counter — this is
    purely a wake-up signal."""
    from peanut_review import polling
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, agents=[
        {"name": "vera", "model": "opus", "persona": "vera.md"},
        {"name": "felix", "model": "sonnet", "persona": "felix.md"},
    ])
    sess.transition_state(sd, models.SessionState.ROUND.value)

    # Simulate the first pass finishing and orchestrator already having
    # signaled next-round once (so leftover files exist).
    polling.write_signal(sd, "vera", "round-done")
    polling.write_signal(sd, "felix", "round-done")
    polling.write_signal(sd, "vera", "next-round")

    rc = main(["--session", sd, "signal-all", "next-round"])
    assert rc == 0

    sigs = Path(sd) / "signals"
    assert not (sigs / "vera.round-done").exists()
    assert not (sigs / "felix.round-done").exists()
    assert (sigs / "vera.next-round").exists()
    assert (sigs / "felix.next-round").exists()


def test_signal_all_next_round_from_init_lifts_to_round_state():
    """First `signal-all next-round` from INIT moves the session into
    ROUND so subsequent state-aware logic kicks in."""
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, agents=[{"name": "vera", "model": "opus", "persona": "vera.md"}])
    assert sess.load_session(sd).state == "init"

    rc = main(["--session", sd, "signal-all", "next-round"])
    assert rc == 0
    assert sess.load_session(sd).state == "round"


def test_signal_all_next_round_after_complete_refused():
    """Once a session is COMPLETE/ABORTED, signaling next-round shouldn't
    silently resurrect it."""
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, agents=[{"name": "vera", "model": "opus", "persona": "vera.md"}])
    sess.transition_state(sd, models.SessionState.COMPLETE.value)

    rc = main(["--session", sd, "signal-all", "next-round"])
    assert rc != 0
    assert sess.load_session(sd).state == "complete"


def test_signal_all_other_event_does_not_change_state():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, agents=[{"name": "vera", "model": "opus", "persona": "vera.md"}])

    rc = main(["--session", sd, "signal-all", "some-event"])
    assert rc == 0
    assert sess.load_session(sd).state == "init"


def test_comments_since_filter_returns_only_newer():
    """`comments --since <id>` is the cursor-based replacement for the old
    `--round N` filter: orchestrators record the last comment id they saw
    and ask for everything posted after."""
    ws = _make_workspace({"a.py": "line1\nline2\nline3\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws, agents=[{"name": "vera", "model": "opus", "persona": "vera.md"}])

    for n, body in enumerate(["first", "second", "third"], start=1):
        main(["--session", sd, "add-comment",
              "--file", "a.py", "--line", str(n), "--body", body,
              "--author", "vera"])

    from peanut_review.store import read_all_comments
    all_c = read_all_comments(sd)
    cursor = all_c[0].id  # first comment

    f = io.StringIO()
    with redirect_stdout(f):
        rc = main(["--session", sd, "comments",
                   "--since", cursor, "--format", "json"])
    assert rc == 0
    data = json.loads(f.getvalue())
    assert [c["body"] for c in data] == ["second", "third"]


# Issue 4: empty diff warning

def test_init_warns_on_empty_diff():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    err = io.StringIO()
    with patch("peanut_review.session._run_git", side_effect=_mock_git_empty_diff), \
         redirect_stderr(err):
        rc = main(["--session", sd, "init", "--workspace", "/tmp/repo"])
    assert rc == 0
    assert "diff is empty" in err.getvalue()


def test_init_no_warning_on_nonempty_diff():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    err = io.StringIO()
    with patch("peanut_review.session._run_git", side_effect=_mock_git), \
         redirect_stderr(err):
        rc = main(["--session", sd, "init", "--workspace", "/tmp/repo"])
    assert rc == 0
    assert "diff is empty" not in err.getvalue()


# Issue 1: line number validation

def test_add_comment_rejects_nonexistent_file():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    ws = _make_workspace()  # empty workspace
    _init_session(sd, workspace=ws)

    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "add-comment",
                   "--file", "no_such_file.py", "--line", "1",
                   "--body", "test", "--author", "vera"])
    assert rc == 1
    assert "file not found" in err.getvalue()


def test_add_comment_rejects_out_of_range_line():
    ws = _make_workspace({"short.py": "a\nb\nc\n"})  # 3 lines
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "add-comment",
                   "--file", "short.py", "--line", "99",
                   "--body", "test", "--author", "vera"])
    assert rc == 1
    assert "3 lines but line 99 is out of range" in err.getvalue()

    # Verify comment was NOT stored
    from peanut_review.store import read_all_comments
    assert len(read_all_comments(sd)) == 0


def test_add_comment_echoes_source_line():
    ws = _make_workspace({"foo.py": "import os\nprint('hello')\nreturn 42\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "add-comment",
                   "--file", "foo.py", "--line", "2",
                   "--body", "test", "--author", "vera"])
    assert rc == 0
    assert "foo.py:2: print('hello')" in out.getvalue()

    # Verify comment WAS stored
    from peanut_review.store import read_all_comments
    assert len(read_all_comments(sd)) == 1


def test_add_comment_rejects_line_zero():
    ws = _make_workspace({"foo.py": "line1\nline2\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "add-comment",
                   "--file", "foo.py", "--line", "0",
                   "--body", "test", "--author", "vera"])
    assert rc == 1
    assert "line must be >= 1" in err.getvalue()

    from peanut_review.store import read_all_comments
    assert len(read_all_comments(sd)) == 0


def test_add_comment_from_body_file_preserves_backticks():
    """Bodies with backticks must survive intact when read via --body-file."""
    ws = _make_workspace({"foo.py": "a\nb\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    body_path = os.path.join(tempfile.mkdtemp(prefix="pr-body-"), "body.md")
    body_text = "Verified: (1) `py_compile` clean, (2) `bash -n` clean, (3) $(echo hi) runs"
    Path(body_path).write_text(body_text)

    rc = main(["--session", sd, "add-comment",
               "--file", "foo.py", "--line", "1",
               "--body-file", body_path, "--author", "vera"])
    assert rc == 0

    from peanut_review.store import read_all_comments
    comments = read_all_comments(sd)
    assert len(comments) == 1
    assert comments[0].body == body_text


def test_add_comment_requires_body_or_body_file():
    ws = _make_workspace({"foo.py": "a\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "add-comment",
                   "--file", "foo.py", "--line", "1", "--author", "vera"])
    assert rc == 1
    assert "--body or --body-file is required" in err.getvalue()


def test_add_comment_body_file_missing():
    ws = _make_workspace({"foo.py": "a\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "add-comment",
                   "--file", "foo.py", "--line", "1",
                   "--body-file", "/nonexistent/path.md", "--author", "vera"])
    assert rc == 1
    assert "could not read --body-file" in err.getvalue()


def test_add_global_comment_via_subcommand_persists_with_empty_file():
    """add-global-comment stores file="" line=0 and skips workspace validation."""
    ws = _make_workspace()  # no files needed
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "add-global-comment",
                   "--body", "Tests are missing for the new auth path",
                   "--severity", "warning", "--author", "vera"])
    assert rc == 0
    assert "(global)" in out.getvalue()

    from peanut_review.store import read_all_comments
    comments = read_all_comments(sd)
    assert len(comments) == 1
    assert comments[0].file == ""
    assert comments[0].line == 0
    assert comments[0].severity == "warning"
    assert comments[0].body.startswith("Tests are missing")


def test_add_comment_global_flag_is_equivalent():
    """`add-comment --global` produces the same record as `add-global-comment`."""
    ws = _make_workspace()
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)
    rc = main(["--session", sd, "add-comment", "--global",
               "--body", "scope question", "--author", "vera"])
    assert rc == 0
    from peanut_review.store import read_all_comments
    cs = read_all_comments(sd)
    assert len(cs) == 1 and cs[0].file == "" and cs[0].line == 0


def test_add_comment_global_combined_with_file_rejected():
    ws = _make_workspace({"foo.py": "a\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "add-comment", "--global",
                   "--file", "foo.py", "--line", "1",
                   "--body", "x", "--author", "vera"])
    assert rc == 1
    assert "--global cannot be combined" in err.getvalue()


def test_add_comment_omitting_file_and_line_is_rejected_without_global():
    """Bare `add-comment --body x` is ambiguous; require --global to opt in."""
    ws = _make_workspace()
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "add-comment",
                   "--body", "x", "--author", "vera"])
    # Treated as a global because both --file and --line are absent — this is a
    # convenience: agents typing `add-comment --body ...` shouldn't have to know
    # about the --global flag. The output line includes "(global)".
    assert rc == 0
    assert "(global)" in out.getvalue()


def test_comments_listing_shows_global_marker():
    ws = _make_workspace({"foo.py": "a\nb\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    main(["--session", sd, "add-global-comment",
          "--body", "scope concern", "--severity", "warning", "--author", "vera"])
    main(["--session", sd, "add-comment",
          "--file", "foo.py", "--line", "1",
          "--body", "anchored", "--author", "felix"])

    out = io.StringIO()
    with redirect_stdout(out):
        main(["--session", sd, "comments"])
    text = out.getvalue()
    assert "[global]" in text
    assert "foo.py" in text


def test_add_comment_reply_to_inherits_parent_location():
    """A reply gets the parent's file/line and a reply_to pointer."""
    ws = _make_workspace({"foo.py": "alpha\nbeta\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    main(["--session", sd, "add-comment",
          "--file", "foo.py", "--line", "2", "--body", "needs work",
          "--author", "vera"])
    from peanut_review.store import read_all_comments
    parent_id = read_all_comments(sd)[0].id

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "add-comment",
                   "--reply-to", parent_id, "--body", "agreed",
                   "--author", "felix"])
    assert rc == 0
    assert f"reply to {parent_id}" in out.getvalue()

    cs = read_all_comments(sd)
    reply = [c for c in cs if c.author == "felix"][0]
    assert reply.reply_to == parent_id
    assert reply.file == "foo.py"
    assert reply.line == 2


def test_add_comment_reply_to_unknown_id_errors():
    ws = _make_workspace({"foo.py": "a\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "add-comment",
                   "--reply-to", "c_does_not_exist",
                   "--body", "x", "--author", "vera"])
    assert rc == 1
    assert "not found" in err.getvalue()


def test_add_comment_reply_to_a_reply_collapses_to_root():
    """Trying to reply to a reply silently re-roots to the top-level."""
    ws = _make_workspace({"foo.py": "a\nb\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)
    main(["--session", sd, "add-comment", "--file", "foo.py", "--line", "1",
          "--body", "p", "--author", "vera"])
    from peanut_review.store import read_all_comments
    parent_id = read_all_comments(sd)[0].id
    main(["--session", sd, "add-comment", "--reply-to", parent_id,
          "--body", "r1", "--author", "felix"])
    reply_id = [c.id for c in read_all_comments(sd) if c.author == "felix"][0]
    main(["--session", sd, "add-comment", "--reply-to", reply_id,
          "--body", "r2", "--author", "merlin"])
    third = [c for c in read_all_comments(sd) if c.author == "merlin"][0]
    # Should anchor at the original parent, not at reply_id.
    assert third.reply_to == parent_id


def test_unresolve_subcommand_reopens():
    ws = _make_workspace({"foo.py": "x\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)
    main(["--session", sd, "add-comment", "--file", "foo.py", "--line", "1",
          "--body", "x", "--author", "vera"])
    from peanut_review.store import read_all_comments
    cid = read_all_comments(sd)[0].id
    main(["--session", sd, "resolve", cid, "--by", "jakub"])
    assert read_all_comments(sd)[0].resolved is True

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "unresolve", cid])
    assert rc == 0
    assert "Unresolved" in out.getvalue()
    assert read_all_comments(sd)[0].resolved is False


def test_delete_hides_from_default_list_and_undelete_restores():
    ws = _make_workspace({"foo.py": "line1\nline2\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)

    main(["--session", sd, "add-comment",
          "--file", "foo.py", "--line", "1", "--body", "bad take",
          "--author", "felix"])
    from peanut_review.store import read_all_comments
    cid = read_all_comments(sd)[0].id

    rc = main(["--session", sd, "delete", cid, "--by", "jakub"])
    assert rc == 0

    # Default listing hides the deleted comment
    out = io.StringIO()
    with redirect_stdout(out):
        main(["--session", sd, "comments", "--format", "json"])
    assert json.loads(out.getvalue()) == []

    # --include-deleted surfaces it, with metadata
    out = io.StringIO()
    with redirect_stdout(out):
        main(["--session", sd, "comments", "--format", "json", "--include-deleted"])
    listed = json.loads(out.getvalue())
    assert len(listed) == 1
    assert listed[0]["deleted"] is True
    assert listed[0]["deleted_by"] == "jakub"

    # undelete restores visibility
    rc = main(["--session", sd, "undelete", cid])
    assert rc == 0
    out = io.StringIO()
    with redirect_stdout(out):
        main(["--session", sd, "comments", "--format", "json"])
    assert len(json.loads(out.getvalue())) == 1


def test_delete_unknown_comment_returns_error():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "delete", "c_missing", "--by", "jakub"])
    assert rc == 1
    assert "not found" in err.getvalue()


def test_add_comment_meta_skips_validation():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["--session", sd, "add-comment",
                   "--file", "__meta__", "--line", "0",
                   "--body", "## Test Execution: passed", "--author", "vera"])
    assert rc == 0
    # For __meta__, prints the comment ID
    assert out.getvalue().strip().startswith("c_")


# Issue 3: refresh agent statuses

def test_refresh_agent_statuses_marks_exited_as_done():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, agents=[{"name": "vera", "model": "opus", "persona": "vera.md"}])

    # Set agent as running with a non-existent PID
    s = sess.load_session(sd)
    s.agents[0].status = "running"
    s.agents[0].pid = 999999999  # very unlikely to be a real PID
    sess.save_session(sd, s)

    from peanut_review.session import refresh_agent_statuses as _refresh_agent_statuses
    changed = _refresh_agent_statuses(sd, s)
    assert changed is True
    assert s.agents[0].status == "done"


def test_refresh_agent_statuses_leaves_pending_alone():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, agents=[{"name": "vera", "model": "opus", "persona": "vera.md"}])

    s = sess.load_session(sd)
    assert s.agents[0].status == "pending"

    from peanut_review.session import refresh_agent_statuses as _refresh_agent_statuses
    changed = _refresh_agent_statuses(sd, s)
    assert changed is False
    assert s.agents[0].status == "pending"


def test_edit_command_replaces_body_and_keeps_history():
    ws = _make_workspace({"foo.py": "line1\nline2\n"})
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd, workspace=ws)
    main(["--session", sd, "add-comment",
          "--file", "foo.py", "--line", "1",
          "--body", "first take", "--severity", "nit",
          "--author", "felix"])
    from peanut_review.store import read_all_comments
    cid = read_all_comments(sd)[0].id

    rc = main(["--session", sd, "edit", cid,
               "--body", "second take", "--severity", "warning",
               "--author", "jakub"])
    assert rc == 0

    out = io.StringIO()
    with redirect_stdout(out):
        main(["--session", sd, "comments", "--format", "json"])
    listed = json.loads(out.getvalue())
    assert len(listed) == 1
    assert listed[0]["body"] == "second take"
    assert listed[0]["severity"] == "warning"
    assert listed[0]["edited_by"] == "jakub"
    assert listed[0]["edited_at"]
    assert len(listed[0]["versions"]) == 1
    assert listed[0]["versions"][0]["body"] == "first take"
    assert listed[0]["versions"][0]["severity"] == "nit"


def test_edit_command_requires_body_or_severity():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "edit", "c_anything"])
    assert rc == 1
    assert "at least one of" in err.getvalue()


def test_edit_command_unknown_comment_errors():
    sd = os.path.join(tempfile.mkdtemp(prefix="pr-test-"), "session")
    _init_session(sd)
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["--session", sd, "edit", "c_missing", "--body", "x"])
    assert rc == 1
    assert "not found" in err.getvalue()


def test_session_with_github_field_round_trips():
    s = models.Session(
        id="x", workspace="/tmp/repo", base_ref="main", topic_ref="HEAD",
        original_head="abc", current_head="abc", diff_source="gh-pr",
        github=models.GitHubPR(repo="o/r", number=42, url="https://example.com",
                                head_sha="abc", base_sha="def", title="t"),
    )
    s2 = models.Session.from_json(s.to_json())
    assert s2.diff_source == "gh-pr"
    assert s2.github is not None
    assert s2.github.repo == "o/r"
    assert s2.github.number == 42
