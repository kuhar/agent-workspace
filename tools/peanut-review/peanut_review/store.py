"""JSONL comment store — append, read, filter, resolve."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .models import Comment, CommentEdit, _now_iso

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


def append_edit(session_dir: str | Path, edit: CommentEdit) -> CommentEdit:
    """Append a CommentEdit to the editor's JSONL file. Returns the edit.

    The edit lives under the *editor's* author file, not the original
    comment's author file, so each contributor's append-only log stays its
    own (matters when one user edits another's comment).
    """
    path = _agent_file(session_dir, edit.author)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = edit.to_json() + "\n"
    if len(line.encode()) > _PIPE_BUF:
        log.warning("Edit %s exceeds PIPE_BUF — write may not be atomic", edit.id)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    return edit


def read_agent_comments(session_dir: str | Path, agent: str) -> list[Comment]:
    """Read comments authored by `agent` (no edit application).

    Edits live in the *editor's* file, not the comment author's, so this
    function alone won't reflect cross-author edits. Use it for per-author
    bookkeeping (resolve_comment etc.). For the user-facing view, go through
    `read_all_comments`.
    """
    path = _agent_file(session_dir, agent)
    if not path.exists():
        return []
    records = _read_jsonl(path)
    return [r for r in records if isinstance(r, Comment)]


def read_all_comments(session_dir: str | Path) -> list[Comment]:
    """Read and merge comments from all agents, sorted by timestamp.

    Folds CommentEdit events into the matching Comments: latest body/severity
    is applied, prior values are preserved on the in-memory `versions` list.
    """
    cdir = _comments_dir(session_dir)
    records: list[Comment | CommentEdit] = []
    for f in sorted(cdir.glob("*.jsonl")):
        records.extend(_read_jsonl(f))
    records.sort(key=lambda r: r.timestamp)

    by_id: dict[str, Comment] = {}
    for r in records:
        if isinstance(r, Comment):
            by_id[r.id] = r
    for r in records:
        if isinstance(r, CommentEdit):
            target = by_id.get(r.target_id)
            if target is None:
                log.warning("Edit %s targets unknown comment %s — skipping",
                            r.id, r.target_id)
                continue
            target.versions.append({
                "body": target.body,
                "severity": target.severity,
                "edited_at": target.edited_at,
                "edited_by": target.edited_by,
            })
            if r.body is not None:
                target.body = r.body
            if r.severity is not None:
                target.severity = r.severity
            target.edited_at = r.timestamp
            target.edited_by = r.author

    comments = [r for r in records if isinstance(r, Comment)]
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


def resolve_comment(
    session_dir: str | Path, comment_id: str, resolved_by: str | None = None
) -> bool:
    """Mark a comment as resolved by rewriting the agent's JSONL file.

    Returns True if the comment was found and resolved.
    """
    cdir = _comments_dir(session_dir)
    for f in cdir.glob("*.jsonl"):
        records = _read_jsonl(f)
        found = False
        for r in records:
            if isinstance(r, Comment) and r.id == comment_id:
                r.resolved = True
                r.resolved_by = resolved_by
                r.resolved_at = _now_iso()
                found = True
                break
        if found:
            _write_jsonl(f, records)
            return True
    return False


def unresolve_comment(session_dir: str | Path, comment_id: str) -> bool:
    """Clear the resolved flag on a comment. Returns True if found.

    Mirrors `undelete_comment` for the resolve dimension.
    """
    cdir = _comments_dir(session_dir)
    for f in cdir.glob("*.jsonl"):
        records = _read_jsonl(f)
        found = False
        for r in records:
            if isinstance(r, Comment) and r.id == comment_id:
                r.resolved = False
                r.resolved_by = None
                r.resolved_at = None
                found = True
                break
        if found:
            _write_jsonl(f, records)
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
        records = _read_jsonl(f)
        found = False
        for r in records:
            if isinstance(r, Comment) and r.id == comment_id:
                if not r.deleted:
                    r.deleted = True
                    r.deleted_by = deleted_by
                    r.deleted_at = _now_iso()
                found = True
                break
        if found:
            _write_jsonl(f, records)
            return True
    return False


def undelete_comment(session_dir: str | Path, comment_id: str) -> bool:
    """Clear the soft-delete flags on a comment. Returns True if found."""
    cdir = _comments_dir(session_dir)
    for f in cdir.glob("*.jsonl"):
        records = _read_jsonl(f)
        found = False
        for r in records:
            if isinstance(r, Comment) and r.id == comment_id:
                r.deleted = False
                r.deleted_by = None
                r.deleted_at = None
                found = True
                break
        if found:
            _write_jsonl(f, records)
            return True
    return False


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
        records = _read_jsonl(f)
        changed = False
        for r in records:
            if not isinstance(r, Comment):
                continue
            if not r.resolved and not r.stale and not r.deleted:
                r.stale = True
                changed = True
                count += 1
        if changed:
            _write_jsonl(f, records)
    return count


def collect_edits(session_dir: str | Path,
                  target_id: str | None = None,
                  ) -> list[CommentEdit]:
    """Read all CommentEdit records, optionally filtered by target.

    Used by `comments --show-edits` and the web UI's history popover when
    they want the raw edit log rather than the folded `Comment.versions`.
    """
    cdir = _comments_dir(session_dir)
    out: list[CommentEdit] = []
    for f in sorted(cdir.glob("*.jsonl")):
        for r in _read_jsonl(f):
            if not isinstance(r, CommentEdit):
                continue
            if target_id and r.target_id != target_id:
                continue
            out.append(r)
    out.sort(key=lambda e: e.timestamp)
    return out


def _read_jsonl(path: Path) -> list[Comment | CommentEdit]:
    """Read a JSONL file, dispatching on the `type` discriminator.

    Lines without a `type` field default to "comment" (matches the on-disk
    format from before edit support). Unparseable lines are skipped with a
    warning so a single corrupt entry doesn't break the whole session.
    """
    out: list[Comment | CommentEdit] = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("Skipping corrupt line %d in %s: %s", lineno, path, e)
                continue
            kind = d.get("type", "comment")
            try:
                if kind == "edit":
                    out.append(CommentEdit.from_json(line))
                else:
                    out.append(Comment.from_json(line))
            except TypeError as e:
                log.warning("Skipping malformed line %d in %s: %s", lineno, path, e)
    return out


def _write_jsonl(path: Path, records: list[Comment | CommentEdit]) -> None:
    """Rewrite a JSONL file atomically."""
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(r.to_json() + "\n")
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
    """Stamp external-provider metadata on a comment, in place.

    Used by the gh push/pull layer to record round-trip info. Rewrites the
    comment's own JSONL file (the comment author's, not the caller's). Only
    fields passed as non-None are updated — pass `external_synced_body=""`
    to record an empty body explicitly.
    """
    cdir = _comments_dir(session_dir)
    for f in cdir.glob("*.jsonl"):
        records = _read_jsonl(f)
        found = False
        for r in records:
            if isinstance(r, Comment) and r.id == comment_id:
                if external_source is not None:
                    r.external_source = external_source
                if external_id is not None:
                    r.external_id = external_id
                if external_url is not None:
                    r.external_url = external_url
                if external_in_reply_to is not None:
                    r.external_in_reply_to = external_in_reply_to
                if external_synced_body is not None:
                    r.external_synced_body = external_synced_body
                found = True
                break
        if found:
            _write_jsonl(f, records)
            return True
    return False
