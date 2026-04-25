"""Tests for the JSONL comment store."""
import json
import tempfile
from pathlib import Path

from peanut_review.models import Comment
from peanut_review.store import (
    append_comment,
    delete_comment,
    edit_comment,
    filter_comments,
    mark_stale,
    read_agent_comments,
    read_all_comments,
    resolve_comment,
    undelete_comment,
    update_comment_external,
)


def _make_session() -> str:
    d = tempfile.mkdtemp(prefix="pr-test-")
    (Path(d) / "comments").mkdir()
    return d


def test_append_and_read():
    sd = _make_session()
    c = Comment(author="vera", file="src/foo.cpp", line=42, body="Null check needed", severity="critical")
    append_comment(sd, c)

    comments = read_agent_comments(sd, "vera")
    assert len(comments) == 1
    assert comments[0].id == c.id
    assert comments[0].file == "src/foo.cpp"
    assert comments[0].severity == "critical"


def test_multiple_agents():
    sd = _make_session()
    append_comment(sd, Comment(author="vera", file="a.py", line=1, body="A"))
    append_comment(sd, Comment(author="felix", file="b.py", line=2, body="B"))
    append_comment(sd, Comment(author="vera", file="c.py", line=3, body="C"))

    assert len(read_agent_comments(sd, "vera")) == 2
    assert len(read_agent_comments(sd, "felix")) == 1

    all_c = read_all_comments(sd)
    assert len(all_c) == 3


def test_filter_comments():
    sd = _make_session()
    append_comment(sd, Comment(author="vera", file="a.py", line=1, body="X", severity="critical"))
    append_comment(sd, Comment(author="vera", file="b.py", line=2, body="Y", severity="nit"))
    append_comment(sd, Comment(author="felix", file="a.py", line=5, body="Z", severity="warning"))

    all_c = read_all_comments(sd)
    assert len(filter_comments(all_c, agent="vera")) == 2
    assert len(filter_comments(all_c, file="a.py")) == 2
    assert len(filter_comments(all_c, severity="critical")) == 1


def test_filter_comments_since_id_returns_only_newer():
    """`--since <id>` is the cursor for "what's new" polling; replaces the
    old `--round N` filter. Same-second timestamps are handled by
    position-in-sorted-list, not raw timestamp comparison."""
    sd = _make_session()
    a = Comment(author="vera", file="a.py", line=1, body="A", severity="nit")
    b = Comment(author="felix", file="a.py", line=2, body="B", severity="nit")
    c = Comment(author="vera", file="a.py", line=3, body="C", severity="nit")
    append_comment(sd, a)
    append_comment(sd, b)
    append_comment(sd, c)

    all_c = read_all_comments(sd)
    # since=a → b, c
    after_a = filter_comments(all_c, since=a.id)
    assert [x.body for x in after_a] == ["B", "C"]
    # since=c → empty (c is the most recent)
    assert filter_comments(all_c, since=c.id) == []
    # unknown id → return everything (caller's problem to validate)
    assert len(filter_comments(all_c, since="c_doesnotexist")) == 3


def test_resolve_comment():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="Fix this")
    append_comment(sd, c)

    assert resolve_comment(sd, c.id, resolved_by="jakub")

    comments = read_agent_comments(sd, "vera")
    assert comments[0].resolved is True
    assert comments[0].resolved_by == "jakub"
    assert comments[0].resolved_at is not None


def test_resolve_nonexistent():
    sd = _make_session()
    assert resolve_comment(sd, "c_nonexist") is False


def test_filter_unresolved():
    sd = _make_session()
    c1 = Comment(author="vera", file="a.py", line=1, body="A")
    c2 = Comment(author="vera", file="b.py", line=2, body="B")
    append_comment(sd, c1)
    append_comment(sd, c2)
    resolve_comment(sd, c1.id)

    all_c = read_all_comments(sd)
    unresolved = filter_comments(all_c, unresolved=True)
    assert len(unresolved) == 1
    assert unresolved[0].id == c2.id


