"""Signal and wait primitives for reviewer coordination."""
from __future__ import annotations

import time
from pathlib import Path

from .models import _now_iso


def _signals_dir(session_dir: str | Path) -> Path:
    return Path(session_dir) / "signals"


# --- Signals ---

def write_signal(session_dir: str | Path, agent: str, event: str) -> Path:
    """Create a signal file. Returns the path."""
    path = _signals_dir(session_dir) / f"{agent}.{event}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_now_iso() + "\n")
    return path


def check_signal(session_dir: str | Path, agent: str, event: str) -> bool:
    """Check if a signal file exists."""
    return (_signals_dir(session_dir) / f"{agent}.{event}").exists()


def wait_signal(
    session_dir: str | Path,
    agent: str,
    event: str,
    timeout: int = 600,
    poll_interval: float = 2.0,
) -> bool:
    """Block until a signal file appears. Returns True if signaled, False on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check_signal(session_dir, agent, event):
            return True
        time.sleep(poll_interval)
    return False


def wait_all_signals(
    session_dir: str | Path,
    agents: list[str],
    event: str,
    timeout: int = 600,
    poll_interval: float = 2.0,
) -> list[str]:
    """Block until all agents signal. Returns list of agents that timed out."""
    deadline = time.monotonic() + timeout
    remaining = set(agents)
    while remaining and time.monotonic() < deadline:
        for agent in list(remaining):
            if check_signal(session_dir, agent, event):
                remaining.discard(agent)
        if remaining:
            time.sleep(poll_interval)
    return sorted(remaining)


def signal_all(session_dir: str | Path, agents: list[str], event: str) -> list[Path]:
    """Signal all agents with the given event."""
    return [write_signal(session_dir, agent, event) for agent in agents]


def wait_round_completion(
    session_dir: str | Path,
    agents: list[str],
    timeout: int = 600,
    poll_interval: float = 2.0,
) -> tuple[list[str], list[str]]:
    """Wait for every selected agent to finish; return failed and unfinished names."""
    from . import runtime, session
    from .models import AgentStatus

    deadline = time.monotonic() + timeout
    remaining = set(agents)
    failed = set()
    while remaining:
        current = {a.name: a for a in session.load_session(session_dir).agents}
        for name in sorted(remaining):
            agent = current.get(name)
            if agent is None:
                continue
            status = runtime.derive_agent_status(session_dir, agent)
            if status == AgentStatus.DONE.value:
                remaining.remove(name)
            elif status in {AgentStatus.FAILED.value, AgentStatus.TIMEOUT.value}:
                failed.add(name)
                remaining.remove(name)
        if not remaining or time.monotonic() >= deadline:
            break
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))
    return sorted(failed), sorted(remaining)
