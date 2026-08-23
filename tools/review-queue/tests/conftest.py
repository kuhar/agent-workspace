from __future__ import annotations

from pathlib import Path

import pytest

from review_queue.models import ProjectConfig, QueueConfig


@pytest.fixture
def project(tmp_path: Path) -> ProjectConfig:
    launcher = tmp_path / "review-pr.sh"
    launcher.write_text("#!/usr/bin/env bash\nexit 0\n")
    launcher.chmod(0o755)
    return ProjectConfig(
        name="rocjitsu",
        repo="ROCm/rocm-systems",
        query="draft:false team-review-requested:ROCm/rocjitsu-core-team",
        launcher=launcher,
        wrapper_root=tmp_path / "wrappers",
        estimated_wrapper_gib=1,
        priority=0,
    )


@pytest.fixture
def config(tmp_path: Path, project: ProjectConfig) -> QueueConfig:
    return QueueConfig(
        poll_seconds=60,
        push_quiet_seconds=0,
        bootstrap_hours=24,
        max_running=2,
        max_wrappers=3,
        min_free_gib=1,
        start_paused=False,
        projects=(project,),
        config_path=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
    )
