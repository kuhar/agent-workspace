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
