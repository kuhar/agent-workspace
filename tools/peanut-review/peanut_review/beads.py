"""Beads (br) CLI integration for review tracking."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .session import load_session


def _run_br(*args: str, cwd: str | None = None) -> str:
    result = subprocess.run(
        ["br", *args],
        capture_output=True, text=True, timeout=30, cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(f"br {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def create_review_bead(session_dir: str | Path) -> str:
    """Create a bead to track this review session. Returns the bead ID."""
    session = load_session(session_dir)
    title = f"peanut-review: {session.id}"
    agents = ", ".join(a.name for a in session.agents)
    desc = (
        f"Automated code review session\n"
        f"Base: {session.base_ref}\n"
        f"Head: {session.original_head[:12]}\n"
        f"Agents: {agents}\n"
        f"Session: {session_dir}"
    )
    output = _run_br(
        "create", "--title", title, "--description", desc,
        "--type", "task",
        cwd=session.workspace,
    )
    # br create outputs the issue ID as last token
    parts = output.strip().split() if output else []
    return parts[-1] if parts else ""


def update_with_verdict(session_dir: str | Path, verdict: str, body: str = "") -> None:
    """Update the bead with the review verdict."""
    session = load_session(session_dir)
    if not session.bead_id:
        return
    comment = f"Review verdict: {verdict}"
    if body:
        comment += f"\n{body}"
    _run_br("comments", "add", session.bead_id, comment, cwd=session.workspace)
    status = "closed" if verdict == "approve" else "in_progress"
    close_reason = f"Review {verdict}" if verdict == "approve" else ""
    args = ["update", session.bead_id, "--status", status]
    if close_reason:
        args.extend(["--close-reason", close_reason])
    _run_br(*args, cwd=session.workspace)
