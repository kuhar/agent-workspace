"""Session creation, loading, discovery, and updates."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import AgentConfig, AgentStatus, GitHubPR, Session, SshTarget, _now_iso
from . import curator

META_FILE = "__meta__"
# Sentinel for "high-level / global" comments not tied to any file or line.
# Stored as file="" line=0 so existing JSONL data (where these fields
# defaulted to empty) is already compatible.
GLOBAL_FILE = ""


_VALID_ID_RE = __import__("re").compile(r"^[A-Za-z0-9_-]+$")


def _generate_session_id() -> str:
    now = datetime.now(timezone.utc)
    short = uuid.uuid4().hex[:4]
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{short}"


def _validate_session_id(sid: str) -> None:
    """Session ids become URL path segments — must be slug-safe and not
    collide with reserved web routes. Raises ValueError on bad input.
    """
    if not _VALID_ID_RE.match(sid):
        raise ValueError(
            f"invalid session id {sid!r}: only [A-Za-z0-9_-] allowed"
        )
    # Mirrors web/app.py:RESERVED_ROOTS — keep them in sync.
    if sid in {"api"}:
        raise ValueError(f"session id {sid!r} collides with a reserved route")


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


def _read_file_at_ref(workspace: str, ref: str, file: str) -> str:
    """Read a file from a commit without changing its line structure."""
    result = subprocess.run(
        ["git", "-C", workspace, "show", f"{ref}:{file}"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def resolve_git_ref(workspace: str, ref: str = "HEAD") -> str:
    """Resolve `ref` to a commit SHA in `workspace`."""
    return _run_git(workspace, "rev-parse", "--verify", f"{ref}^{{commit}}")


def normalize_repo_relative(repo_relative: str | os.PathLike[str] | None) -> str:
    """Normalize a path from workspace root to the reviewed Git repository."""
    if repo_relative is None:
        return ""
    raw = str(repo_relative).strip()
    if not raw or raw == ".":
        return ""
    path = Path(raw)
    if path.is_absolute():
        raise ValueError("repo-relative path must be relative")
    if ".." in path.parts:
        raise ValueError("repo-relative path must stay under workspace")
    return str(path)


def repo_path(session: Session) -> str:
    """Return the Git repository path for a session."""
    return session.repo_path()


def retarget_review_snapshot(
    session: Session,
    new_base: str,
    new_head: str,
) -> bool:
    """Move a session's active review diff to a resolved commit pair.

    `current_head` is the commit agents/comments are associated with, while
    `base_ref`, `topic_ref`, and `diff_commands` are what humans and agents use
    to render the active diff. They must move together during synchronization.
    """
    repo = repo_path(session)
    base_sha = resolve_git_ref(repo, new_base)
    head_sha = resolve_git_ref(repo, new_head)
    diff_range = f"{base_sha}...{head_sha}"
    diff_command = f"git diff {diff_range}"
    diff_stat = _run_git(repo, "diff", "--stat", diff_range)

    changed = False
    if session.base_ref != base_sha:
        session.base_ref = base_sha
        changed = True
    if session.current_head != head_sha:
        session.current_head = head_sha
        changed = True
    if session.topic_ref != head_sha:
        session.topic_ref = head_sha
        changed = True
    if session.diff_commands != [diff_command]:
        session.diff_commands = [diff_command]
        changed = True
    if session.diff_stat != diff_stat:
        session.diff_stat = diff_stat
        changed = True
    return changed


def retarget_review_head(session: Session, new_head: str) -> bool:
    """Move a session's active review head while keeping its pinned base."""
    return retarget_review_snapshot(session, session.base_ref, new_head)


