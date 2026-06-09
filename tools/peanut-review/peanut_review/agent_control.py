"""Runtime control for launched reviewer agents."""
from __future__ import annotations

import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from . import runtime, session as sess


SUPPORTED_RUNNERS = {"cursor", "opencode", "codex", "claude"}
DEFAULT_KILL_GRACE_SECONDS = 5.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _normalize_agent_names(agent_names: Sequence[str] | None) -> list[str] | None:
    if agent_names is None:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for raw in agent_names:
        name = raw.strip()
        if not name or name in seen:
            continue
        result.append(name)
        seen.add(name)
    return result


def _select_agents(agents, agent_names: Sequence[str] | None):
    requested = _normalize_agent_names(agent_names)
    if requested is None:
        return list(agents)

    available = {agent.name for agent in agents}
    unknown = [name for name in requested if name not in available]
    if unknown:
        known = ", ".join(agent.name for agent in agents) or "<none>"
        raise ValueError(
            f"unknown agent(s): {', '.join(unknown)} "
            f"(available: {known})"
        )

    requested_set = set(requested)
    return [agent for agent in agents if agent.name in requested_set]


def _read_proc_environ(pid: int) -> dict[str, str] | None:
    path = Path("/proc") / str(pid) / "environ"
    try:
        raw = path.read_bytes()
    except OSError:
        return None

    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode(errors="replace")] = value.decode(errors="replace")
    return env


def _same_session_path(left: str | None, right: str | Path) -> bool:
    if not left:
        return False
    if left == str(right):
        return True
    try:
        return Path(left).resolve() == Path(right).resolve()
    except OSError:
        return False


def _process_matches_agent(
    pid: int,
    *,
    session_dir: str | Path,
    agent_name: str,
    force: bool = False,
) -> tuple[bool, str]:
    """Return whether pid is safe to signal for this session agent."""
    if force:
        return True, "force"

    env = _read_proc_environ(pid)
    if env is None:
        return False, f"cannot verify pid {pid} environment"

    if not _same_session_path(env.get("PEANUT_SESSION"), session_dir):
        return False, f"pid {pid} does not match PEANUT_SESSION"
    if env.get("GIT_AUTHOR_NAME") != agent_name:
        return False, f"pid {pid} does not match GIT_AUTHOR_NAME={agent_name}"
    return True, "environment matched"


def _get_pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except ProcessLookupError:
        return None
    except OSError:
        return None


