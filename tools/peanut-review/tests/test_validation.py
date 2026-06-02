"""Tests for config and launch prerequisite validation."""
from __future__ import annotations

import json
from pathlib import Path

from peanut_review import validation
from peanut_review.models import AgentConfig


def _write_cli_json(workspace: Path, *, allow=None, deny=None) -> Path:
    cursor_dir = workspace / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    path = cursor_dir / "cli.json"
    path.write_text(json.dumps({
        "permissions": {
            "allow": ["Shell(peanut-review **)"] if allow is None else allow,
            "deny": ["Write(**)"] if deny is None else deny,
        }
    }))
    return path


def test_validate_project_config_normalizes_paths(tmp_path: Path):
    workspace = tmp_path / "worktree" / "repo"
    workspace.mkdir(parents=True)
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "vera.md").write_text("persona\n")
    config_path = tmp_path / ".peanut-review.json"

    cfg = validation.validate_project_config(
        {
            "reviewRoot": "reviews",
            "workspaceRoot": "worktree",
            "repoRelative": "repo",
            "personasDir": "personas",
            "agents": [
                {"name": "vera", "model": "opus", "persona": "vera.md"},
            ],
        },
        config_path=config_path,
    )

    assert cfg["reviewRoot"] == str((tmp_path / "reviews").resolve())
    assert cfg["workspaceRoot"] == str((tmp_path / "worktree").resolve())
    assert cfg["workspace"] == str((tmp_path / "worktree").resolve())
    assert cfg["repoPath"] == str(workspace.resolve())
    assert cfg["personasDir"] == str(personas.resolve())
    assert cfg["agents"][0]["runner"] == "cursor"


def test_validate_project_config_reports_actionable_errors(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    personas = tmp_path / "personas"
    personas.mkdir()
    config_path = tmp_path / ".peanut-review.json"

    try:
        validation.validate_project_config(
            {
                "reviewRoot": "reviews",
                "workspaceRoot": ".",
                "repoRelative": "../repo",
                "personasDir": "personas",
                "reviewAgentTimeoutSeconds": 0,
                "agents": [
                    {
                        "name": "vera",
                        "model": "opus",
                        "persona": "missing.md",
                        "runner": "cursor",
                    },
                    {
                        "name": "vera",
                        "model": "gpt",
                        "persona": "missing.md",
                        "runner": "unknown",
                    },
                ],
            },
            config_path=config_path,
        )
    except validation.ValidationError as e:
        message = str(e)
    else:
        raise AssertionError("expected validation error")

    assert "repoRelative must stay under workspaceRoot" in message
    assert "reviewAgentTimeoutSeconds must be a positive integer" in message
    assert "duplicate agent name: vera" in message
    assert "persona not found" in message
    assert "unsupported" in message


def test_validate_cursor_cli_json_rejects_missing_file(tmp_path: Path):
    try:
        validation.validate_cursor_cli_json(tmp_path)
    except validation.ValidationError as e:
        message = str(e)
    else:
        raise AssertionError("expected validation error")

    assert ".cursor/cli.json" in message
    assert "not found" in message


def test_validate_cursor_cli_json_rejects_unsafe_permissions(tmp_path: Path):
    _write_cli_json(tmp_path, allow=["Shell(git status **)"], deny=["Shell(**)"])

    try:
        validation.validate_cursor_cli_json(tmp_path)
    except validation.ValidationError as e:
        message = str(e)
    else:
        raise AssertionError("expected validation error")

    assert "Shell(peanut-review **)" in message
    assert "Shell(**)" in message


def test_validate_cursor_cli_json_rejects_non_workspace_override(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    other = tmp_path / "other-cli.json"
    other.write_text(json.dumps({
        "permissions": {
            "allow": ["Shell(peanut-review **)"],
            "deny": ["Write(**)"],
        }
    }))

    try:
        validation.validate_cursor_cli_json(workspace, cli_json=other)
    except validation.ValidationError as e:
        message = str(e)
    else:
        raise AssertionError("expected validation error")

    assert "must point to Cursor's workspace config" in message


def test_validate_launch_prerequisites_checks_cursor_config(tmp_path: Path):
    agents = [
        AgentConfig(name="vera", model="opus", persona="vera.md", runner="cursor"),
    ]

    try:
        validation.validate_launch_prerequisites(workspace=tmp_path, agents=agents)
    except validation.ValidationError as e:
        assert "Launch configuration validation failed" in str(e)
    else:
        raise AssertionError("expected validation error")

    _write_cli_json(tmp_path)
    validation.validate_launch_prerequisites(workspace=tmp_path, agents=agents)
