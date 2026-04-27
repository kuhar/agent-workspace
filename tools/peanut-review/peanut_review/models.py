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
    # Non-actionable observations: questions, praise, FYI notes, and
    # imported comments from external systems (e.g. GitHub) that carry no
    # severity of their own. NOT a fallback for "I'm not sure how serious"
    # — pick a real severity if you're asking for a change.
    FEEDBACK = "feedback"


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
    # External provenance — set when imported from or pushed to a remote
    # provider (currently only "github"). external_synced_body holds the body
    # at the last successful sync so we can detect local edits that need
    # PATCHing back. external_in_reply_to is the provider's reply pointer
    # (e.g. GitHub's in_reply_to_id) — kept around so reverse mappings during
    # pull don't have to scan all bodies.
    external_source: str | None = None
    external_id: str | None = None
    external_url: str | None = None
    external_in_reply_to: str | None = None
    external_synced_body: str | None = None
    # Edit history — `versions` stacks prior {body, severity, edited_at,
    # edited_by} snapshots in chronological order (versions[0] is the
    # original creator's state). edited_at/edited_by reflect the most recent
    # edit, or None on a never-edited comment.
    edited_at: str | None = None
    edited_by: str | None = None
    versions: list[dict] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        # Drop None / empty-list values for compactness on disk.
        d = {k: v for k, v in d.items() if v not in (None, [])}
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
class GitHubPR:
    """Provenance for a session backed by a GitHub PR.

    `head_sha`/`base_sha` are pinned at session creation; agent comments
    posted later use Session.current_head as their commit_id when pushed,
    not these. Stored to detect rebase/retarget after the fact.
    """
    repo: str = ""           # "owner/name"
    number: int = 0
    url: str = ""
    head_sha: str = ""
    base_sha: str = ""
    title: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, "", 0)}

    @classmethod
    def from_dict(cls, d: dict) -> GitHubPR:
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
    agents: list[AgentConfig] = field(default_factory=list)
    state: str = SessionState.INIT.value
    timeout: int = 1200
    github: GitHubPR | None = None

    def to_json(self) -> str:
        d = asdict(self)
        d["agents"] = [a.to_dict() for a in self.agents]
        if self.github is not None:
            d["github"] = self.github.to_dict()
        else:
            d.pop("github", None)
        d = {k: v for k, v in d.items() if v is not None}
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Session:
        d = json.loads(text)
        agents = [AgentConfig.from_dict(a) for a in d.pop("agents", [])]
        gh_raw = d.pop("github", None)
        filtered = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        s = cls(**filtered)
        s.agents = agents
        if gh_raw:
            s.github = GitHubPR.from_dict(gh_raw)
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
    # Set after a successful `gh-push-verdict` so re-runs are idempotent.
    external_review_id: str | None = None
    external_review_url: str | None = None

    def to_json(self) -> str:
        d = asdict(self)
        d = {k: v for k, v in d.items() if v not in (None, [])}
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> Verdict:
        d = json.loads(text)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
