"""JSONL comment store — append, read, filter, resolve."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .models import Comment, _now_iso

log = logging.getLogger(__name__)

# O_APPEND writes are not strictly atomic on regular files (unlike pipes),
# but since each agent writes to its own .jsonl file, concurrent interleaving
# is not a concern. We warn on large lines as a safety check.
_PIPE_BUF = 4096


def _comments_dir(session_dir: str | Path) -> Path:
    return Path(session_dir) / "comments"


def _agent_file(session_dir: str | Path, agent: str) -> Path:
    return _comments_dir(session_dir) / f"{agent}.jsonl"


def append_comment(session_dir: str | Path, comment: Comment) -> Comment:
    """Append a comment to the agent's JSONL file. Returns the comment."""
    path = _agent_file(session_dir, comment.author)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = comment.to_json() + "\n"
    if len(line.encode()) > _PIPE_BUF:
        log.warning("Comment %s exceeds PIPE_BUF — write may not be atomic", comment.id)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return comment


def read_agent_comments(session_dir: str | Path, agent: str) -> list[Comment]:
    """Read all comments from one agent's JSONL file."""
    path = _agent_file(session_dir, agent)
    if not path.exists():
        return []
    return _read_jsonl(path)


def read_all_comments(session_dir: str | Path) -> list[Comment]:
    """Read and merge comments from all agents, sorted by timestamp."""
    cdir = _comments_dir(session_dir)
    comments: list[Comment] = []
    for f in sorted(cdir.glob("*.jsonl")):
        comments.extend(_read_jsonl(f))
    comments.sort(key=lambda c: c.timestamp)
    return comments


def filter_comments(
    comments: list[Comment],
    *,
    agent: str | None = None,
    file: str | None = None,
    severity: str | None = None,
    round_num: int | None = None,
    unresolved: bool = False,
    include_deleted: bool = False,
) -> list[Comment]:
    """Filter a list of comments by criteria.

    Deleted comments are hidden by default — set `include_deleted=True` to
    see them (auditing, undelete). Agents reading back comments must NOT pass
    this flag, so humans can hide bad comments before round 2.
    """
    result = comments
    if not include_deleted:
        result = [c for c in result if not c.deleted]
    if agent:
        result = [c for c in result if c.author == agent]
    if file:
        result = [c for c in result if c.file == file]
    if severity:
        result = [c for c in result if c.severity == severity]
    if round_num is not None:
        result = [c for c in result if c.round == round_num]
    if unresolved:
        result = [c for c in result if not c.resolved]
    return result


def resolve_comment(
    session_dir: str | Path, comment_id: str, resolved_by: str | None = None
) -> bool:
    """Mark a comment as resolved by rewriting the agent's JSONL file.

    Returns True if the comment was found and resolved.
    """
    cdir = _comments_dir(session_dir)
    for f in cdir.glob("*.jsonl"):
        comments = _read_jsonl(f)
        found = False
        for c in comments:
            if c.id == comment_id:
                c.resolved = True
                c.resolved_by = resolved_by
                c.resolved_at = _now_iso()
                found = True
                break
        if found:
            _write_jsonl(f, comments)
            return True
    return False


def delete_comment(
    session_dir: str | Path, comment_id: str, deleted_by: str | None = None,
) -> bool:
    """Soft-delete a comment (hide from default views). Returns True if found.

    Idempotent: re-deleting keeps the original deleted_at/by.
    """
    cdir = _comments_dir(session_dir)
    for f in cdir.glob("*.jsonl"):
        comments = _read_jsonl(f)
        found = False
        for c in comments:
            if c.id == comment_id:
                if not c.deleted:
                    c.deleted = True
                    c.deleted_by = deleted_by
                    c.deleted_at = _now_iso()
                found = True
                break
        if found:
            _write_jsonl(f, comments)
            return True
    return False


def undelete_comment(session_dir: str | Path, comment_id: str) -> bool:
    """Clear the soft-delete flags on a comment. Returns True if found."""
    cdir = _comments_dir(session_dir)
    for f in cdir.glob("*.jsonl"):
        comments = _read_jsonl(f)
        found = False
        for c in comments:
            if c.id == comment_id:
                c.deleted = False
                c.deleted_by = None
                c.deleted_at = None
                found = True
                break
        if found:
            _write_jsonl(f, comments)
            return True
    return False


def mark_stale(session_dir: str | Path) -> int:
    """Mark all unresolved comments as stale. Returns count marked."""
    cdir = _comments_dir(session_dir)
    count = 0
    for f in cdir.glob("*.jsonl"):
        comments = _read_jsonl(f)
        changed = False
        for c in comments:
            if not c.resolved and not c.stale and not c.deleted:
                c.stale = True
                changed = True
                count += 1
        if changed:
            _write_jsonl(f, comments)
    return count


def _read_jsonl(path: Path) -> list[Comment]:
    """Read a JSONL file, skipping unparseable lines."""
    comments = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                comments.append(Comment.from_json(line))
            except (json.JSONDecodeError, TypeError) as e:
                log.warning("Skipping corrupt line %d in %s: %s", lineno, path, e)
    return comments


def _write_jsonl(path: Path, comments: list[Comment]) -> None:
    """Rewrite a JSONL file atomically."""
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w") as f:
        for c in comments:
            f.write(c.to_json() + "\n")
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
