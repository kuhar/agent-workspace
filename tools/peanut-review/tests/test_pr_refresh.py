"""Tests for safe GitHub PR checkout refreshes."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from peanut_review import gh, gh_pull, pr_refresh, session as sess, store
from peanut_review.models import Comment, GitHubPR


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _github_session(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "review", str(repo)], check=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "file.txt").write_text("topic\n")
    _git(repo, "commit", "-q", "-am", "topic")
    head = _git(repo, "rev-parse", "HEAD")

    session_dir = tmp_path / "session"
    session, _ = sess.create_session(
        workspace=str(repo),
        base_ref=base,
        topic_ref=head,
        agents=[{"name": "felix", "model": "m", "persona": "felix.md"}],
        session_dir=str(session_dir),
    )
    session.github = GitHubPR(
        repo="acme/project",
        number=42,
        url="https://github.com/acme/project/pull/42",
        head_sha=head,
        base_sha=base,
        title="Change",
        head_ref_name="feature/change",
    )
    sess.save_session(session_dir, session)
    return session_dir, repo


def test_refresh_checks_out_current_branch_syncs_and_pulls(tmp_path: Path):
    session_dir, repo = _github_session(tmp_path)
    old_head = _git(repo, "rev-parse", "HEAD")
    base = sess.load_session(session_dir).base_ref
    comment = Comment(author="felix", file="file.txt", line=1, body="old")
    store.append_comment(session_dir, comment)
    updated_head = ""

    def fake_checkout(pr, *, workspace, branch):
        nonlocal updated_head
        assert pr == "https://github.com/acme/project/pull/42"
        assert workspace == str(repo)
        assert branch == "review"
        (repo / "file.txt").write_text("updated\n")
        _git(repo, "commit", "-q", "-am", "updated")
        updated_head = _git(repo, "rev-parse", "HEAD")
        return "fast-forwarded"

    def fake_info(repo_name, number):
        assert (repo_name, number) == ("acme/project", 42)
        return gh.PRInfo(
            repo=repo_name,
            number=number,
            url="https://github.com/acme/project/pull/42",
            title="Updated change",
            head_sha=updated_head,
            base_sha=base,
            head_ref_name="feature/change",
        )

    with (
        patch("peanut_review.pr_refresh.gh.checkout_pr", side_effect=fake_checkout),
        patch("peanut_review.pr_refresh.gh.fetch_pr_info", side_effect=fake_info),
        patch(
            "peanut_review.pr_refresh.gh_pull.pull_comments",
            return_value=gh_pull.PullResult(skipped=3),
        ) as pull,
    ):
        result = pr_refresh.refresh_pr(session_dir)

    assert result.old_head == old_head
    assert result.new_head == updated_head
    assert result.head_changed is True
    assert result.stale_count == 1
    assert result.summary().startswith(
        f"Updated {old_head[:12]} to {updated_head[:12]}; Pulled"
    )
    synced = sess.load_session(session_dir)
    assert synced.current_head == updated_head
    assert synced.github is not None
    assert synced.github.title == "Updated change"
    assert store.read_all_comments(session_dir)[0].stale is True
    pull.assert_called_once()


def test_refresh_rejects_dirty_checkout_before_gh(tmp_path: Path):
    session_dir, repo = _github_session(tmp_path)
    (repo / "local.txt").write_text("untracked\n")

    with patch("peanut_review.pr_refresh.gh.checkout_pr") as checkout:
        with pytest.raises(pr_refresh.RefreshConflict, match="local changes"):
            pr_refresh.refresh_pr(session_dir)

    checkout.assert_not_called()


def test_refresh_rejects_live_agents_before_checkout(tmp_path: Path):
    session_dir, _ = _github_session(tmp_path)
    live = {
        "reviewer_live": True,
        "supervisor_live": False,
    }
    with (
        patch("peanut_review.pr_refresh.runtime.inspect_agent_runtime", return_value=live),
        patch("peanut_review.pr_refresh.gh.checkout_pr") as checkout,
    ):
        with pytest.raises(pr_refresh.RefreshConflict, match="felix"):
            pr_refresh.refresh_pr(session_dir)

    checkout.assert_not_called()
