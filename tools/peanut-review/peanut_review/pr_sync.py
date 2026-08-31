"""Shared GitHub PR snapshot synchronization helpers."""
from __future__ import annotations

from pathlib import Path

from . import session as sess, store
from .models import GitHubPR, Session


def github_pr_from_info(
    pr_info,
    *,
    base_sha: str | None = None,
    head_sha: str | None = None,
) -> GitHubPR:
    return GitHubPR(
        repo=pr_info.repo,
        number=pr_info.number,
        url=pr_info.url,
        head_sha=head_sha or pr_info.head_sha,
        base_sha=base_sha or pr_info.base_sha,
        title=pr_info.title,
        head_ref_name=pr_info.head_ref_name,
    )


def sync_session_to_pr(
    session_dir: str | Path,
    pr_info,
    *,
    base_ref: str | None = None,
    topic_ref: str | None = None,
    workspace: str | None = None,
    repo_relative: str | None = None,
) -> tuple[Session, bool, bool, int]:
    """Synchronize a session to PR metadata and stale moved-head comments."""
    existing = sess.load_session(session_dir)
    if existing.github is not None and (
        existing.github.repo != pr_info.repo
        or existing.github.number != pr_info.number
    ):
        raise ValueError(
            f"session is linked to {existing.github.repo}#{existing.github.number}, "
            f"not {pr_info.repo}#{pr_info.number}"
        )
    session, head_changed, changed = sess.sync_session_snapshot(
        session_dir,
        base_ref=base_ref or pr_info.base_sha,
        topic_ref=topic_ref or pr_info.head_sha,
        github=github_pr_from_info(
            pr_info,
            base_sha=base_ref,
            head_sha=topic_ref,
        ),
        workspace=workspace,
        repo_relative=repo_relative,
    )
    stale_count = store.mark_stale(session_dir) if head_changed else 0
    return session, head_changed, changed, stale_count
