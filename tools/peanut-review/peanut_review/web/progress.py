"""Agent-derived progress labels for the review UI."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def _field(agent: Any, name: str, default: str) -> str:
    if isinstance(agent, Mapping):
        value = agent.get(name, default)
    else:
        value = getattr(agent, name, default)
    return str(value or default)


def summarize_agent_progress(agents: Iterable[Any]) -> dict[str, str]:
    """Summarize the current reviewer/Curator activity for display."""
    items = list(agents)
    reviewers = [a for a in items if _field(a, "role", "reviewer") != "curator"]
    curators = [a for a in items if _field(a, "role", "reviewer") == "curator"]

    reviewer_statuses = [_field(a, "status", "pending") for a in reviewers]
    running = reviewer_statuses.count("running")
    if running:
        noun = "agent" if running == 1 else "agents"
        return {
            "label": f"{running} review {noun} running",
            "status": "running",
        }
    failed = sum(status in {"failed", "timeout"} for status in reviewer_statuses)
    if failed:
        noun = "agent" if failed == 1 else "agents"
        return {
            "label": f"{failed} review {noun} failed",
            "status": "failed",
        }

    pending = reviewer_statuses.count("pending")
    if pending:
        noun = "agent" if pending == 1 else "agents"
        return {
            "label": f"{pending} review {noun} pending",
            "status": "pending",
        }

    curator_statuses = {_field(a, "status", "pending") for a in curators}
    if "running" in curator_statuses:
        return {"label": "curator running", "status": "running"}
    if "done" in curator_statuses:
        return {"label": "curator done", "status": "done"}
    if curator_statuses & {"failed", "timeout"}:
        return {"label": "curator failed", "status": "failed"}

    if reviewers:
        noun = "agent" if len(reviewers) == 1 else "agents"
        return {"label": f"review {noun} done", "status": "done"}
    if curators:
        return {"label": "curator pending", "status": "pending"}
    return {"label": "no review agents", "status": "pending"}
