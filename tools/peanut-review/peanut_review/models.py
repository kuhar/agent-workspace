"""Data models for peanut-review."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _now_iso() -> str:
    # Microsecond precision so two comments posted in the same wall-clock
    # second by different authors still get a strict ordering. The store
    # merges per-author JSONL files by sorting on this timestamp, and the
    # `--since <id>` cursor relies on that order being deterministic.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _short_id(prefix: str = "c") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    SUGGESTION = "suggestion"
    NIT = "nit"


class SessionState(str, Enum):
    INIT = "init"
    ROUND = "round"
    COMPLETE = "complete"
    ABORTED = "aborted"


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Comment:
    id: str = field(default_factory=lambda: _short_id("c"))
    type: str = "comment"
    author: str = ""
    timestamp: str = field(default_factory=_now_iso)
    file: str = ""
    line: int = 0
    end_line: int | None = None
    side: str = "right"
    body: str = ""
    severity: str = Severity.SUGGESTION.value
    resolved: bool = False
    resolved_by: str | None = None
    resolved_at: str | None = None
    stale: bool = False
    head_sha: str | None = None
    # Soft delete — hides the comment from agents and the UI by default, but
    # the record is retained so the audit trail (and any `--include-deleted`
    # view) can show that the comment existed.
    deleted: bool = False
    deleted_by: str | None = None
    deleted_at: str | None = None
    # Threading — replies point at a top-level comment id. Replies do not
    # nest: setting reply_to on a reply silently re-roots to its parent's
    # parent, so trees are at most one level deep (GitHub-style).
    reply_to: str | None = None

    def to_json(self) -> str:
        d = asdict(self)
        # Drop None values for compactness
        d = {k: v for k, v in d.items() if v is not None}
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> Comment:
        d = json.loads(line)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AgentConfig:
    name: str = ""
    model: str = ""
    persona: str = ""
    status: str = AgentStatus.PENDING.value
    pid: int | None = None
    # Backend runner: "cursor" (cursor-agent) or "opencode" (opencode via lcode).
    runner: str = "cursor"
    # For runner="opencode": which lcode GPU pair to use. None → "qwen"/"null".
    lcode_primary: str | None = None
    lcode_subagent: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict) -> AgentConfig:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Session:
    version: int = 1
    id: str = ""
    created_at: str = field(default_factory=_now_iso)
    workspace: str = ""
    base_ref: str = "main"
    topic_ref: str = "HEAD"
    original_head: str = ""
    current_head: str = ""
    diff_commands: list[str] = field(default_factory=list)
    diff_stat: str = ""
    bead_id: str | None = None
    agents: list[AgentConfig] = field(default_factory=list)
    state: str = SessionState.INIT.value
    timeout: int = 1200

    def to_json(self) -> str:
        d = asdict(self)
        d["agents"] = [a.to_dict() for a in self.agents]
        d = {k: v for k, v in d.items() if v is not None}
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Session:
        d = json.loads(text)
        agents = [AgentConfig.from_dict(a) for a in d.pop("agents", [])]
        filtered = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        s = cls(**filtered)
        s.agents = agents
        return s


@dataclass
class Question:
    id: str = ""
    agent: str = ""
    timestamp: str = field(default_factory=_now_iso)
    question: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Question:
        d = json.loads(text)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Reply:
    answered_by: str = "orchestrator"
    timestamp: str = field(default_factory=_now_iso)
    answer: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Reply:
        d = json.loads(text)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Verdict:
    decision: str = ""  # "approve" or "request-changes"
    body: str = ""
    timestamp: str = field(default_factory=_now_iso)
    agents_summary: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)
