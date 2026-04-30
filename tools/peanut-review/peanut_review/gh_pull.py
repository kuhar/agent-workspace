"""Pull GitHub PR comments into a local session. Shared between the CLI
(`gh-pull`) and the web UI's `/api/gh/pull` endpoint.

The pull is keyed on provider source + `external_id`:
  1. New comments (no local match) are appended as `gh:<login>` authors.
     Replies land threaded — `in_reply_to_id` is resolved to the matching
     local comment and stored as `reply_to`, normalized via
     `store.normalize_reply_to`.
  2. Non-empty PR review summaries are appended as high-level comments.
  3. Existing comments whose GitHub body diverges from our last
     `external_synced_body` get an `edit_comment` applied so the change shows
     up in version history.
  4. Existing comments with stale local import timestamps are retimestamped to
     the original GitHub timestamp.
  5. Already-synced comments (matching id, body, and timestamp) are skipped.

Idempotent: re-running with no upstream changes is a no-op.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import gh, models, session as sess, store


# Match a "nit" prefix that humans actually use on GitHub:
#   "nit: foo", "Nit - foo", "(nit) foo", "[nit] foo", "nit, foo".
# We scan only the first two lines so a body that just mentions the word
# "nit" in passing doesn't get reclassified.
_NIT_PREFIX_RE = re.compile(
    r"(?:^|[^A-Za-z0-9])nit[\s:,)\]\-]",
    re.IGNORECASE,
)


def _classify_imported_severity(body: str) -> str:
    head = "\n".join(body.splitlines()[:2])
    if _NIT_PREFIX_RE.search(head):
        return models.Severity.NIT.value
    return models.Severity.FEEDBACK.value


def _classify_imported_review_severity(raw: dict) -> str:
    if raw.get("state") == "CHANGES_REQUESTED":
        return models.Severity.WARNING.value
    return _classify_imported_severity(raw.get("body", ""))


def _classify_imported_review_category(raw: dict) -> str:
    state = raw.get("state")
    if state == "APPROVED":
        return models.CommentCategory.APPROVE.value
    if state == "CHANGES_REQUESTED":
        return models.CommentCategory.REQUEST_CHANGES.value
    return models.CommentCategory.COMMENT.value


def _external_key(source: str | None, ext_id: str | None) -> str:
    return f"{source or ''}:{ext_id or ''}"


def _normalize_github_timestamp(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _github_timestamp(raw: dict, *fields: str) -> str | None:
    for field in fields:
        value = _normalize_github_timestamp(raw.get(field))
        if value:
            return value
    return None


def _comment_kwargs(raw: dict, *timestamp_fields: str) -> dict:
    ts = _github_timestamp(raw, *timestamp_fields)
    return {"timestamp": ts} if ts else {}


@dataclass
class PullResult:
    new_anchored: int = 0
    new_global: int = 0
    new_reviews: int = 0
    edited: int = 0
    retimestamped: int = 0
    recategorized: int = 0
    skipped: int = 0

    @property
    def total_changes(self) -> int:
        return (self.new_anchored + self.new_global + self.new_reviews +
                self.edited + self.retimestamped + self.recategorized)

    def summary(self) -> str:
        bits = [f"{self.new_anchored} anchored", f"{self.new_global} global"]
        if self.new_reviews:
            bits.append(f"{self.new_reviews} review summaries")
        if self.edited:
            bits.append(f"{self.edited} edited")
        if self.retimestamped:
            bits.append(f"{self.retimestamped} timestamps")
        if self.recategorized:
            bits.append(f"{self.recategorized} categories")
        return f"Pulled {' + '.join(bits)} ({self.skipped} already local)."


def _retimestamp_existing(
    session_dir: str | Path,
    existing: models.Comment,
    timestamp: str | None,
    result: PullResult,
    *,
    dry_run: bool,
) -> bool:
    if not timestamp or existing.timestamp == timestamp:
        return False
    result.retimestamped += 1
    if not dry_run:
        store.update_comment_external(session_dir, existing.id, timestamp=timestamp)
    return True


def _recategorize_existing(
    session_dir: str | Path,
    existing: models.Comment,
    category: str,
    result: PullResult,
    *,
    dry_run: bool,
) -> bool:
    if existing.category == category:
        return False
    result.recategorized += 1
    if not dry_run:
        store.update_comment_external(session_dir, existing.id, category=category)
    return True


def pull_comments(
    session_dir: str | Path,
    session: models.Session,
    *,
    dry_run: bool = False,
) -> PullResult:
    """Fetch and merge GitHub PR comments. Raises `gh.GhError` on transport
    failure — callers translate to UX-appropriate output."""
    if session.github is None:
        raise ValueError("session has no GitHub backing")
    ghpr = session.github

    review_comments = gh.fetch_review_comments(ghpr.repo, ghpr.number)
    issue_comments = gh.fetch_issue_comments(ghpr.repo, ghpr.number)
    pr_reviews = gh.fetch_pr_reviews(ghpr.repo, ghpr.number)

    local_by_ext: dict[str, models.Comment] = {
        _external_key(c.external_source, c.external_id): c
        for c in store.read_all_comments(session_dir)
        if c.external_id and c.external_source in {"github", "github-review"}
    }

    result = PullResult()

    def _resolve_reply_to(raw: dict) -> str | None:
        parent_ext = raw.get("in_reply_to_id")
        if not parent_ext:
            return None
        parent_local = local_by_ext.get(_external_key("github", str(parent_ext)))
        if parent_local is None:
            return None
        all_local = store.read_all_comments(session_dir)
        return store.normalize_reply_to(all_local, parent_local.id)

    for raw in review_comments:
        ext_id = str(raw["id"])
        body = raw.get("body", "")
        timestamp = _github_timestamp(raw, "created_at")
        existing = local_by_ext.get(_external_key("github", ext_id))

        if existing is not None:
            if body != (existing.external_synced_body or ""):
                if dry_run:
                    result.edited += 1
                    continue
                login = raw.get("user", {}).get("login", "unknown")
                store.edit_comment(
                    session_dir, existing.id, body=body,
                    edited_by=f"gh:{login}",
                )
                store.update_comment_external(
                    session_dir, existing.id, timestamp=timestamp,
                    external_synced_body=body,
                )
                result.edited += 1
            elif _retimestamp_existing(
                session_dir, existing, timestamp, result, dry_run=dry_run,
            ):
                pass
            else:
                result.skipped += 1
            continue

        if dry_run:
            result.new_anchored += 1
            continue

        login = raw.get("user", {}).get("login", "unknown")
        c = models.Comment(
            author=f"gh:{login}",
            file=raw.get("path", ""),
            line=raw.get("line") or raw.get("original_line") or 0,
            end_line=(raw.get("start_line")
                      if raw.get("start_line") and raw["start_line"] != raw.get("line")
                      else None),
            body=body,
            severity=_classify_imported_severity(body),
            head_sha=raw.get("commit_id"),
            external_source="github",
            external_id=ext_id,
            external_url=raw.get("html_url", ""),
            external_in_reply_to=(str(raw["in_reply_to_id"])
                                  if raw.get("in_reply_to_id") else None),
            external_synced_body=body,
            reply_to=_resolve_reply_to(raw),
            **_comment_kwargs(raw, "created_at"),
        )
        store.append_comment(session_dir, c)
        local_by_ext[_external_key("github", ext_id)] = c
        result.new_anchored += 1

    for raw in issue_comments:
        ext_id = str(raw["id"])
        body = raw.get("body", "")
        timestamp = _github_timestamp(raw, "created_at")
        existing = local_by_ext.get(_external_key("github", ext_id))

        if existing is not None:
            if body != (existing.external_synced_body or ""):
                if dry_run:
                    result.edited += 1
                    continue
                login = raw.get("user", {}).get("login", "unknown")
                store.edit_comment(
                    session_dir, existing.id, body=body,
                    edited_by=f"gh:{login}",
                )
                store.update_comment_external(
                    session_dir, existing.id, timestamp=timestamp,
                    external_synced_body=body,
                )
                result.edited += 1
            elif _retimestamp_existing(
                session_dir, existing, timestamp, result, dry_run=dry_run,
            ):
                pass
            else:
                result.skipped += 1
            continue

        if dry_run:
            result.new_global += 1
            continue

        login = raw.get("user", {}).get("login", "unknown")
        c = models.Comment(
            author=f"gh:{login}",
            file=sess.GLOBAL_FILE,
            line=0,
            body=body,
            severity=_classify_imported_severity(body),
            head_sha=session.current_head,
            external_source="github",
            external_id=ext_id,
            external_url=raw.get("html_url", ""),
            external_synced_body=body,
            **_comment_kwargs(raw, "created_at"),
        )
        store.append_comment(session_dir, c)
        local_by_ext[_external_key("github", ext_id)] = c
        result.new_global += 1

    for raw in pr_reviews:
        body = raw.get("body", "")
        category = _classify_imported_review_category(raw)
        if not body.strip() and category == models.CommentCategory.COMMENT.value:
            continue
        ext_id = str(raw["id"])
        timestamp = _github_timestamp(raw, "submitted_at", "created_at", "updated_at")
        existing = local_by_ext.get(_external_key("github-review", ext_id))

        if existing is not None:
            if body != (existing.external_synced_body or ""):
                if dry_run:
                    result.edited += 1
                    continue
                login = raw.get("user", {}).get("login", "unknown")
                store.edit_comment(
                    session_dir, existing.id, body=body,
                    severity=_classify_imported_review_severity(raw),
                    edited_by=f"gh:{login}",
                )
                store.update_comment_external(
                    session_dir, existing.id, timestamp=timestamp,
                    category=category,
                    external_synced_body=body,
                )
                result.edited += 1
            elif _recategorize_existing(
                session_dir, existing, category, result, dry_run=dry_run,
            ):
                _retimestamp_existing(
                    session_dir, existing, timestamp, result, dry_run=dry_run,
                )
            elif _retimestamp_existing(
                session_dir, existing, timestamp, result, dry_run=dry_run,
            ):
                pass
            else:
                result.skipped += 1
            continue

        if dry_run:
            result.new_reviews += 1
            continue

        login = raw.get("user", {}).get("login", "unknown")
        c = models.Comment(
            author=f"gh:{login}",
            file=sess.GLOBAL_FILE,
            line=0,
            body=body,
            severity=_classify_imported_review_severity(raw),
            category=category,
            head_sha=raw.get("commit_id") or session.current_head,
            external_source="github-review",
            external_id=ext_id,
            external_url=raw.get("html_url", ""),
            external_synced_body=body,
            **_comment_kwargs(raw, "submitted_at", "created_at", "updated_at"),
        )
        store.append_comment(session_dir, c)
        local_by_ext[_external_key("github-review", ext_id)] = c
        result.new_reviews += 1

    return result
