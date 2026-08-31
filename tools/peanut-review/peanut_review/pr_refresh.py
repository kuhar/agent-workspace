"""Safely refresh a GitHub-backed review checkout and session."""
from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from . import gh, gh_pull, runtime, session as sess
from .pr_sync import sync_session_to_pr


class RefreshConflict(RuntimeError):
    """The checkout cannot be refreshed without operator intervention."""


@dataclass(frozen=True)
class RefreshResult:
    old_head: str
    new_head: str
    head_changed: bool
    stale_count: int
    checkout_output: str
    pull_result: gh_pull.PullResult

    def summary(self) -> str:
        if self.head_changed:
            checkout = f"Updated {self.old_head[:12]} to {self.new_head[:12]}"
        else:
            checkout = f"Already at {self.new_head[:12]}"
        return f"{checkout}; {self.pull_result.summary()}"


_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[Path, threading.Lock] = {}


def _session_lock(session_dir: str | Path) -> threading.Lock:
    key = Path(session_dir).resolve()
    with _LOCKS_GUARD:
        return _SESSION_LOCKS.setdefault(key, threading.Lock())


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RefreshConflict(f"could not inspect checkout: {e}") from e
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RefreshConflict(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _ensure_agents_stopped(session_dir: Path, session) -> None:
    live = []
    for agent in session.agents:
        snapshot = runtime.inspect_agent_runtime(session_dir, agent)
        if snapshot["reviewer_live"] or snapshot["supervisor_live"]:
            live.append(agent.name)
    if live:
        raise RefreshConflict(
            "cannot refresh while agents are running: " + ", ".join(live)
        )


def refresh_pr(session_dir: str | Path) -> RefreshResult:
    """Checkout the linked PR, sync its snapshot, then pull GitHub comments.

    This deliberately has no force mode. Dirty, detached, diverged, or active
    sessions require operator intervention through the CLI.
    """
    session_path = Path(session_dir)
    lock = _session_lock(session_path)
    if not lock.acquire(blocking=False):
        raise RefreshConflict("a PR refresh is already running for this session")
    try:
        session = sess.load_session(session_path)
        if session.github is None:
            raise ValueError("session is not GitHub-backed")
        _ensure_agents_stopped(session_path, session)

        repo = sess.repo_path(session)
        dirty = _git(repo, "status", "--porcelain")
        if dirty:
            raise RefreshConflict(
                "checkout has local changes; refresh it from the CLI after "
                "preserving or discarding them intentionally"
            )
        try:
            branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        except RefreshConflict as e:
            raise RefreshConflict(
                "checkout is detached; select a local review branch first"
            ) from e

        old_head = _git(repo, "rev-parse", "HEAD")
        try:
            checkout_output = gh.checkout_pr(
                session.github.url,
                workspace=str(repo),
                branch=branch,
            )
        except gh.GhError as e:
            raise RefreshConflict(
                f"gh pr checkout failed without force: {e}"
            ) from e

        pr_info = gh.fetch_pr_info(session.github.repo, session.github.number)
        new_head = _git(repo, "rev-parse", "HEAD")
        if new_head != pr_info.head_sha:
            raise RefreshConflict(
                "checkout did not reach the PR head "
                f"({new_head[:12]} != {pr_info.head_sha[:12]})"
            )

        _, head_changed, _, stale_count = sync_session_to_pr(
            session_path, pr_info,
        )
        updated = sess.load_session(session_path)
        pull_result = gh_pull.pull_comments(session_path, updated)
        return RefreshResult(
            old_head=old_head,
            new_head=new_head,
            head_changed=head_changed,
            stale_count=stale_count,
            checkout_output=checkout_output,
            pull_result=pull_result,
        )
    finally:
        lock.release()