def test_mark_stale():
    sd = _make_session()
    c1 = Comment(author="vera", file="a.py", line=1, body="A")
    c2 = Comment(author="vera", file="b.py", line=2, body="B")
    append_comment(sd, c1)
    append_comment(sd, c2)
    resolve_comment(sd, c1.id)

    count = mark_stale(sd)
    assert count == 1  # Only unresolved c2 marked stale

    comments = read_agent_comments(sd, "vera")
    resolved = [c for c in comments if c.id == c1.id][0]
    unresolved = [c for c in comments if c.id == c2.id][0]
    assert resolved.stale is False  # Resolved comments not marked
    assert unresolved.stale is True


def test_corrupt_line_recovery():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="Good")
    append_comment(sd, c)

    # Append corrupt data
    path = Path(sd) / "comments" / "vera.jsonl"
    with open(path, "a") as f:
        f.write("this is not json\n")
        f.write('{"partial": true\n')  # Incomplete JSON

    comments = read_agent_comments(sd, "vera")
    assert len(comments) == 1
    assert comments[0].body == "Good"


def test_comment_round_trip_json():
    c = Comment(
        author="merlin", file="ir.mlir", line=10,
        body="Check op semantics", severity="warning",
        end_line=15, head_sha="abc123",
    )
    line = c.to_json()
    c2 = Comment.from_json(line)
    assert c2.author == "merlin"
    assert c2.end_line == 15
    assert c2.head_sha == "abc123"


def test_empty_agent_file():
    sd = _make_session()
    assert read_agent_comments(sd, "nobody") == []


def test_delete_marks_and_sets_metadata():
    sd = _make_session()
    c = Comment(author="felix", file="a.py", line=1, body="bad take", severity="nit")
    append_comment(sd, c)
    assert delete_comment(sd, c.id, deleted_by="jakub") is True

    stored = read_agent_comments(sd, "felix")[0]
    assert stored.deleted is True
    assert stored.deleted_by == "jakub"
    assert stored.deleted_at is not None


def test_delete_missing_comment_returns_false():
    sd = _make_session()
    append_comment(sd, Comment(author="felix", file="a.py", line=1, body="x"))
    assert delete_comment(sd, "c_does_not_exist") is False


def test_delete_is_idempotent_preserving_original_metadata():
    sd = _make_session()
    c = Comment(author="felix", file="a.py", line=1, body="x")
    append_comment(sd, c)
    delete_comment(sd, c.id, deleted_by="first")
    first = read_agent_comments(sd, "felix")[0]
    delete_comment(sd, c.id, deleted_by="second")
    second = read_agent_comments(sd, "felix")[0]
    assert second.deleted_by == first.deleted_by == "first"
    assert second.deleted_at == first.deleted_at


def test_undelete_clears_flags():
    sd = _make_session()
    c = Comment(author="felix", file="a.py", line=1, body="x")
    append_comment(sd, c)
    delete_comment(sd, c.id, deleted_by="jakub")
    assert undelete_comment(sd, c.id) is True
    stored = read_agent_comments(sd, "felix")[0]
    assert stored.deleted is False
    assert stored.deleted_by is None
    assert stored.deleted_at is None


def test_filter_comments_hides_deleted_by_default():
    live = Comment(author="felix", file="a.py", line=1, body="live")
    gone = Comment(author="felix", file="a.py", line=2, body="gone", deleted=True)
    assert filter_comments([live, gone]) == [live]
    # include_deleted=True brings them back
    assert filter_comments([live, gone], include_deleted=True) == [live, gone]


def test_unresolve_clears_resolved_metadata():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="x")
    append_comment(sd, c)
    assert resolve_comment(sd, c.id, resolved_by="jakub")

    from peanut_review.store import unresolve_comment
    assert unresolve_comment(sd, c.id) is True
    stored = read_agent_comments(sd, "vera")[0]
    assert stored.resolved is False
    assert stored.resolved_by is None
    assert stored.resolved_at is None