def create_session(
    *,
    workspace: str,
    repo_relative: str | None = None,
    base_ref: str = "main",
    topic_ref: str = "HEAD",
    agents: list[dict] | None = None,
    ssh_targets: dict[str, dict] | None = None,
    personas_dir: str | None = None,
    timeout: int = 1200,
    session_dir: str | None = None,
    session_id: str | None = None,
    github: GitHubPR | None = None,
    include_curator: bool = False,
) -> tuple[Session, str]:
    """Create a new review session directory and session.json. Returns (session, session_dir).

    `session_id` overrides the auto-generated `<timestamp>-<hex4>` slug. It
    must be URL-safe (`[A-Za-z0-9_-]+`) and not collide with reserved web
    routes (currently `api`). When `github` is supplied, it is stamped onto
    the session as PR provenance — push/pull use it to know which PR to
    talk to.
    """
    if session_id is not None:
        _validate_session_id(session_id)
        sid = session_id
    else:
        sid = _generate_session_id()
    if session_dir is None:
        session_dir = f"/tmp/peanut-review/{sid}"
    sdir = Path(session_dir)

    # Create directory structure
    for subdir in ["comments", "notes", "signals", "prompts", "log"]:
        (sdir / subdir).mkdir(parents=True, exist_ok=True)

    # Copy personas
    if personas_dir:
        personas_dst = sdir / "personas"
        personas_dst.mkdir(exist_ok=True)
        src = Path(personas_dir)
        for f in src.glob("*.md"):
            shutil.copy2(f, personas_dst / f.name)

    workspace_abs = os.path.abspath(workspace)
    repo_rel = normalize_repo_relative(repo_relative)
    repo = str((Path(workspace_abs) / repo_rel).resolve()) if repo_rel else workspace_abs

    # Materialize the review snapshot. Symbolic refs are useful CLI inputs but
    # unsafe persisted state: a later fetch or branch switch could silently
    # change the rendered diff.
    base_sha = resolve_git_ref(repo, base_ref)
    head_sha = resolve_git_ref(repo, topic_ref)
    diff_range = f"{base_sha}...{head_sha}"
    diff_stat = _run_git(repo, "diff", "--stat", diff_range)

    # Build agent configs
    agent_configs = []
    if agents:
        for a in agents:
            agent_configs.append(AgentConfig.from_dict(a))
    target_configs = {
        name: SshTarget.from_dict(target)
        for name, target in (ssh_targets or {}).items()
    }
    for agent in agent_configs:
        if agent.ssh_target and agent.ssh_target not in target_configs:
            raise ValueError(
                f"agent {agent.name} references unknown SSH target {agent.ssh_target!r}"
            )
        if agent.ssh_target and curator.is_curator(agent):
            raise ValueError("curator agents cannot use an SSH target")
    if include_curator:
        curator.ensure_curator_agent(agent_configs)

    session = Session(
        id=sid,
        created_at=_now_iso(),
        workspace=workspace_abs,
        repo_relative=repo_rel,
        base_ref=base_sha,
        topic_ref=head_sha,
        original_head=head_sha,
        current_head=head_sha,
        diff_commands=[f"git diff {diff_range}"],
        diff_stat=diff_stat,
        agents=agent_configs,
        ssh_targets=target_configs,
        timeout=timeout,
        github=github,
    )

    save_session(sdir, session)
    return session, str(sdir)


def reviewer_agents(session: Session) -> list[AgentConfig]:
    return curator.reviewers(session.agents)


def curator_agents(session: Session) -> list[AgentConfig]:
    return curator.curators(session.agents)


def ensure_curator(session: Session) -> AgentConfig:
    return curator.ensure_curator_agent(session.agents)


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


@contextmanager
def _session_lock(session_dir: str | Path):
    """Serialize small session.json runtime updates across supervisors."""
    import fcntl

    path = Path(session_dir) / "session.json.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def sync_session_snapshot(
    session_dir: str | Path,
    *,
    base_ref: str,
    topic_ref: str,
    github: GitHubPR | None = None,
    workspace: str | os.PathLike[str] | None = None,
    repo_relative: str | os.PathLike[str] | None = None,
) -> tuple[Session, bool, bool]:
    """Atomically synchronize the persisted review workspace and snapshot.

    Returns `(session, head_changed, metadata_changed)`. Callers decide when
    a changed head should stale existing comments.
    """
    with _session_lock(session_dir):
        session = load_session(session_dir)
        old_head = session.current_head
        changed = False
        if workspace is not None:
            workspace_abs = os.path.abspath(workspace)
            repo_rel = normalize_repo_relative(repo_relative)
            if session.workspace != workspace_abs:
                session.workspace = workspace_abs
                changed = True
            if session.repo_relative != repo_rel:
                session.repo_relative = repo_rel
                changed = True
        changed = retarget_review_snapshot(session, base_ref, topic_ref) or changed
        if github is not None and session.github != github:
            session.github = github
            changed = True
        if changed:
            save_session(session_dir, session)
        return session, session.current_head != old_head, changed


def workspace_head(session: Session) -> str | None:
    """Return the checked-out workspace commit, or None when unavailable."""
    try:
        return resolve_git_ref(repo_path(session), "HEAD")
    except RuntimeError:
        return None


