from __future__ import annotations

from pathlib import Path

import pytest

from review_queue.config import load_config


def test_load_config_expands_and_validates(tmp_path: Path) -> None:
    launcher = tmp_path / "review-pr.sh"
    launcher.touch()
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[queue]
poll_seconds = 10
push_quiet_seconds = 0
bootstrap_hours = 12
max_running = 2
max_wrappers = 3
min_free_gib = 4
start_paused = true
start_mode = "manual"

[ui]
theme = "monokai"

[[projects]]
name = "demo"
repo = "owner/repo"
query = "draft:false"
launcher = "{launcher}"
wrapper_root = "{tmp_path / "wrappers"}"
estimated_wrapper_gib = 5
priority = 7
include_paths = ["emulation"]
"""
    )
    loaded = load_config(config, state_dir=tmp_path / "state")
    assert loaded.start_paused
    assert loaded.start_mode == "manual"
    assert loaded.push_quiet_seconds == 0
    assert loaded.projects[0].priority == 7
    assert loaded.projects[0].launcher == launcher
    assert loaded.theme == "monokai"
    assert loaded.projects[0].include_paths == ("emulation/",)


def test_config_rejects_duplicate_projects(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    project = f"""
[[projects]]
name = "demo"
repo = "owner/repo"
query = "draft:false"
launcher = "{tmp_path / "review-pr.sh"}"
wrapper_root = "{tmp_path / "wrappers"}"
"""
    config.write_text("[queue]\n" + project + project)
    with pytest.raises(ValueError, match="duplicate project"):
        load_config(config)


def test_default_poll_interval_is_two_minutes(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
[[projects]]
name = "demo"
repo = "owner/repo"
query = "draft:false"
launcher = "{tmp_path / "review-pr.sh"}"
wrapper_root = "{tmp_path / "wrappers"}"
"""
    )

    assert load_config(config).poll_seconds == 120


@pytest.mark.parametrize(
    "value, expected",
    [
        (["emulation"], ("emulation/",)),
        (["emulation/", "emulation"], ("emulation/",)),
        ([], ()),
    ],
)
def test_include_paths_normalizes_directories(value, expected):
    from review_queue.config import _include_paths

    assert _include_paths(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "emulation/",
        [1],
        ["/emulation"],
        ["../emulation"],
        ["emulation/../docs"],
        ["emulation//foo"],
        ["emulation/*"],
        [""],
        ["."],
    ],
)
def test_include_paths_rejects_invalid_directories(value):
    from review_queue.config import _include_paths

    with pytest.raises(ValueError):
        _include_paths(value)