def test_unresolve_missing_returns_false():
    sd = _make_session()
    from peanut_review.store import unresolve_comment
    assert unresolve_comment(sd, "c_does_not_exist") is False


def test_reply_to_round_trips_in_store():
    sd = _make_session()
    parent = Comment(author="vera", file="a.py", line=1, body="parent")
    append_comment(sd, parent)
    reply = Comment(author="felix", file="a.py", line=1, body="reply",
                    reply_to=parent.id)
    append_comment(sd, reply)

    cs = read_agent_comments(sd, "felix")
    assert cs[0].reply_to == parent.id


def test_normalize_reply_to_re_roots_replies():
    """Replying to a reply collapses to the same parent — flat threads only."""
    from peanut_review.store import normalize_reply_to
    parent = Comment(id="c_aaaa", author="x", file="a.py", line=1, body="p")
    reply = Comment(id="c_bbbb", author="y", file="a.py", line=1, body="r",
                    reply_to="c_aaaa")
    cs = [parent, reply]
    # Targeting the reply collapses to the parent's id.
    assert normalize_reply_to(cs, "c_bbbb") == "c_aaaa"
    # Targeting the parent stays the parent.
    assert normalize_reply_to(cs, "c_aaaa") == "c_aaaa"
    # Unknown id returns None.
    assert normalize_reply_to(cs, "c_zzzz") is None


def test_thread_for_returns_parent_then_replies_in_time_order():
    from peanut_review.store import thread_for
    parent = Comment(id="c_p", author="x", file="a.py", line=1, body="p",
                     timestamp="2026-04-01T00:00:00+00:00")
    r1 = Comment(id="c_r1", author="y", file="a.py", line=1, body="r1",
                 reply_to="c_p", timestamp="2026-04-02T00:00:00+00:00")
    r2 = Comment(id="c_r2", author="z", file="a.py", line=1, body="r2",
                 reply_to="c_p", timestamp="2026-04-03T00:00:00+00:00")
    # Out of order on purpose.
    out = thread_for([r2, parent, r1], "c_p")
    assert [c.id for c in out] == ["c_p", "c_r1", "c_r2"]


def test_global_comment_stores_with_empty_file_and_zero_line():
    """High-level / global comments use file="" and line=0 as the sentinel."""
    sd = _make_session()
    g = Comment(author="vera", file="", line=0, body="scope concern",
                severity="warning")
    append_comment(sd, g)
    cs = read_agent_comments(sd, "vera")
    assert len(cs) == 1
    assert cs[0].file == ""
    assert cs[0].line == 0


def test_edit_rewrites_body_and_records_prior_version():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="v1", severity="nit")
    append_comment(sd, c)

    assert edit_comment(sd, c.id, body="v2", severity="warning",
                        edited_by="jakub")

    [stored] = read_all_comments(sd)
    assert stored.id == c.id
    assert stored.body == "v2"
    assert stored.severity == "warning"
    assert stored.edited_by == "jakub"
    assert stored.edited_at is not None
    assert len(stored.versions) == 1
    assert stored.versions[0]["body"] == "v1"
    assert stored.versions[0]["severity"] == "nit"
    assert stored.versions[0]["edited_by"] is None  # original


def test_multiple_edits_stack_versions_in_order():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="v1", severity="nit")
    append_comment(sd, c)
    edit_comment(sd, c.id, body="v2", edited_by="jakub")
    edit_comment(sd, c.id, severity="critical", edited_by="merlin")

    [stored] = read_all_comments(sd)
    assert stored.body == "v2"
    assert stored.severity == "critical"
    assert stored.edited_by == "merlin"
    assert len(stored.versions) == 2
    # versions[0] = original (body=v1, severity=nit, no editor).
    assert stored.versions[0]["body"] == "v1"
    assert stored.versions[0]["severity"] == "nit"
    assert stored.versions[0]["edited_by"] is None
    # versions[1] = state after jakub's body edit (severity still nit).
    assert stored.versions[1]["body"] == "v2"
    assert stored.versions[1]["severity"] == "nit"
    assert stored.versions[1]["edited_by"] == "jakub"


