"""Push planning + execution. Shared between the CLI (`gh-push`) and the
web UI's confirmation-modal endpoints.

Two phases so the UI can preview before committing:
- `plan_push(comments)` — pure: classify into new-top/reply/edit buckets,
  build the local→external id map, count skipped-meta.
- `execute_push(session_dir, session, ghpr, plan)` — side-effecting: hits
  `gh` for each bucket, persists external_* on each successful comment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import gh, models, session as sess, store


@dataclass
class PushPlan:
    new_top: list[models.Comment] = field(default_factory=list)
    new_replies: list[models.Comment] = field(default_factory=list)
    edits: list[models.Comment] = field(default_factory=list)
    skipped_meta: int = 0
    # local_id → external_id, seeded from already-pushed comments.
    ext_map: dict[str, str] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.new_top) + len(self.new_replies) + len(self.edits)


@dataclass
class PushItemResult:
    id: str
    action: str  # "new" | "reply" | "edit"
    external_id: str | None = None
    external_url: str | None = None
    error: str | None = None


@dataclass
class PushResult:
    items: list[PushItemResult] = field(default_factory=list)
    pushed: int = 0
    failed: int = 0
    orphaned: int = 0
    skipped_meta: int = 0

    def summary(self) -> str:
        parts = [f"Pushed {self.pushed}"]
        if self.failed:
            parts.append(f"failed {self.failed}")
        if self.orphaned:
            parts.append(f"orphaned {self.orphaned}")
        if self.skipped_meta:
            parts.append(f"skipped {self.skipped_meta} __meta__")
        return ", ".join(parts) + "."


def plan_push(comments: list[models.Comment]) -> PushPlan:
    """Classify live comments into push buckets.

    - external_id is None → new (top-level or reply by reply_to)
    - external_id set + body diverges from synced body → edit
    - matches synced → no-op (not in any bucket)
    - file == META_FILE → skipped (__meta__ has no GH equivalent)
    """
    plan = PushPlan()
    for c in comments:
        if c.deleted:
            continue
        if c.file == sess.META_FILE:
            plan.skipped_meta += 1
            continue
        if c.external_id is None:
            (plan.new_replies if c.reply_to else plan.new_top).append(c)
        elif c.body != (c.external_synced_body or ""):
            plan.edits.append(c)
    plan.ext_map = {
        c.id: c.external_id for c in comments
        if c.external_id is not None
    }
    return plan


def execute_push(
    session_dir: str | Path,
    session: models.Session,
    ghpr: models.GitHubPR,
    plan: PushPlan,
) -> PushResult:
    """Run the plan: POST new top-levels, then replies, then PATCH edits.

    Replies whose parent has no external_id (and isn't pushed in this run)
    are reported as orphaned. Each successful op stamps `external_id`,
    `external_url`, and `external_synced_body` on the local comment so a
    re-run is a no-op.
    """
    result = PushResult(skipped_meta=plan.skipped_meta)
    ext_map = dict(plan.ext_map)  # local copy — don't mutate caller's

    for c in plan.new_top:
        try:
            if c.file == sess.GLOBAL_FILE:
                resp = gh.post_issue_comment(ghpr.repo, ghpr.number, body=c.body)
            else:
                resp = gh.post_review_comment(
                    ghpr.repo, ghpr.number,
                    body=c.body,
                    commit_id=session.current_head,
                    path=c.file,
                    line=c.line,
                    start_line=c.end_line,
                )
        except gh.GhError as e:
            result.items.append(PushItemResult(id=c.id, action="new", error=str(e)))
            result.failed += 1
            continue
        ext_id = str(resp["id"])
        url = resp.get("html_url", "")
        store.update_comment_external(
            session_dir, c.id,
            external_source="github", external_id=ext_id,
            external_url=url, external_synced_body=c.body,
        )
        ext_map[c.id] = ext_id
        result.items.append(PushItemResult(
            id=c.id, action="new", external_id=ext_id, external_url=url,
        ))
        result.pushed += 1

    for c in plan.new_replies:
        parent_ext = ext_map.get(c.reply_to or "")
        if not parent_ext:
            result.items.append(PushItemResult(
                id=c.id, action="reply",
                error=f"parent {c.reply_to} not pushed",
            ))
            result.orphaned += 1
            continue
        try:
            resp = gh.post_review_reply(
                ghpr.repo, ghpr.number, parent_ext, body=c.body,
            )
        except gh.GhError as e:
            result.items.append(PushItemResult(id=c.id, action="reply", error=str(e)))
            result.failed += 1
            continue
        ext_id = str(resp["id"])
        url = resp.get("html_url", "")
        store.update_comment_external(
            session_dir, c.id,
            external_source="github", external_id=ext_id,
            external_url=url, external_in_reply_to=parent_ext,
            external_synced_body=c.body,
        )
        ext_map[c.id] = ext_id
        result.items.append(PushItemResult(
            id=c.id, action="reply", external_id=ext_id, external_url=url,
        ))
        result.pushed += 1

    for c in plan.edits:
        try:
            if c.file == sess.GLOBAL_FILE:
                gh.patch_issue_comment(ghpr.repo, c.external_id, body=c.body)
            else:
                gh.patch_review_comment(ghpr.repo, c.external_id, body=c.body)
        except gh.GhError as e:
            result.items.append(PushItemResult(id=c.id, action="edit", error=str(e)))
            result.failed += 1
            continue
        store.update_comment_external(
            session_dir, c.id,
            external_synced_body=c.body,
        )
        result.items.append(PushItemResult(
            id=c.id, action="edit",
            external_id=c.external_id, external_url=c.external_url,
        ))
        result.pushed += 1

    return result
