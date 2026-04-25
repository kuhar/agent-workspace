"""Signal/wait/ask/reply primitives for agent communication."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .models import Question, Reply, _now_iso


def _signals_dir(session_dir: str | Path) -> Path:
    return Path(session_dir) / "signals"


def _messages_dir(session_dir: str | Path, agent: str) -> Path:
    return Path(session_dir) / "messages" / agent


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


# --- Messages (ask/reply) ---

def _next_question_id(msg_dir: Path) -> str:
    """Find the next question number."""
    existing = sorted(msg_dir.glob("q_*.json"))
    if not existing:
        return "q_001"
    last = existing[-1].stem  # e.g. q_003
    num = int(last.split("_")[1]) + 1
    return f"q_{num:03d}"


def write_question(
    session_dir: str | Path, agent: str, question_text: str
) -> Question:
    """Write a question file atomically (O_CREAT|O_EXCL to avoid TOCTOU race)."""
    msg_dir = _messages_dir(session_dir, agent)
    msg_dir.mkdir(parents=True, exist_ok=True)
    # Read glob once, then increment counter on collisions
    existing = sorted(msg_dir.glob("q_*.json"))
    next_num = int(existing[-1].stem.split("_")[1]) + 1 if existing else 1
    for attempt in range(100):
        qid = f"q_{next_num + attempt:03d}"
        q = Question(id=qid, agent=agent, timestamp=_now_iso(), question=question_text)
        path = msg_dir / f"{qid}.json"
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, (q.to_json() + "\n").encode())
            finally:
                os.close(fd)
            return q
        except FileExistsError:
            continue
    raise RuntimeError(f"Could not allocate question ID after 100 attempts for agent {agent}")


def wait_reply(
    session_dir: str | Path,
    agent: str,
    question_id: str,
    timeout: int = 600,
    poll_interval: float = 2.0,
) -> Reply | None:
    """Block until a reply file appears for the given question. Returns Reply or None."""
    msg_dir = _messages_dir(session_dir, agent)
    reply_path = msg_dir / f"{question_id}.reply"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reply_path.exists():
            try:
                return Reply.from_json(reply_path.read_text())
            except (json.JSONDecodeError, ValueError):
                pass  # file exists but not fully written yet; retry
        time.sleep(poll_interval)
    return None


def write_reply(
    session_dir: str | Path,
    agent: str,
    question_id: str,
    answer: str,
    answered_by: str = "orchestrator",
) -> Reply:
    """Write a reply file, unblocking the agent's `ask`."""
    msg_dir = _messages_dir(session_dir, agent)
    msg_dir.mkdir(parents=True, exist_ok=True)
    r = Reply(answered_by=answered_by, timestamp=_now_iso(), answer=answer)
    (msg_dir / f"{question_id}.reply").write_text(r.to_json() + "\n")
    return r


def list_transcript(session_dir: str | Path) -> list[dict]:
    """Return every question + (optional) reply pair across all agents.

    Each entry is `{"agent", "id", "timestamp", "question", "reply"}` where
    `reply` is `None` if the agent is still waiting, else the Reply dict.
    Sorted by question timestamp ascending so the UI reads top-to-bottom in
    the order things actually happened.
    """
    msgs = Path(session_dir) / "messages"
    if not msgs.exists():
        return []
    out: list[dict] = []
    for d in sorted(msgs.iterdir()):
        if not d.is_dir():
            continue
        for qf in sorted(d.glob("q_*.json")):
            try:
                q = Question.from_json(qf.read_text())
            except (json.JSONDecodeError, TypeError):
                continue
            entry = {
                "agent": d.name,
                "id": q.id,
                "timestamp": q.timestamp,
                "question": q.question,
                "reply": None,
            }
            reply_path = qf.with_suffix(".reply")
            if reply_path.exists():
                try:
                    r = Reply.from_json(reply_path.read_text())
                    entry["reply"] = {
                        "answered_by": r.answered_by,
                        "timestamp": r.timestamp,
                        "answer": r.answer,
                    }
                except (json.JSONDecodeError, ValueError):
                    pass  # leave reply=None if file is half-written
            out.append(entry)
    out.sort(key=lambda e: e["timestamp"])
    return out


def list_unanswered(session_dir: str | Path, agent: str | None = None) -> list[Question]:
    """List all unanswered questions, optionally filtered by agent."""
    msgs = Path(session_dir) / "messages"
    if not msgs.exists():
        return []
    questions = []
    dirs = [msgs / agent] if agent else sorted(msgs.iterdir())
    for d in dirs:
        if not d.is_dir():
            continue
        for qf in sorted(d.glob("q_*.json")):
            reply_path = qf.with_suffix(".reply")
            if not reply_path.exists():
                try:
                    questions.append(Question.from_json(qf.read_text()))
                except (json.JSONDecodeError, TypeError):
                    pass
    return questions
