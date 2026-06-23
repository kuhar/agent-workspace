"""Push planning + execution. Shared between the CLI (`gh-push`) and the
web UI's confirmation-modal endpoints.

Two phases so the UI can preview before committing:
- `plan_push(comments)` — pure: classify into new-top/reply/edit buckets,
  build the local→external id map, count skipped rows.
- `execute_push(session_dir, session, ghpr, plan)` — side-effecting: creates
  one GitHub review for new top-level feedback, then persists external_* on
  each successful comment.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import subprocess

from . import gh, models, session as sess, store


DEFAULT_REVIEW_BODY = "A few comments"
DEFAULT_DIFF_CONTEXT = 3


@dataclass(frozen=True)
class AnchorPromotion:
    """A local inline anchor that cannot be submitted as a GitHub review item."""

    comment_id: str
    original_file: str
    original_line: int
    original_end_line: int | None = None
    reason: str = "outside GitHub review diff"

    @property
    def ref(self) -> str:
        if self.original_end_line and self.original_end_line != self.original_line:
            lo, hi = sorted((self.original_line, self.original_end_line))
            return f"{self.original_file}:{lo}-{hi}"
        return f"{self.original_file}:{self.original_line}"


@dataclass(frozen=True)
class ReviewAnchorIndex:
    """New-side lines that GitHub can accept as review-comment anchors."""

    lines_by_path: dict[str, set[int]]
    error: str | None = None

    def can_anchor(self, c: models.Comment) -> bool:
        if self.error is not None:
            return True
        if c.file == sess.GLOBAL_FILE:
            return True
        lines = self.lines_by_path.get(c.file)
        if lines is None:
            return False
        end = c.end_line if c.end_line is not None else c.line
        lo, hi = (c.line, end) if c.line <= end else (end, c.line)
        return all(line in lines for line in range(lo, hi + 1))


@dataclass
class PushPlan:
    new_top: list[models.Comment] = field(default_factory=list)
    new_replies: list[models.Comment] = field(default_factory=list)
    edits: list[models.Comment] = field(default_factory=list)
    skipped_meta: int = 0
    skipped_imported_reviews: int = 0
    anchor_validation_error: str | None = None
    promoted_anchors: dict[str, AnchorPromotion] = field(default_factory=dict)
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
    promoted: int = 0
    skipped_meta: int = 0
    skipped_imported_reviews: int = 0

    def summary(self) -> str:
        parts = [f"Pushed {self.pushed}"]
        if self.promoted:
            parts.append(f"promoted {self.promoted} to global")
        if self.failed:
            parts.append(f"failed {self.failed}")
        if self.orphaned:
            parts.append(f"orphaned {self.orphaned}")
        if self.skipped_meta:
            parts.append(f"skipped {self.skipped_meta} __meta__")
        if self.skipped_imported_reviews:
            parts.append(f"skipped {self.skipped_imported_reviews} imported reviews")
        return ", ".join(parts) + "."


_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def build_review_anchor_index(
    repo_path: str | Path,
    base_ref: str,
    topic_ref: str,
    *,
    context: int = DEFAULT_DIFF_CONTEXT,
) -> ReviewAnchorIndex:
    """Build the GitHub-style new-side review-anchor line set.

    The main renderer intentionally uses full-file context so humans can see
    local comments anywhere in a changed file. GitHub review comments are more
    restrictive: anchors must land on new-side lines that appear in the review
    diff hunk. Use a normal unified diff here so planning catches those anchors
    before the create-review API rejects the batch.
    """
    result = subprocess.run(
        [
            "git", "-C", str(repo_path),
            "diff", f"-U{context}", "--no-color",
            f"{base_ref}...{topic_ref}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if not detail:
            detail = f"git diff exited {result.returncode}"
        return ReviewAnchorIndex({}, error=detail)

    lines_by_path: dict[str, set[int]] = {}
    current_path: str | None = None
    new_ln = 0
    for line in result.stdout.splitlines():
        if line.startswith("diff --git"):
            current_path = None
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                current_path = None
                continue
            current_path = raw_path[2:] if raw_path.startswith("b/") else raw_path
            lines_by_path.setdefault(current_path, set())
            continue
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                new_ln = int(m.group(1))
            continue
        if current_path is None:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            lines_by_path[current_path].add(new_ln)
            new_ln += 1
        elif line.startswith("-"):
            continue
        else:
            lines_by_path[current_path].add(new_ln)
            new_ln += 1

    return ReviewAnchorIndex(lines_by_path)


def _promoted_body(c: models.Comment, promotion: AnchorPromotion) -> str:
    body = c.body.strip()
    prefix = f"Original anchor: `{promotion.ref}`"
    return f"{prefix}\n\n{body}" if body else prefix


def _promote_comment(c: models.Comment, promotion: AnchorPromotion) -> models.Comment:
    return replace(
        c,
        file=sess.GLOBAL_FILE,
        line=0,
        end_line=None,
        body=_promoted_body(c, promotion),
    )


def plan_push(
    comments: list[models.Comment],
    *,
    anchor_index: ReviewAnchorIndex | None = None,
) -> PushPlan:
    """Classify live comments into push buckets.

    - external_id is None → new (top-level or reply by reply_to)
    - external_id set + body diverges from synced body → edit
    - matches synced → no-op (not in any bucket)
    - file == META_FILE → skipped (__meta__ has no GH equivalent)
    - imported/pushed PR review summaries are skipped after their backing
      GitHub review object exists
    """
    plan = PushPlan()
    if anchor_index is not None:
        plan.anchor_validation_error = anchor_index.error
    for c in comments:
        c.category = models.normalize_comment_category(c.category)
        if c.deleted:
            continue
        if c.file == sess.META_FILE:
            plan.skipped_meta += 1
            continue
        if c.external_source and c.external_source != "github":
            plan.skipped_imported_reviews += 1
            continue
        if models.category_is_review_decision(c.category) and c.file != sess.GLOBAL_FILE:
            plan.skipped_meta += 1
            continue
        if c.external_id is None:
            if (
                anchor_index is not None
                and not c.reply_to
                and c.file != sess.GLOBAL_FILE
                and not anchor_index.can_anchor(c)
            ):
                promotion = AnchorPromotion(
                    comment_id=c.id,
                    original_file=c.file,
                    original_line=c.line,
                    original_end_line=c.end_line,
                )
                plan.promoted_anchors[c.id] = promotion
                c = _promote_comment(c, promotion)
            (plan.new_replies if c.reply_to else plan.new_top).append(c)
        elif c.body != (c.external_synced_body or ""):
            plan.edits.append(c)
    plan.ext_map = {
        c.id: c.external_id for c in comments
        if c.external_id is not None
    }
    return plan


def filter_plan(plan: PushPlan, comment_ids: set[str]) -> PushPlan:
    """Return a copy of ``plan`` limited to selected local comment ids."""
    return PushPlan(
        new_top=[c for c in plan.new_top if c.id in comment_ids],
        new_replies=[c for c in plan.new_replies if c.id in comment_ids],
        edits=[c for c in plan.edits if c.id in comment_ids],
        skipped_meta=plan.skipped_meta,
        skipped_imported_reviews=plan.skipped_imported_reviews,
        anchor_validation_error=plan.anchor_validation_error,
        promoted_anchors={
            cid: promotion
            for cid, promotion in plan.promoted_anchors.items()
            if cid in comment_ids
        },
        ext_map=dict(plan.ext_map),
    )


def _review_comment_payload(c: models.Comment) -> dict:
    """Build a GitHub review `comments[]` item for one anchored comment."""
    end = c.end_line if c.end_line is not None else c.line
    lo, hi = (c.line, end) if c.line <= end else (end, c.line)
    payload: dict = {
        "body": c.body,
        "path": c.file,
        "line": hi,
        "side": "RIGHT",
    }
    if lo != hi:
        payload["start_line"] = lo
        payload["start_side"] = "RIGHT"
    return payload


def _review_body(global_comments: list[models.Comment]) -> str:
    """Fold selected global comments into one GitHub review summary."""
    return "\n\n".join(c.body.strip() for c in global_comments if c.body.strip())


def _review_event(global_comments: list[models.Comment]) -> str:
    categories = {
        models.normalize_comment_category(c.category) for c in global_comments
    }
    if models.CommentCategory.REQUEST_CHANGES.value in categories:
        return "REQUEST_CHANGES"
    if models.CommentCategory.APPROVE.value in categories:
        return "APPROVE"
    return "COMMENT"


def _payload_signature(payload: dict) -> tuple[str, int, int, str]:
    line = int(payload.get("line") or 0)
    start_line = int(payload.get("start_line") or 0)
    if start_line == line:
        start_line = 0
    return (
        str(payload.get("path") or ""),
        line,
        start_line,
        str(payload.get("body") or ""),
    )


def _remote_signature(raw: dict) -> tuple[str, int, int, str]:
    line = int(raw.get("line") or raw.get("original_line") or 0)
    start_line = int(raw.get("start_line") or 0)
    if start_line == line:
        start_line = 0
    return (
        str(raw.get("path") or ""),
        line,
        start_line,
        str(raw.get("body") or ""),
    )


def _remote_key(raw: dict) -> str:
    return str(raw.get("id") or id(raw))


def _match_review_comments(
    local_comments: list[models.Comment],
    payloads: list[dict],
    remote_comments: list[dict],
) -> dict[str, dict]:
    """Map GitHub's created review comments back to local comment ids."""
    by_signature: dict[tuple[str, int, int, str], deque[dict]] = defaultdict(deque)
    for raw in remote_comments:
        by_signature[_remote_signature(raw)].append(raw)

    remaining = deque(remote_comments)
    used: set[str] = set()
    matched: dict[str, dict] = {}
    for c, payload in zip(local_comments, payloads):
        raw = None
        queue = by_signature.get(_payload_signature(payload))
        while queue and _remote_key(queue[0]) in used:
            queue.popleft()
        if queue:
            raw = queue.popleft()
        else:
            while remaining and _remote_key(remaining[0]) in used:
                remaining.popleft()
            if remaining:
                raw = remaining.popleft()
        if raw is None:
            continue
        used.add(_remote_key(raw))
        matched[c.id] = raw
    return matched


