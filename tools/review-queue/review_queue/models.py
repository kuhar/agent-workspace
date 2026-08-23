from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    name: str
    repo: str
    query: str
    launcher: Path
    wrapper_root: Path
    estimated_wrapper_gib: int = 160
    priority: int = 0


@dataclass(frozen=True, slots=True)
class QueueConfig:
    poll_seconds: int
    push_quiet_seconds: int
    bootstrap_hours: int
    max_running: int
    max_wrappers: int
    min_free_gib: int
    start_paused: bool
    projects: tuple[ProjectConfig, ...]
    config_path: Path
    state_dir: Path
    theme: str = "catppuccin-mocha"


@dataclass(frozen=True, slots=True)
class PullRequest:
    project: str
    repo: str
    number: int
    url: str
    title: str
    author: str
    head_sha: str
    head_ref: str
    updated_at: str
    state: str = "OPEN"
    is_draft: bool = False


@dataclass(frozen=True, slots=True)
class QueueItem:
    job_id: int
    project: str
    repo: str
    number: int
    url: str
    title: str
    author: str
    head_sha: str
    head_ref: str
    updated_at: str
    status: str
    score: int
    project_priority: int
    author_priority: int
    pr_priority: int
    queued_at: str
    quiet_until: str | None
    wrapper_path: str | None = None
    error: str | None = None
    review_count: int = 0


@dataclass(frozen=True, slots=True)
class RunView:
    job_id: int
    project: str
    repo: str
    number: int
    title: str
    author: str
    head_sha: str
    status: str
    started_at: str | None
    wrapper_path: str | None
    log_path: str | None
    error: str | None
    review_count: int = 0


@dataclass(frozen=True, slots=True)
class WrapperView:
    wrapper_id: int
    project: str
    repo: str
    number: int
    path: str
    state: str
    pinned: bool
    last_used_at: str
    size_bytes: int
    cleanup_error: str | None
    title: str = ""
    author: str = ""
    review_count: int = 0


@dataclass(frozen=True, slots=True)
class ProjectHealth:
    name: str
    stale: bool
    last_poll_at: str | None
    last_success_at: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    paused: bool
    dispatch_blocked_reason: str | None
    max_running: int
    max_wrappers: int
    free_bytes: int
    queue: tuple[QueueItem, ...] = field(default_factory=tuple)
    running: tuple[RunView, ...] = field(default_factory=tuple)
    wrappers: tuple[WrapperView, ...] = field(default_factory=tuple)
    projects: tuple[ProjectHealth, ...] = field(default_factory=tuple)
