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
    round_num = sess.current_round(s.state)

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
        round=round_num,
        head_sha=s.current_head,
    )
    store.append_comment(sd, comment)

    if file_lines and line >= 1:
        return f"Comment {comment.id} stored. {file}:{line}: {file_lines[line - 1]}"
    return f"Comment {comment.id} stored."


@mcp.tool()
def list_comments(
    round_num: int | None = None,
    severity: str | None = None,
    file: str | None = None,
) -> str:
    """List review comments, optionally filtered.

    Args:
        round_num: Filter by round (1 or 2)
        severity: Filter by severity (critical, warning, suggestion, nit)
        file: Filter by file path
    """
    sd = _session_dir()
    comments = store.read_all_comments(sd)
    comments = store.filter_comments(
        comments, file=file, severity=severity, round_num=round_num,
    )
    if not comments:
        return "No comments found."

    lines = []
    for c in comments:
        stale = " [stale]" if c.stale else ""
        resolved = " [resolved]" if c.resolved else ""
        lines.append(f"[{c.id}] {c.author} {c.severity} {c.file}:{c.line} R{c.round}{stale}{resolved}")
        lines.append(f"  {c.body}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def signal(event: str) -> str:
    """Signal that you have completed a phase (e.g., "round1-done", "round2-done").

    Args:
        event: Event name, typically "round1-done" or "round2-done"
    """
    sd = _session_dir()
    agent = _get_author()
    polling.write_signal(sd, agent, event)
    return f"Signaled {agent}.{event}"


@mcp.tool()
def wait(event: str, timeout: int = 600) -> str:
    """Wait for the orchestrator to signal an event (e.g., "triage-done").

    This blocks until the signal arrives or timeout expires.

    Args:
        event: Event name to wait for (e.g., "triage-done")
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
def read_triage() -> str:
    """Read the triage decisions from Round 1 (available after triage-done signal).

    Returns the triage JSON showing which comments were applied vs dismissed,
    with descriptions and rebuttals.
    """
    sd = _session_dir()
    triage_path = Path(sd) / "triage.json"
    if not triage_path.exists():
        return "No triage.json found yet. Wait for the triage-done signal first."
    return triage_path.read_text()


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
