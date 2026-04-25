"""Tests for the JSONL comment store."""
import json
import tempfile
import time
from pathlib import Path

from peanut_review.models import Comment, CommentEdit
from peanut_review.store import (
    append_comment,
    append_edit,
    collect_edits,
    delete_comment,
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


def test_edit_applies_at_read_and_records_version():
    """A CommentEdit folded into the parent updates body/severity and pushes
    the prior values onto Comment.versions in load order."""
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="v1", severity="nit")
    append_comment(sd, c)
    # Tiny sleep so timestamps differ; microsecond precision in _now_iso
    # makes this enough even for a fast machine.
    time.sleep(0.001)
    append_edit(sd, CommentEdit(target_id=c.id, author="jakub",
                                body="v2", severity="warning"))

    [merged] = read_all_comments(sd)
    assert merged.id == c.id
    assert merged.body == "v2"
    assert merged.severity == "warning"
    assert merged.edited_by == "jakub"
    assert merged.edited_at is not None
    assert len(merged.versions) == 1
    assert merged.versions[0]["body"] == "v1"
    assert merged.versions[0]["severity"] == "nit"
    assert merged.versions[0]["edited_at"] is None  # was the original


def test_multiple_edits_stack_in_timestamp_order():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="v1", severity="nit")
    append_comment(sd, c)
    time.sleep(0.001)
    append_edit(sd, CommentEdit(target_id=c.id, author="jakub", body="v2"))
    time.sleep(0.001)
    append_edit(sd, CommentEdit(target_id=c.id, author="merlin",
                                severity="critical"))

    [merged] = read_all_comments(sd)
    assert merged.body == "v2"
    assert merged.severity == "critical"
    assert merged.edited_by == "merlin"
    assert len(merged.versions) == 2
    assert merged.versions[0]["body"] == "v1"
    assert merged.versions[0]["severity"] == "nit"
    assert merged.versions[1]["body"] == "v2"
    assert merged.versions[1]["severity"] == "nit"  # still nit before merlin's edit


def test_edit_by_other_author_writes_to_their_jsonl():
    """When jakub edits vera's comment, the edit lands in jakub.jsonl. Vera's
    own JSONL remains untouched (per-author append-only stays clean)."""
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="orig")
    append_comment(sd, c)
    append_edit(sd, CommentEdit(target_id=c.id, author="jakub", body="rewritten"))

    vera_path = Path(sd) / "comments" / "vera.jsonl"
    jakub_path = Path(sd) / "comments" / "jakub.jsonl"
    assert vera_path.exists()
    assert jakub_path.exists()
    # Vera's file holds only her original comment record.
    vera_lines = vera_path.read_text().splitlines()
    assert len(vera_lines) == 1
    assert json.loads(vera_lines[0])["type"] == "comment"
    # Jakub's file holds only the edit event.
    jakub_lines = jakub_path.read_text().splitlines()
    assert len(jakub_lines) == 1
    assert json.loads(jakub_lines[0])["type"] == "edit"


def test_since_cursor_unaffected_by_edits():
    """`comments --since <id>` is the cursor for new activity. An edit doesn't
    add a new comment id, so it should NOT bubble up as something new."""
    sd = _make_session()
    a = Comment(author="vera", file="a.py", line=1, body="A")
    b = Comment(author="vera", file="a.py", line=2, body="B")
    append_comment(sd, a)
    time.sleep(0.001)
    append_comment(sd, b)
    time.sleep(0.001)
    append_edit(sd, CommentEdit(target_id=a.id, author="jakub", body="A-edited"))

    all_c = read_all_comments(sd)
    assert [c.id for c in all_c] == [a.id, b.id]
    # since=a should still return only b — even though a was edited after b.
    after_a = filter_comments(all_c, since=a.id)
    assert [c.id for c in after_a] == [b.id]


def test_unknown_target_id_edit_skipped_with_warning(caplog):
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="real")
    append_comment(sd, c)
    append_edit(sd, CommentEdit(target_id="c_doesnotexist", author="jakub",
                                body="orphan"))

    with caplog.at_level("WARNING"):
        merged = read_all_comments(sd)
    assert len(merged) == 1
    assert merged[0].body == "real"
    assert merged[0].versions == []
    assert any("c_doesnotexist" in r.message for r in caplog.records)


def test_edit_round_trip_via_jsonl():
    e = CommentEdit(target_id="c_abc", author="jakub", body="new", severity="warning")
    line = e.to_json()
    e2 = CommentEdit.from_json(line)
    assert e2.target_id == "c_abc"
    assert e2.author == "jakub"
    assert e2.body == "new"
    assert e2.severity == "warning"
    # Type discriminator round-trips.
    assert json.loads(line)["type"] == "edit"


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


def test_collect_edits_returns_only_edits():
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="v1")
    append_comment(sd, c)
    append_edit(sd, CommentEdit(target_id=c.id, author="jakub", body="v2"))

    edits = collect_edits(sd, target_id=c.id)
    assert len(edits) == 1
    assert edits[0].target_id == c.id
    assert edits[0].author == "jakub"
    assert edits[0].body == "v2"

    # Filter out non-matching target.
    assert collect_edits(sd, target_id="c_other") == []


def test_derived_fields_not_persisted_to_disk():
    """to_json must not emit edited_at/edited_by/versions — they are folded
    in fresh on each read from the edit log. If they leak to disk, an old
    edit log + new on-disk fields would double-apply."""
    c = Comment(author="vera", file="a.py", line=1, body="x")
    c.edited_at = "2026-04-25T12:00:00.000000+00:00"
    c.edited_by = "jakub"
    c.versions = [{"body": "old", "severity": "nit"}]
    raw = json.loads(c.to_json())
    assert "edited_at" not in raw
    assert "edited_by" not in raw
    assert "versions" not in raw


def test_resolve_skips_edit_records():
    """resolve_comment iterates polymorphic JSONL — must not match a CommentEdit
    with the same id-shape, must not crash on missing `resolved` attr."""
    sd = _make_session()
    c = Comment(author="vera", file="a.py", line=1, body="x")
    append_comment(sd, c)
    append_edit(sd, CommentEdit(target_id=c.id, author="jakub", body="y"))
    assert resolve_comment(sd, c.id, resolved_by="jakub")
    [stored] = read_all_comments(sd)
    assert stored.resolved is True
    assert stored.body == "y"  # edit still applied at read time


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
