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
    since: str | None = None,
    unresolved: bool = False,
    include_deleted: bool = False,
) -> list[Comment]:
    """Filter a list of comments by criteria.

    `since` is a comment id; only comments whose timestamp is strictly after
    that comment's timestamp are returned. Used by the orchestrator (or any
    returning reviewer) to poll for new activity since they last looked. If
    the id isn't found, no rows are filtered out — callers that want strict
    behavior should validate the id first.

    Deleted comments are hidden by default — set `include_deleted=True` to
    see them (auditing, undelete). Agents reading back comments must NOT pass
    this flag, so humans can hide bad comments between rounds.
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
    if since:
        # Use position in the original sorted list rather than a timestamp
        # comparison so same-second ties are handled deterministically.
        ids = [c.id for c in comments]
        try:
            cutoff_idx = ids.index(since)
        except ValueError:
            cutoff_idx = -1  # id not found → return everything
        kept_ids = {c.id for c in comments[cutoff_idx + 1:]}
        result = [c for c in result if c.id in kept_ids]
    if unresolved:
        result = [c for c in result if not c.resolved]
    return result


def _mutate_comment(
    session_dir: str | Path, comment_id: str, mutator,
) -> bool:
    """Find a comment by id across all per-author JSONL files, run `mutator(c)`,
    and atomically rewrite that file. Returns True if the comment was found.

    Used by every per-comment mutation (resolve, delete, edit, …) so they all
    share the same locate + rewrite contract.
    """
    cdir = _comments_dir(session_dir)
    for f in cdir.glob("*.jsonl"):
        comments = _read_jsonl(f)
        for c in comments:
            if c.id == comment_id:
                mutator(c)
                _write_jsonl(f, comments)
                return True
    return False


def resolve_comment(
    session_dir: str | Path, comment_id: str, resolved_by: str | None = None
) -> bool:
    """Mark a comment as resolved. Returns True if found."""
    def _apply(c: Comment) -> None:
        c.resolved = True
        c.resolved_by = resolved_by
        c.resolved_at = _now_iso()
    return _mutate_comment(session_dir, comment_id, _apply)


def unresolve_comment(session_dir: str | Path, comment_id: str) -> bool:
    """Clear the resolved flag on a comment. Returns True if found."""
    def _apply(c: Comment) -> None:
        c.resolved = False
        c.resolved_by = None
        c.resolved_at = None
    return _mutate_comment(session_dir, comment_id, _apply)


def delete_comment(
    session_dir: str | Path, comment_id: str, deleted_by: str | None = None,
) -> bool:
    """Soft-delete a comment. Idempotent: re-deleting keeps original metadata."""
    def _apply(c: Comment) -> None:
        if not c.deleted:
            c.deleted = True
            c.deleted_by = deleted_by
            c.deleted_at = _now_iso()
    return _mutate_comment(session_dir, comment_id, _apply)


def undelete_comment(session_dir: str | Path, comment_id: str) -> bool:
    """Clear the soft-delete flags on a comment. Returns True if found."""
    def _apply(c: Comment) -> None:
        c.deleted = False
        c.deleted_by = None
        c.deleted_at = None
    return _mutate_comment(session_dir, comment_id, _apply)


def edit_comment(
    session_dir: str | Path, comment_id: str, *,
    body: str | None = None,
    severity: str | None = None,
    edited_by: str,
) -> bool:
    """Rewrite a comment's body and/or severity, snapshotting prior state.

    `versions[0]` is always the original creator's state (edited_at/by null).
    Each subsequent call appends the *prior* state, then bumps body/severity
    /edited_at/edited_by to the new values. Caller must pass at least one of
    body or severity. Returns True if the comment was found.
    """
    if body is None and severity is None:
        raise ValueError("edit_comment requires body or severity")
    def _apply(c: Comment) -> None:
        c.versions.append({
            "body": c.body,
            "severity": c.severity,
            "edited_at": c.edited_at,
            "edited_by": c.edited_by,
        })
        if body is not None:
            c.body = body
        if severity is not None:
            c.severity = severity
        c.edited_at = _now_iso()
        c.edited_by = edited_by
    return _mutate_comment(session_dir, comment_id, _apply)


def normalize_reply_to(comments: list[Comment], reply_to: str) -> str | None:
    """Resolve a `reply_to` to a *top-level* comment id.

    If the target itself is a reply, re-roots to its parent so threads stay
    flat (GitHub-style: no nesting beyond depth 1). Returns None if the
    target id doesn't exist among `comments`.
    """
    by_id = {c.id: c for c in comments}
    target = by_id.get(reply_to)
    if target is None:
        return None
    if target.reply_to and target.reply_to in by_id:
        return target.reply_to
    return target.id


def thread_for(comments: list[Comment], parent_id: str) -> list[Comment]:
    """Return the thread rooted at `parent_id`: parent first, then replies in
    timestamp order. Excludes deleted entries.
    """
    parent = next((c for c in comments if c.id == parent_id and not c.deleted), None)
    if parent is None:
        return []
    replies = [c for c in comments if c.reply_to == parent_id and not c.deleted]
    replies.sort(key=lambda c: c.timestamp)
    return [parent, *replies]


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
    """Read a JSONL file, skipping unparseable lines with a warning."""
    comments: list[Comment] = []
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


def update_comment_external(
    session_dir: str | Path,
    comment_id: str,
    *,
    external_source: str | None = None,
    external_id: str | None = None,
    external_url: str | None = None,
    external_in_reply_to: str | None = None,
    external_synced_body: str | None = None,
) -> bool:
    """Stamp external-provider metadata on a comment, in place. Returns True
    if the comment was found. Only non-None args are applied.
    """
    def _apply(c: Comment) -> None:
        if external_source is not None:
            c.external_source = external_source
        if external_id is not None:
            c.external_id = external_id
        if external_url is not None:
            c.external_url = external_url
        if external_in_reply_to is not None:
            c.external_in_reply_to = external_in_reply_to
        if external_synced_body is not None:
            c.external_synced_body = external_synced_body
    return _mutate_comment(session_dir, comment_id, _apply)
