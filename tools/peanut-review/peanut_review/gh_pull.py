"""Pull GitHub PR comments into a local session. Shared between the CLI
(`gh-pull`) and the web UI's `/api/gh/pull` endpoint.

The pull is keyed on `external_id`:
  1. New comments (no local match) are appended as `gh:<login>` authors.
     Replies land threaded — `in_reply_to_id` is resolved to the matching
     local comment and stored as `reply_to`, normalized via
     `store.normalize_reply_to`.
  2. Existing comments whose GitHub body diverges from our last
     `external_synced_body` get an `edit_comment` applied so the change
     shows up in version history.
  3. Already-synced comments (matching id, matching body) are skipped.

Idempotent: re-running with no upstream changes is a no-op.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass
class PullResult:
    new_anchored: int = 0
    new_global: int = 0
    edited: int = 0
    skipped: int = 0

    @property
    def total_changes(self) -> int:
        return self.new_anchored + self.new_global + self.edited

    def summary(self) -> str:
        bits = [f"{self.new_anchored} anchored", f"{self.new_global} global"]
        if self.edited:
            bits.append(f"{self.edited} edited")
        return f"Pulled {' + '.join(bits)} ({self.skipped} already local)."


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

    local_by_ext: dict[str, models.Comment] = {
        c.external_id: c for c in store.read_all_comments(session_dir)
        if c.external_source == "github" and c.external_id
    }

    result = PullResult()

    def _resolve_reply_to(raw: dict) -> str | None:
        parent_ext = raw.get("in_reply_to_id")
        if not parent_ext:
            return None
        parent_local = local_by_ext.get(str(parent_ext))
        if parent_local is None:
            return None
        all_local = store.read_all_comments(session_dir)
        return store.normalize_reply_to(all_local, parent_local.id)

    for raw in review_comments:
        ext_id = str(raw["id"])
        body = raw.get("body", "")
        existing = local_by_ext.get(ext_id)

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
                    session_dir, existing.id, external_synced_body=body,
                )
                result.edited += 1
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
        )
        store.append_comment(session_dir, c)
        local_by_ext[ext_id] = c
        result.new_anchored += 1

    for raw in issue_comments:
        ext_id = str(raw["id"])
        body = raw.get("body", "")
        existing = local_by_ext.get(ext_id)

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
                    session_dir, existing.id, external_synced_body=body,
                )
                result.edited += 1
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
        )
        store.append_comment(session_dir, c)
        local_by_ext[ext_id] = c
        result.new_global += 1

    return result
