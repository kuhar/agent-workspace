"""Session lifecycle — create, load, discover, update."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import AgentConfig, AgentStatus, Session, SessionState, _now_iso

META_FILE = "__meta__"
# Sentinel for "high-level / global" comments not tied to any file or line.
# Stored as file="" line=0 so existing JSONL data (where these fields
# defaulted to empty) is already compatible.
GLOBAL_FILE = ""


def _generate_session_id() -> str:
    now = datetime.now(timezone.utc)
    short = uuid.uuid4().hex[:4]
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{short}"


def _run_git(workspace: str, *args: str) -> str:
    """Run a git command in the workspace, return stdout stripped.

    Raises RuntimeError on non-zero exit.
    """
    result = subprocess.run(
        ["git", "-C", workspace, *args],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def create_session(
    *,
    workspace: str,
    base_ref: str = "main",
    topic_ref: str = "HEAD",
    agents: list[dict] | None = None,
    personas_dir: str | None = None,
    timeout: int = 1200,
    session_dir: str | None = None,
) -> tuple[Session, str]:
    """Create a new review session directory and session.json. Returns (session, session_dir)."""
    sid = _generate_session_id()
    if session_dir is None:
        session_dir = f"/tmp/peanut-review/{sid}"
    sdir = Path(session_dir)

    # Create directory structure
    for subdir in ["comments", "signals", "messages", "prompts", "log"]:
        (sdir / subdir).mkdir(parents=True, exist_ok=True)

    # Copy personas
    if personas_dir:
        personas_dst = sdir / "personas"
        personas_dst.mkdir(exist_ok=True)
        src = Path(personas_dir)
        for f in src.glob("*.md"):
            shutil.copy2(f, personas_dst / f.name)

    # Resolve git info
    head_sha = _run_git(workspace, "rev-parse", "HEAD")
    diff_stat = _run_git(workspace, "diff", "--stat", f"{base_ref}...{topic_ref}")

    # Build agent configs
    agent_configs = []
    if agents:
        for a in agents:
            agent_configs.append(AgentConfig.from_dict(a))

    session = Session(
        id=sid,
        created_at=_now_iso(),
        workspace=os.path.abspath(workspace),
        base_ref=base_ref,
        topic_ref=topic_ref,
        original_head=head_sha,
        current_head=head_sha,
        diff_commands=[f"git diff {base_ref}...{topic_ref}"],
        diff_stat=diff_stat,
        agents=agent_configs,
        state=SessionState.INIT.value,
        timeout=timeout,
    )

    save_session(sdir, session)
    return session, str(sdir)


def save_session(session_dir: str | Path, session: Session) -> None:
    """Write session.json atomically."""
    sdir = Path(session_dir)
    tmp = sdir / "session.json.tmp"
    dst = sdir / "session.json"
    tmp.write_text(session.to_json() + "\n")
    tmp.replace(dst)


def load_session(session_dir: str | Path) -> Session:
    """Load session.json from a session directory."""
    path = Path(session_dir) / "session.json"
    return Session.from_json(path.read_text())


def discover_session(start_path: str | Path | None = None) -> str | None:
    """Find session dir from $PEANUT_SESSION or .peanut-session marker."""
    env = os.environ.get("PEANUT_SESSION")
    if env:
        return env
    if start_path:
        p = Path(start_path)
        for d in [p] + list(p.parents):
            marker = d / ".peanut-session"
            if marker.exists():
                return marker.read_text().strip()
    return None


def transition_state(session_dir: str | Path, new_state: str) -> Session:
    """Load session, update state, save, and return it."""
    session = load_session(session_dir)
    session.state = new_state
    save_session(session_dir, session)
    return session


def update_agent_status(
    session_dir: str | Path, agent_name: str, status: str, pid: int | None = None
) -> Session:
    """Update an agent's status (and optionally PID) in session.json."""
    session = load_session(session_dir)
    for a in session.agents:
        if a.name == agent_name:
            a.status = status
            if pid is not None:
                a.pid = pid
            break
    save_session(session_dir, session)
    return session


def current_round(state: str) -> int:
    """Return the review round (1 or 2) based on session state."""
    return 2 if state == SessionState.ROUND2.value else 1


def refresh_agent_statuses(session_dir: str | Path, session: Session) -> bool:
    """Check PIDs of running agents, mark exited ones as done. Returns True if changed."""
    changed = False
    for agent in session.agents:
        if agent.status != AgentStatus.RUNNING.value or not agent.pid:
            continue
        try:
            os.kill(agent.pid, 0)
        except ProcessLookupError:
            agent.status = AgentStatus.DONE.value
            changed = True
        except PermissionError:
            pass
    if changed:
        save_session(session_dir, session)
    return changed


def validate_comment_location(
    workspace: str, file: str, line: int,
) -> tuple[list[str] | None, str | None]:
    """Validate file/line for a comment. Returns (lines, error_message).

    For __meta__ files and global comments (file==""), returns (None, None)
    — no validation needed.
    On success, returns (file_lines, None).
    On error, returns (None, error_string).
    """
    if file == META_FILE or file == GLOBAL_FILE:
        return None, None
    file_path = Path(workspace) / file
    if not file_path.exists():
        return None, f"file not found in workspace: {file}"
    if line < 1:
        return None, f"line must be >= 1 for source files (got {line})"
    lines = file_path.read_text().splitlines()
    if line > len(lines):
        return None, f"{file} has {len(lines)} lines but line {line} is out of range"
    return lines, None
