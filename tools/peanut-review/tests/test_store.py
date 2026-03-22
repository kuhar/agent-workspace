"""Tests for the JSONL comment store."""
import json
import tempfile
from pathlib import Path

from peanut_review.models import Comment
from peanut_review.store import (
    append_comment,
    filter_comments,
    mark_stale,
    read_agent_comments,
    read_all_comments,
    resolve_comment,
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
    append_comment(sd, Comment(author="vera", file="a.py", line=1, body="X", severity="critical", round=1))
    append_comment(sd, Comment(author="vera", file="b.py", line=2, body="Y", severity="nit", round=1))
    append_comment(sd, Comment(author="felix", file="a.py", line=5, body="Z", severity="warning", round=2))

    all_c = read_all_comments(sd)
    assert len(filter_comments(all_c, agent="vera")) == 2
    assert len(filter_comments(all_c, file="a.py")) == 2
    assert len(filter_comments(all_c, severity="critical")) == 1
    assert len(filter_comments(all_c, round_num=2)) == 1


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