def test_edit_only_severity_keeps_body():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="hi", severity="nit")
    append_comment(sd, c)
    edit_comment(sd, c.id, severity="warning", edited_by="irene")

    [stored] = read_all_comments(sd)
    assert stored.body == "hi"  # unchanged
    assert stored.severity == "warning"
    assert stored.edited_by == "irene"


def test_edit_unknown_id_returns_false():
    sd = _make_session()
    assert edit_comment(sd, "c_missing", body="x", edited_by="jakub") is False


def test_edit_rejects_no_change():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="hi")
    append_comment(sd, c)
    import pytest
    with pytest.raises(ValueError):
        edit_comment(sd, c.id, edited_by="jakub")


def test_since_cursor_unaffected_by_edits():
    """`comments --since <id>` cursors on comment-creation order. An edit
    doesn't change the comment id or timestamp, so the cursor stays put."""
    sd = _make_session()
    a = Comment(author="vera", file="a.py", line=1, body="A")
    b = Comment(author="vera", file="a.py", line=2, body="B")
    append_comment(sd, a)
    append_comment(sd, b)
    edit_comment(sd, a.id, body="A-edited", edited_by="jakub")

    all_c = read_all_comments(sd)
    assert [c.id for c in all_c] == [a.id, b.id]
    after_a = filter_comments(all_c, since=a.id)
    assert [c.id for c in after_a] == [b.id]


def test_external_id_round_trip_via_jsonl():
    c = Comment(
        author="gh:octocat", file="a.py", line=1, body="from github",
        external_source="github", external_id="2147483647",
        external_url="https://github.com/o/r/pull/1#discussion_r2147483647",
        external_synced_body="from github",
    )
    line = c.to_json()
    c2 = Comment.from_json(line)
    assert c2.external_source == "github"
    assert c2.external_id == "2147483647"
    assert c2.external_url.endswith("r2147483647")
    assert c2.external_synced_body == "from github"


def test_update_comment_external_stamps_metadata():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="x")
    append_comment(sd, c)
    assert update_comment_external(
        sd, c.id,
        external_source="github",
        external_id="42",
        external_url="https://github.com/o/r/pull/1#discussion_r42",
        external_synced_body="x",
    )
    [stored] = read_agent_comments(sd, "vera")
    assert stored.external_source == "github"
    assert stored.external_id == "42"
    assert stored.external_synced_body == "x"


def test_versions_round_trip_via_jsonl():
    """versions/edited_at/edited_by are now persisted; round-trip check."""
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="v1")
    append_comment(sd, c)
    edit_comment(sd, c.id, body="v2", edited_by="jakub")
    [stored] = read_all_comments(sd)
    # Re-read after a write cycle: versions still match.
    [stored2] = read_all_comments(sd)
    assert stored.versions == stored2.versions
    assert stored.edited_at == stored2.edited_at
    assert stored.body == "v2"


def test_resolve_after_edit_keeps_edit_state():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="v1")
    append_comment(sd, c)
    edit_comment(sd, c.id, body="v2", edited_by="jakub")
    assert resolve_comment(sd, c.id, resolved_by="jakub")
    [stored] = read_all_comments(sd)
    assert stored.resolved is True
    assert stored.body == "v2"
    assert stored.edited_by == "jakub"
    assert len(stored.versions) == 1


def test_mark_stale_skips_deleted():
    sd = _make_session()
    keep = Comment(author="felix", file="a.py", line=1, body="keep")
    tomb = Comment(author="felix", file="a.py", line=2, body="tomb", deleted=True)
    append_comment(sd, keep)
    append_comment(sd, tomb)
    n = mark_stale(sd)
    assert n == 1  # only the live one was marked
    all_cs = {c.id: c for c in read_agent_comments(sd, "felix")}
    assert all_cs[keep.id].stale is True
    assert all_cs[tomb.id].stale is False
