"""GitHub push activity summaries for the session index."""
from __future__ import annotations

from datetime import datetime, timezone

from ..models import Comment, Session
from ..session import META_FILE


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def summarize_push_activity(
    session: Session,
    comments: list[Comment],
) -> dict[str, str | int] | None:
    """Report new local comments since the latest known GitHub push."""
    if session.github is None:
        return None

    if session.last_github_push_at is None:
        # Older sessions may contain locally-pushed comments but predate the
        # watermark. Stay neutral until their next push rather than treating
        # intentionally omitted comments as new or claiming no push occurred.
        has_legacy_local_push = any(
            c.external_id is not None and not c.author.lower().startswith("gh:")
            for c in comments
        )
        if has_legacy_local_push:
            return None
        return {"status": "never", "label": "not pushed yet", "count": 0}

    pushed_at = _timestamp(session.last_github_push_at)
    if pushed_at is None:
        return None
    new_count = sum(
        1
        for c in comments
        if not c.deleted
        and c.file != META_FILE
        and c.external_source is None
        and (created_at := _timestamp(c.timestamp)) is not None
        and created_at > pushed_at
    )
    if not new_count:
        return None
    noun = "comment" if new_count == 1 else "comments"
    return {
        "status": "new",
        "label": f"{new_count} new {noun} since push",
        "count": new_count,
    }
