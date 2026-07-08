"""Curator agent helpers."""
from __future__ import annotations

from collections.abc import Sequence

from .models import AgentConfig, AgentRole


CURATOR_AGENT_NAME = "Curator"


def is_curator(agent: AgentConfig) -> bool:
    return getattr(agent, "role", AgentRole.REVIEWER.value) == AgentRole.CURATOR.value


def reviewers(agents: Sequence[AgentConfig]) -> list[AgentConfig]:
    return [agent for agent in agents if not is_curator(agent)]


def curators(agents: Sequence[AgentConfig]) -> list[AgentConfig]:
    return [agent for agent in agents if is_curator(agent)]


def ensure_curator_agent(agents: list[AgentConfig]) -> AgentConfig:
    existing = curators(agents)
    if existing:
        return existing[0]
    raise ValueError(
        "curator agent is not configured; add an agents[] entry with "
        '"role": "curator", a model, and a runner'
    )
