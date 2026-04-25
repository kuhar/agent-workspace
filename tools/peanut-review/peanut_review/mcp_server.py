"""MCP server exposing peanut-review tools for cursor agents.

Agents call structured MCP tools instead of Shell(peanut-review ...).
Eliminates the "prints commands instead of executing" problem with Gemini.

Usage:
    PEANUT_SESSION=/tmp/peanut-review/... python -m peanut_review.mcp_server

Configured in .cursor/mcp.json:
    {
      "mcpServers": {
        "peanut-review": {
          "command": "python3",
          "args": ["-m", "peanut_review.mcp_server"],
          "env": { "PEANUT_SESSION": "<session-dir>" }
        }
      }
    }
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import models, polling, session as sess, store

mcp = FastMCP("peanut-review")


def _session_dir() -> str:
    d = os.environ.get("PEANUT_SESSION", "")
    if not d or not Path(d).exists():
        raise ValueError(
            "PEANUT_SESSION not set or directory does not exist. "
            "Set it to the session directory path."
        )
    return d


def _get_author() -> str:
    return os.environ.get("GIT_AUTHOR_NAME", "unknown").lower()


# ── Tools ─────────────────────────────────────────────────────────────


@mcp.tool()
def status() -> str:
    """Show session status: state, agents, comment counts, signals."""
    sd = _session_dir()
    s = sess.load_session(sd)
    sess.refresh_agent_statuses(sd, s)

    lines = [
        f"Session: {s.id}",
        f"State: {s.state}",
        f"Base: {s.base_ref}",
        f"Head: {s.current_head[:12]}",
        f"Workspace: {s.workspace}",
        "",
        "Agents:",
    ]
    for a in s.agents:
        lines.append(f"  {a.name:<12} {a.status:<10} {a.model}")

    comments = [c for c in store.read_all_comments(sd) if not c.deleted]
    if comments:
        lines.append("")
        lines.append(
            f"Comments: {len(comments)} total, "
            f"{sum(1 for c in comments if c.severity == 'critical')} critical, "
            f"{sum(1 for c in comments if c.resolved)} resolved"
        )

    return "\n".join(lines)


@mcp.tool()
def add_comment(
    file: str,
    line: int,
    body: str,
    severity: str = "suggestion",
    end_line: int | None = None,
) -> str:
    """Post a review comment on a specific file and line.

    For high-level / cross-cutting feedback that doesn't belong on a single
    line (architecture, scope, testing strategy, missing telemetry, etc.),
    use `add_global_comment` instead.

    Args:
        file: Relative file path (use "__meta__" for test execution reports)
        line: Line number in the SOURCE FILE (not the diff output). Must be >= 1 for real files.
        body: Comment text describing the finding
        severity: One of: critical, warning, suggestion, nit
        end_line: Optional end line for multi-line findings

    Returns the source line at that location so you can verify it matches your finding.
    If the line doesn't match what you expected, you may have the wrong line number.
    """
    sd = _session_dir()
    s = sess.load_session(sd)
    author = _get_author()

    if severity not in ("critical", "warning", "suggestion", "nit"):
        return f"Error: severity must be one of: critical, warning, suggestion, nit (got '{severity}')"

    file_lines, err = sess.validate_comment_location(s.workspace, file, line)
    if err:
        return f"Error: {err}"

    comment = models.Comment(
        author=author,
        file=file,
        line=line,
        end_line=end_line,
        body=body,
        severity=severity,
        head_sha=s.current_head,
    )
    store.append_comment(sd, comment)

    if file_lines and line >= 1:
        return f"Comment {comment.id} stored. {file}:{line}: {file_lines[line - 1]}"
    return f"Comment {comment.id} stored."


@mcp.tool()
def add_global_comment(
    body: str,
    severity: str = "suggestion",
) -> str:
    """Post a HIGH-LEVEL comment that isn't tied to any file or line.

    Use this for review feedback that spans multiple files, calls out missing
    pieces (tests, docs, telemetry, error handling), questions the overall
    approach, or flags scope/architecture concerns. Do NOT use this for
    file/line findings — use add_comment for those.

    Args:
        body: Comment text.
        severity: One of: critical, warning, suggestion, nit.
    """
    sd = _session_dir()
    s = sess.load_session(sd)
    author = _get_author()

    if severity not in ("critical", "warning", "suggestion", "nit"):
        return f"Error: severity must be one of: critical, warning, suggestion, nit (got '{severity}')"

    comment = models.Comment(
        author=author,
        file=sess.GLOBAL_FILE,
        line=0,
        body=body,
        severity=severity,
        head_sha=s.current_head,
    )
    store.append_comment(sd, comment)
    return f"Global comment {comment.id} stored."


@mcp.tool()
def reply(
    parent_id: str,
    body: str,
    severity: str = "suggestion",
) -> str:
    """Reply to an existing comment, threading the discussion.

    Use this in Round 2+ to push back on a rebuttal or follow up on a
    Round 1 finding. Replies inherit the parent's file/line, so the thread
    stays anchored to the original spot.

    Args:
        parent_id: The Round 1 comment ID you're replying to (c_xxxxxxxx).
        body: Reply text.
        severity: One of: critical, warning, suggestion, nit.
    """
    sd = _session_dir()
    s = sess.load_session(sd)
    author = _get_author()

    if severity not in ("critical", "warning", "suggestion", "nit"):
        return f"Error: severity must be one of: critical, warning, suggestion, nit (got '{severity}')"

    all_comments = store.read_all_comments(sd)
    rooted = store.normalize_reply_to(all_comments, parent_id)
    if rooted is None:
        return f"Error: parent comment not found: {parent_id}"
    parent = next(c for c in all_comments if c.id == rooted)

    comment = models.Comment(
        author=author,
        file=parent.file,
        line=parent.line,
        body=body,
        severity=severity,
        head_sha=s.current_head,
        reply_to=rooted,
    )
    store.append_comment(sd, comment)
    return f"Reply {comment.id} stored (to {rooted})."


@mcp.tool()
def edit(
    comment_id: str,
    body: str | None = None,
    severity: str | None = None,
) -> str:
    """Rewrite an existing comment's body and/or severity.

    The prior wording is preserved in version history (visible in the web UI
    and via `peanut-review comments --show-edits`). Use this to refine a
    comment after additional context, or — when this session is backed by a
    GitHub PR — to clean up an agent-authored comment before pushing it.

    Args:
        comment_id: The comment to edit (c_xxxxxxxx).
        body: New comment text. Omit to keep the current body.
        severity: New severity (critical, warning, suggestion, nit). Omit
            to keep the current severity.
    """
    sd = _session_dir()
    edited_by = _get_author()

    if body is None and severity is None:
        return "Error: at least one of body or severity is required"
    if severity is not None and severity not in (
        "critical", "warning", "suggestion", "nit"
    ):
        return f"Error: severity must be one of: critical, warning, suggestion, nit (got '{severity}')"

    if not store.edit_comment(sd, comment_id,
                              body=body, severity=severity, edited_by=edited_by):
        return f"Error: comment {comment_id} not found"
    return f"Edited {comment_id}"


@mcp.tool()
def list_comments(
    since: str | None = None,
    severity: str | None = None,
    file: str | None = None,
) -> str:
    """List review comments, optionally filtered.

    Args:
        since: Comment ID — return only comments posted after this one. Use
            this to poll for new activity since you last looked. Pass the id
            of the most recent comment you've seen.
        severity: Filter by severity (critical, warning, suggestion, nit)
        file: Filter by file path
    """
    sd = _session_dir()
    comments = store.read_all_comments(sd)
    comments = store.filter_comments(
        comments, file=file, severity=severity, since=since,
    )
    if not comments:
        return "No comments found."

    lines = []
    for c in comments:
        stale = " [stale]" if c.stale else ""
        resolved = " [resolved]" if c.resolved else ""
        loc = "[global]" if c.file == sess.GLOBAL_FILE else f"{c.file}:{c.line}"
        lines.append(f"[{c.id}] {c.author} {c.severity} {loc}{stale}{resolved}")
        lines.append(f"  {c.body}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def signal(event: str) -> str:
    """Signal that you have completed a phase (e.g., "round-done").

    Args:
        event: Event name, typically "round-done" when finishing a round.
    """
    sd = _session_dir()
    agent = _get_author()
    polling.write_signal(sd, agent, event)
    return f"Signaled {agent}.{event}"


@mcp.tool()
def wait(event: str, timeout: int = 600) -> str:
    """Wait for the orchestrator to signal an event (e.g., "next-round").

    This blocks until the signal arrives or timeout expires.

    Args:
        event: Event name to wait for (e.g., "next-round")
        timeout: Maximum seconds to wait (default: 600)
    """
    sd = _session_dir()
    agent = _get_author()
    ok = polling.wait_signal(sd, agent, event, timeout=timeout)
    if ok:
        return f"Received {agent}.{event}"
    return f"Timeout after {timeout}s waiting for {agent}.{event}"


@mcp.tool()
def ask(question: str, timeout: int = 600) -> str:
    """Ask the orchestrator a question and block until they reply.

    Use this when you are blocked and cannot proceed without guidance.
    Prefer making reasonable assumptions over asking.

    Args:
        question: Your question text
        timeout: Maximum seconds to wait for reply (default: 600)
    """
    sd = _session_dir()
    agent = _get_author()
    q = polling.write_question(sd, agent, question)
    reply = polling.wait_reply(sd, agent, q.id, timeout=timeout)
    if reply:
        return f"Reply: {reply.answer}"
    return f"Timeout after {timeout}s waiting for reply to: {question}"


@mcp.tool()
def read_persona() -> str:
    """Read your persona file to understand your review style and priorities."""
    sd = _session_dir()
    agent = _get_author()
    persona_path = Path(sd) / "personas" / f"{agent}.md"
    if not persona_path.exists():
        return f"No persona file found at {persona_path}"
    return persona_path.read_text()


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