def _wait_dead(pids: list[int], timeout: float) -> list[int]:
    deadline = time.monotonic() + max(timeout, 0.0)
    remaining = [pid for pid in pids if runtime.is_process_live(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = [pid for pid in remaining if runtime.is_process_live(pid)]
    return remaining


def _signal_process_group(pgid: int, sig: signal.Signals) -> None:
    os.killpg(pgid, sig)


def _signal_process(pid: int, sig: signal.Signals) -> None:
    os.kill(pid, sig)


def _safe_reviewer_group(
    *,
    session_dir: str | Path,
    agent_name: str,
    pid: int,
    pgid: int | None,
    force: bool,
) -> tuple[int | None, str | None]:
    ok, reason = _process_matches_agent(
        pid,
        session_dir=session_dir,
        agent_name=agent_name,
        force=force,
    )
    if not ok:
        return None, reason

    current_pgid = _get_pgid(pid)
    target_pgid = pgid or current_pgid
    if target_pgid is None:
        return None, f"could not resolve process group for pid {pid}"
    if current_pgid is not None and pgid is not None and current_pgid != pgid and not force:
        return None, f"pid {pid} moved from recorded pgid {pgid} to {current_pgid}"
    if target_pgid == os.getpgrp() and not force:
        return None, f"refusing to signal current process group {target_pgid}"
    return target_pgid, None


def _safe_process(
    *,
    session_dir: str | Path,
    agent_name: str,
    pid: int,
    force: bool,
) -> str | None:
    if pid == os.getpid() and not force:
        return f"refusing to signal current process pid {pid}"
    ok, reason = _process_matches_agent(
        pid,
        session_dir=session_dir,
        agent_name=agent_name,
        force=force,
    )
    return None if ok else reason


def _mark_agent_killed(
    session_dir: str | Path,
    agent,
    snapshot: dict[str, Any],
    result: dict[str, Any],
) -> None:
    now = _now_iso()
    last_signal = result["signals"][-1]["signal"] if result["signals"] else None
    runtime.update_agent_meta(
        session_dir,
        agent.name,
        {
            "kill_requested_at": now,
            "kill_signals": result["signals"],
            "process_state": runtime.PROCESS_KILLED,
            "termination_signal": last_signal,
            "timed_out": False,
            "end": now,
            "pid": snapshot["pid"],
            "pgid": snapshot["pgid"],
            "supervisor_pid": snapshot["supervisor_pid"],
        },
    )
    status = runtime.derive_agent_status(session_dir, agent)
    sess.update_agent_status(
        session_dir,
        agent.name,
        status,
        pid=snapshot["pid"],
        pgid=snapshot["pgid"],
        supervisor_pid=snapshot["supervisor_pid"],
    )


def _signal_reviewer_group(
    *,
    result: dict[str, Any],
    pgid: int,
    sig: signal.Signals,
    dry_run: bool,
) -> None:
    result["signals"].append({"target": "pgid", "id": pgid, "signal": sig.name})
    if not dry_run:
        _signal_process_group(pgid, sig)


def _signal_supervisor(
    *,
    result: dict[str, Any],
    pid: int,
    sig: signal.Signals,
    dry_run: bool,
) -> None:
    result["signals"].append({"target": "supervisor", "id": pid, "signal": sig.name})
    if not dry_run:
        _signal_process(pid, sig)


def _runner_name(agent, snapshot: dict[str, Any]) -> str:
    meta_runner = snapshot["meta"].get("runner")
    return meta_runner if isinstance(meta_runner, str) and meta_runner else agent.runner


def _kill_one_agent(
    *,
    session_dir: Path,
    agent,
    grace_seconds: float,
    dry_run: bool,
    force: bool,
) -> dict[str, Any]:
    snapshot = runtime.inspect_agent_runtime(session_dir, agent)
    runner = _runner_name(agent, snapshot)
    result: dict[str, Any] = {
        "name": agent.name,
        "runner": runner,
        "status": "pending",
        "reason": "",
        "pid": snapshot["pid"],
        "pgid": snapshot["pgid"],
        "supervisor_pid": snapshot["supervisor_pid"],
        "process_state": snapshot["process_state"],
        "signals": [],
    }

    if runner not in SUPPORTED_RUNNERS:
        result["status"] = "error"
        result["reason"] = f"unsupported runner: {runner}"
        return result

    reviewer_pid = snapshot["pid"]
    supervisor_pid = snapshot["supervisor_pid"]
    reviewer_live = bool(reviewer_pid and runtime.is_process_live(reviewer_pid))
    supervisor_live = bool(supervisor_pid and runtime.is_process_live(supervisor_pid))

    if not reviewer_live and not supervisor_live:
        result["status"] = "skipped"
        result["reason"] = "not running"
        return result

    if reviewer_live:
        pgid, error = _safe_reviewer_group(
            session_dir=session_dir,
            agent_name=agent.name,
            pid=reviewer_pid,
            pgid=snapshot["pgid"],
            force=force,
        )
        if error:
            result["status"] = "error"
            result["reason"] = error
            return result
        assert pgid is not None
        _signal_reviewer_group(
            result=result,
            pgid=pgid,
            sig=signal.SIGTERM,
            dry_run=dry_run,
        )
        if dry_run:
            result["status"] = "dry-run"
            return result

        remaining = _wait_dead([reviewer_pid], grace_seconds)
        if remaining:
            _signal_reviewer_group(
                result=result,
                pgid=pgid,
                sig=signal.SIGKILL,
                dry_run=False,
            )
            remaining = _wait_dead([reviewer_pid], grace_seconds)
        if remaining:
            result["status"] = "error"
            result["reason"] = f"pid {reviewer_pid} still live after SIGKILL"
            return result

        if supervisor_pid and runtime.is_process_live(supervisor_pid):
            _wait_dead([supervisor_pid], grace_seconds)

    if supervisor_pid and runtime.is_process_live(supervisor_pid):
        error = _safe_process(
            session_dir=session_dir,
            agent_name=agent.name,
            pid=supervisor_pid,
            force=force,
        )
        if error:
            result["status"] = "error"
            result["reason"] = error
            return result
        _signal_supervisor(
            result=result,
            pid=supervisor_pid,
            sig=signal.SIGTERM,
            dry_run=dry_run,
        )
        if dry_run:
            result["status"] = "dry-run"
            return result

        remaining = _wait_dead([supervisor_pid], grace_seconds)
        if remaining:
            _signal_supervisor(
                result=result,
                pid=supervisor_pid,
                sig=signal.SIGKILL,
                dry_run=False,
            )
            remaining = _wait_dead([supervisor_pid], grace_seconds)
        if remaining:
            result["status"] = "error"
            result["reason"] = f"supervisor pid {supervisor_pid} still live after SIGKILL"
            return result

    if dry_run:
        result["status"] = "dry-run"
        return result

    result["status"] = "killed"
    _mark_agent_killed(session_dir, agent, snapshot, result)
    return result


def kill_agents(
    session_dir: str | Path,
    *,
    agent_names: Sequence[str] | None = None,
    grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
    dry_run: bool = False,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Terminate selected launched agents using recorded runtime metadata."""
    sdir = Path(session_dir)
    session = sess.load_session(sdir)
    agents = _select_agents(session.agents, agent_names)
    return [
        _kill_one_agent(
            session_dir=sdir,
            agent=agent,
            grace_seconds=grace_seconds,
            dry_run=dry_run,
            force=force,
        )
        for agent in agents
    ]