def workspace_head_mismatch(session: Session) -> tuple[bool, str | None]:
    """Report whether the checkout differs from the pinned review head."""
    live_head = workspace_head(session)
    return (
        live_head is not None and live_head != session.current_head,
        live_head,
    )


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


def update_agent_status(
    session_dir: str | Path,
    agent_name: str,
    status: str,
    pid: int | None = None,
    pgid: int | None = None,
    supervisor_pid: int | None = None,
) -> Session:
    """Update an agent's runtime status in session.json."""
    with _session_lock(session_dir):
        session = load_session(session_dir)
        for a in session.agents:
            if a.name == agent_name:
                a.status = status
                if pid is not None:
                    a.pid = pid
                if pgid is not None:
                    a.pgid = pgid
                if supervisor_pid is not None:
                    a.supervisor_pid = supervisor_pid
                break
        save_session(session_dir, session)
        return session


def reset_agent_runtime(
    session_dir: str | Path,
    agent_names: list[str],
) -> Session:
    """Clear persisted runtime identity for selected agents."""
    names = set(agent_names)
    with _session_lock(session_dir):
        session = load_session(session_dir)
        for a in session.agents:
            if a.name in names:
                a.status = AgentStatus.PENDING.value
                a.pid = None
                a.pgid = None
                a.supervisor_pid = None
        save_session(session_dir, session)
        return session


def _copy_session_fields(dst: Session, src: Session) -> None:
    dst.version = src.version
    dst.id = src.id
    dst.created_at = src.created_at
    dst.workspace = src.workspace
    dst.repo_relative = src.repo_relative
    dst.base_ref = src.base_ref
    dst.topic_ref = src.topic_ref
    dst.original_head = src.original_head
    dst.current_head = src.current_head
    dst.diff_commands = src.diff_commands
    dst.diff_stat = src.diff_stat
    dst.agents = src.agents
    dst.timeout = src.timeout
    dst.github = src.github
    dst.last_github_push_at = src.last_github_push_at


def record_github_push(session_dir: str | Path, pushed_at: str) -> Session:
    """Persist the latest successful GitHub push watermark atomically."""
    with _session_lock(session_dir):
        session = load_session(session_dir)
        if (
            session.last_github_push_at is None
            or pushed_at > session.last_github_push_at
        ):
            session.last_github_push_at = pushed_at
            save_session(session_dir, session)
        return session


def refresh_agent_statuses(session_dir: str | Path, session: Session) -> bool:
    """Refresh agent states from signals, live PIDs, and supervisor metadata."""
    from . import runtime

    changed = False
    with _session_lock(session_dir):
        latest = load_session(session_dir)
        for agent in latest.agents:
            snapshot = runtime.inspect_agent_runtime(session_dir, agent)
            new_status = runtime.derive_status_from_snapshot(agent, snapshot)
            if agent.status != new_status:
                agent.status = new_status
                changed = True
            for field in ("pid", "pgid", "supervisor_pid"):
                value = snapshot[field]
                if value is not None and getattr(agent, field) != value:
                    setattr(agent, field, value)
                    changed = True
        if changed:
            save_session(session_dir, latest)
        _copy_session_fields(session, latest)
    return changed


def validate_comment_location(
    repository: str,
    file: str,
    line: int,
    *,
    head_ref: str | None = None,
    require_pinned: bool = False,
) -> tuple[list[str] | None, str | None]:
    """Validate file/line for a comment. Returns (lines, error_message).

    For legacy __meta__ comments and global comments (file==""), returns
    (None, None) — no validation needed. New test/activity reports should use
    the separate note store instead of __meta__ comments.
    On success, returns (file_lines, None).
    On error, returns (None, error_string).
    """
    if file == META_FILE or file == GLOBAL_FILE:
        return None, None
    if line < 1:
        return None, f"line must be >= 1 for source files (got {line})"
    if head_ref:
        try:
            contents = _read_file_at_ref(repository, head_ref, file)
        except RuntimeError:
            if require_pinned:
                return None, f"file not found at review head {head_ref[:12]}: {file}"
        else:
            lines = contents.splitlines()
            if line > len(lines):
                return None, (
                    f"{file} has {len(lines)} lines but line {line} is out of range"
                )
            return lines, None

    if not head_ref or not require_pinned:
        file_path = Path(repository) / file
        if not file_path.exists():
            return None, f"file not found in repository: {file}"
        lines = file_path.read_text().splitlines()
    if line > len(lines):
        return None, f"{file} has {len(lines)} lines but line {line} is out of range"
    return lines, None