def execute_push(
    session_dir: str | Path,
    session: models.Session,
    ghpr: models.GitHubPR,
    plan: PushPlan,
) -> PushResult:
    """Run the plan: submit new top-levels, then replies, then PATCH edits.

    Replies whose parent has no external_id (and isn't pushed in this run)
    are reported as orphaned. Each successful op stamps `external_id`,
    `external_url`, and `external_synced_body` on the local comment so a
    re-run is a no-op.
    """
    result = PushResult(
        skipped_meta=plan.skipped_meta,
        skipped_imported_reviews=plan.skipped_imported_reviews,
    )
    ext_map = dict(plan.ext_map)  # local copy — don't mutate caller's

    inline_top = [c for c in plan.new_top if c.file != sess.GLOBAL_FILE]
    global_top = [c for c in plan.new_top if c.file == sess.GLOBAL_FILE]
    review_body = _review_body(global_top)
    review_event = _review_event(global_top)
    inline_payloads = [_review_comment_payload(c) for c in inline_top]
    if review_event == "COMMENT" and not review_body:
        review_body = DEFAULT_REVIEW_BODY
    created_inline: dict[str, dict] = {}
    review_id: str | None = None
    review_url = ""
    if inline_top or global_top:
        try:
            resp = gh.post_pr_review(
                ghpr.repo, ghpr.number,
                event=review_event,
                body=review_body,
                commit_id=session.current_head if inline_payloads else None,
                comments=inline_payloads or None,
            )
        except gh.GhError as e:
            for c in plan.new_top:
                result.items.append(PushItemResult(
                    id=c.id, action="new", error=str(e),
                ))
            result.failed += len(plan.new_top)
        else:
            review_id = str(resp["id"])
            review_url = resp.get("html_url", "")
            if inline_top:
                try:
                    remote_inline = gh.fetch_pr_review_comments(
                        ghpr.repo, ghpr.number, review_id,
                    )
                    created_inline = _match_review_comments(
                        inline_top, inline_payloads, remote_inline,
                    )
                except gh.GhError as e:
                    for c in inline_top:
                        result.items.append(PushItemResult(
                            id=c.id, action="new",
                            error=(
                                "review created but comments could not be "
                                f"fetched: {e}"
                            ),
                        ))
                    result.failed += len(inline_top)

            failed_new_ids = {i.id for i in result.items if i.error}
            stamped_review_summary = False
            for c in plan.new_top:
                if c.file == sess.GLOBAL_FILE:
                    if not stamped_review_summary:
                        store.update_comment_external(
                            session_dir, c.id,
                            external_source="github-review",
                            external_id=review_id,
                            external_url=review_url,
                            external_synced_body=review_body,
                        )
                        stamped_review_summary = True
                    store.delete_comment(session_dir, c.id, deleted_by="github-push")
                    result.items.append(PushItemResult(
                        id=c.id, action="new",
                        external_id=review_id, external_url=review_url,
                    ))
                    result.pushed += 1
                    if c.id in plan.promoted_anchors:
                        result.promoted += 1
                    continue

                raw = created_inline.get(c.id)
                if raw is None:
                    if c.id not in failed_new_ids:
                        result.items.append(PushItemResult(
                            id=c.id, action="new",
                            error="review created but comment id was not returned",
                        ))
                        result.failed += 1
                        failed_new_ids.add(c.id)
                    continue
                ext_id = str(raw["id"])
                url = raw.get("html_url", "")
                store.update_comment_external(
                    session_dir, c.id,
                    external_source="github", external_id=ext_id,
                    external_url=url, external_synced_body=c.body,
                )
                ext_map[c.id] = ext_id
                result.items.append(PushItemResult(
                    id=c.id, action="new",
                    external_id=ext_id, external_url=url,
                ))
                result.pushed += 1

    for c in plan.new_replies:
        if c.reply_to in plan.promoted_anchors:
            result.items.append(PushItemResult(
                id=c.id, action="reply",
                error="parent promoted to global; replies cannot be pushed",
            ))
            result.orphaned += 1
            continue
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
