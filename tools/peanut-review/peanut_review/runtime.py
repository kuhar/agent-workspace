"""Runtime inspection for launched reviewer agents."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import store
from .models import AgentConfig, AgentStatus


ROUND_DONE_EVENT = "round-done"

PROCESS_PENDING = "pending"
PROCESS_LAUNCHING = "launching"
PROCESS_RUNNING = "running"
PROCESS_EXITED = "exited"
PROCESS_FAILED = "failed"
PROCESS_TIMEOUT = "timeout"
PROCESS_KILLED = "killed"
PROCESS_STOPPED = "stopped"

PROTOCOL_PENDING = "pending"
PROTOCOL_DONE = "done"

_TERMINAL_PROCESS_STATES = {
    PROCESS_EXITED,
    PROCESS_FAILED,
    PROCESS_TIMEOUT,
    PROCESS_KILLED,
    PROCESS_STOPPED,
}


def agent_log_dir(session_dir: str | Path, agent_name: str) -> Path:
    return Path(session_dir) / "log" / agent_name


def agent_meta_path(session_dir: str | Path, agent_name: str) -> Path:
    return agent_log_dir(session_dir, agent_name) / "meta.json"


def read_agent_meta(session_dir: str | Path, agent_name: str) -> dict[str, Any]:
    path = agent_meta_path(session_dir, agent_name)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def update_agent_meta(
    session_dir: str | Path,
    agent_name: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Merge runtime fields into log/<agent>/meta.json atomically."""
    path = agent_meta_path(session_dir, agent_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_agent_meta(session_dir, agent_name)
    data.update(updates)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return data


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_process_live(pid: int | None) -> bool:
    """Return true for an existing non-zombie process."""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    stat_path = Path("/proc") / str(pid) / "stat"
    if stat_path.exists():
        try:
            stat = stat_path.read_text()
            # comm may contain spaces and is wrapped in parens; the state is
            # the first token after the closing paren.
            state = stat.rsplit(")", 1)[1].strip().split()[0]
        except (OSError, IndexError):
            return True
        if state in {"Z", "X"}:
            return False
    return True


def _signal_path(session_dir: str | Path, agent_name: str, event: str) -> Path:
    return Path(session_dir) / "signals" / f"{agent_name}.{event}"


def has_round_done_signal(session_dir: str | Path, agent_name: str) -> bool:
    return _signal_path(session_dir, agent_name, ROUND_DONE_EVENT).exists()


def agent_comment_count(session_dir: str | Path, agent_name: str) -> int:
    return sum(1 for c in store.read_agent_comments(session_dir, agent_name) if not c.deleted)


def process_state_from_exit(
    return_code: int,
    *,
    timed_out: bool = False,
    termination_signal: str | None = None,
) -> str:
    """Classify process completion from supervisor-owned facts."""
    if timed_out:
        return PROCESS_TIMEOUT
    if termination_signal:
        return PROCESS_KILLED
    if return_code == 0:
        return PROCESS_EXITED
    return PROCESS_FAILED


def _meta_process_state(meta: dict[str, Any]) -> str | None:
    state = meta.get("process_state")
    return state if isinstance(state, str) and state else None


def _derive_process_state(
    *,
    agent: AgentConfig,
    meta: dict[str, Any],
    pid: int | None,
    supervisor_pid: int | None,
    reviewer_live: bool,
    supervisor_live: bool,
    exit_code: int | None,
    timed_out: bool,
    termination_signal: str | None,
    has_final_meta: bool,
) -> str:
    """Derive the supervisor/process axis without consulting review signals."""
    if reviewer_live:
        return PROCESS_RUNNING

    if supervisor_live and not has_final_meta:
        state = _meta_process_state(meta)
        if state in {PROCESS_LAUNCHING, PROCESS_RUNNING}:
            return state
        return PROCESS_RUNNING if pid else PROCESS_LAUNCHING

    if timed_out:
        return PROCESS_TIMEOUT

    if exit_code is not None:
        state = _meta_process_state(meta)
        if state in _TERMINAL_PROCESS_STATES:
            return state
        return process_state_from_exit(
            exit_code,
            timed_out=timed_out,
            termination_signal=termination_signal,
        )

    if agent.status == AgentStatus.PENDING.value and not (pid or supervisor_pid or meta):
        return PROCESS_PENDING

    state = _meta_process_state(meta)
    if state in _TERMINAL_PROCESS_STATES:
        return state
    if state == PROCESS_PENDING and not (pid or supervisor_pid):
        return PROCESS_PENDING

    return PROCESS_FAILED


def inspect_agent_runtime(session_dir: str | Path, agent: AgentConfig) -> dict[str, Any]:
    meta = read_agent_meta(session_dir, agent.name)
    pid = agent.pid if agent.pid is not None else _as_int(meta.get("pid"))
    pgid = agent.pgid if agent.pgid is not None else _as_int(meta.get("pgid"))
    supervisor_pid = (
        agent.supervisor_pid
        if agent.supervisor_pid is not None
        else _as_int(meta.get("supervisor_pid"))
    )
    exit_code = _as_int(meta.get("exit_code"))
    timed_out = bool(meta.get("timed_out"))
    termination_signal = meta.get("termination_signal")
    termination_signal = termination_signal if isinstance(termination_signal, str) else None
    signal = has_round_done_signal(session_dir, agent.name)
    reviewer_live = is_process_live(pid)
    supervisor_live = is_process_live(supervisor_pid)
    has_final_meta = any(k in meta for k in ("end", "exit_code", "timed_out"))
    process_state = _derive_process_state(
        agent=agent,
        meta=meta,
        pid=pid,
        supervisor_pid=supervisor_pid,
        reviewer_live=reviewer_live,
        supervisor_live=supervisor_live,
        exit_code=exit_code,
        timed_out=timed_out,
        termination_signal=termination_signal,
        has_final_meta=has_final_meta,
    )
    protocol_status = PROTOCOL_DONE if signal else PROTOCOL_PENDING
    return {
        "meta": meta,
        "pid": pid,
        "pgid": pgid,
        "supervisor_pid": supervisor_pid,
        "reviewer_live": reviewer_live,
        "supervisor_live": supervisor_live,
        "process_state": process_state,
        "protocol_status": protocol_status,
        "signal": signal,
        "comments": agent_comment_count(session_dir, agent.name),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "termination_signal": termination_signal,
        "has_final_meta": has_final_meta,
    }


def derive_status_from_snapshot(agent: AgentConfig, snapshot: dict[str, Any]) -> str:
    if snapshot["protocol_status"] == PROTOCOL_DONE:
        return AgentStatus.DONE.value
    if snapshot["process_state"] == PROCESS_TIMEOUT:
        return AgentStatus.TIMEOUT.value
    if snapshot["process_state"] in {PROCESS_LAUNCHING, PROCESS_RUNNING}:
        return AgentStatus.RUNNING.value
    if snapshot["process_state"] == PROCESS_PENDING:
        return AgentStatus.PENDING.value
    return AgentStatus.FAILED.value


def derive_agent_status(session_dir: str | Path, agent: AgentConfig) -> str:
    return derive_status_from_snapshot(agent, inspect_agent_runtime(session_dir, agent))


def compact_model(model: str, width: int = 22) -> str:
    if len(model) <= width:
        return model
    if width <= 3:
        return model[:width]
    return model[: width - 3] + "..."


def status_detail_parts(snapshot: dict[str, Any], status: str) -> list[str]:
    parts: list[str] = []
    if snapshot["pid"] and (snapshot["reviewer_live"] or status == AgentStatus.RUNNING.value):
        parts.append(f"pid={snapshot['pid']}")
    if snapshot["pgid"] and (snapshot["reviewer_live"] or status == AgentStatus.RUNNING.value):
        parts.append(f"pgid={snapshot['pgid']}")
    if (
        snapshot["supervisor_pid"]
        and snapshot["supervisor_live"]
        and not snapshot["reviewer_live"]
    ):
        parts.append(f"supervisor={snapshot['supervisor_pid']}")
    if snapshot["timed_out"]:
        parts.append("timed_out=yes")
    if snapshot["exit_code"] is not None and not snapshot["reviewer_live"]:
        parts.append(f"exit={snapshot['exit_code']}")
    if snapshot["termination_signal"]:
        parts.append(f"term={snapshot['termination_signal']}")
    parts.append(f"signal={'yes' if snapshot['signal'] else 'no'}")
    parts.append(f"comments={snapshot['comments']}")
    return parts
