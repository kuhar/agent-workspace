"""Tests for config and launch prerequisite validation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

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
                {
                    "name": "vera",
                    "model": "gpt-5.6-sol",
                    "reasoningEffort": "high",
                    "fastMode": False,
                    "persona": "vera.md",
                    "runner": "codex",
                },
                {"name": "irene", "model": "opus", "persona": "vera.md"},
            ],
        },
        config_path=config_path,
    )

    assert cfg["reviewRoot"] == str((tmp_path / "reviews").resolve())
    assert cfg["workspaceRoot"] == str((tmp_path / "worktree").resolve())
    assert cfg["workspace"] == str((tmp_path / "worktree").resolve())
    assert cfg["repoPath"] == str(workspace.resolve())
    assert cfg["personasDir"] == str(personas.resolve())
    assert cfg["agents"][0]["runner"] == "codex"
    assert cfg["agents"][0]["reasoningEffort"] == "high"
    assert cfg["agents"][0]["fastMode"] is False
    assert cfg["agents"][1]["runner"] == "cursor"


def test_validate_project_config_rejects_invalid_fast_mode(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "vera.md").write_text("persona\n")
    config_path = tmp_path / ".peanut-review.json"

    try:
        validation.validate_project_config(
            {
                "reviewRoot": "reviews",
                "workspaceRoot": ".",
                "repoRelative": "repo",
                "personasDir": "personas",
                "agents": [
                    {
                        "name": "vera",
                        "model": "opus",
                        "persona": "vera.md",
                        "runner": "cursor",
                        "fastMode": True,
                    },
                    {
                        "name": "irene",
                        "model": "gpt",
                        "persona": "vera.md",
                        "runner": "codex",
                        "fastMode": "off",
                    },
                ],
            },
            config_path=config_path,
        )
    except validation.ValidationError as e:
        message = str(e)
    else:
        raise AssertionError("expected validation error")

    assert "agents[0].fastMode is only supported with runner 'codex'" in message
    assert "agents[1].fastMode must be a boolean" in message


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


def test_validate_project_config_normalizes_ssh_targets(tmp_path: Path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "remote.md").write_text("persona\n")
    config = validation.validate_project_config(
        {
            "reviewRoot": "reviews",
            "workspaceRoot": ".",
            "repoRelative": "repo",
            "personasDir": "personas",
            "sshTargets": {
                "docs-host": {
                    "host": "reviewer@docs-host",
                    "controlPath": "/run/user/1000/peanut-docs.sock",
                    "gatewayUrl": "http://127.0.0.1:27184",
                    "workspaceRoot": "/srv/reviews/project",
                    "repoRelative": "source",
                    "buildRoots": ["/srv/reviews/project/build"],
                    "peanutReviewBin": "/opt/peanut/bin/peanut-review",
                },
            },
            "agents": [{
                "name": "remote",
                "model": "gpt",
                "persona": "remote.md",
                "runner": "codex",
                "sshTarget": "docs-host",
            }],
        },
        config_path=tmp_path / ".peanut-review.json",
    )
    assert config["agents"][0]["runner"] == "codex"
    assert config["agents"][0]["sshTarget"] == "docs-host"
    assert config["sshTargets"]["docs-host"] == {
        "host": "reviewer@docs-host",
        "controlPath": "/run/user/1000/peanut-docs.sock",
        "gatewayUrl": "http://127.0.0.1:27184",
        "workspaceRoot": "/srv/reviews/project",
        "repoRelative": "source",
        "buildRoots": ["/srv/reviews/project/build"],
        "peanutReviewBin": "/opt/peanut/bin/peanut-review",
        "runtimeRoot": "/tmp/peanut-review-ssh",
    }


@pytest.mark.parametrize("mutation, expected", [
    (("agent", "missing"), "references unknown target"),
    (("curator", "docs-host"), "not supported for curator"),
    (("gateway", "http://example.com:1234"), "remote loopback"),
    (("gateway", "http://127.0.0.1:80@outside.example"), "remote loopback"),
    (("control", "relative.sock"), "absolute remote POSIX path"),
    (("repo", "../source"), "stay under workspaceRoot"),
    (("build", []), "non-empty array"),
])
def test_validate_project_config_rejects_invalid_ssh_target(
    tmp_path: Path, mutation, expected: str,
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    personas = tmp_path / "personas"
    personas.mkdir()
    (personas / "remote.md").write_text("persona\n")
    target = {
        "host": "reviewer@host",
        "controlPath": "/tmp/control.sock",
        "gatewayUrl": "http://127.0.0.1:27184",
        "workspaceRoot": "/srv/project",
        "repoRelative": "source",
        "buildRoots": ["/srv/project/build"],
    }
    role = "reviewer"
    target_name = "docs-host"
    kind, value = mutation
    if kind == "agent":
        target_name = value
    elif kind == "curator":
        role = kind
    elif kind == "gateway":
        target["gatewayUrl"] = value
    elif kind == "control":
        target["controlPath"] = value
    elif kind == "repo":
        target["repoRelative"] = value
    elif kind == "build":
        target["buildRoots"] = value
    with pytest.raises(validation.ValidationError, match=expected):
        validation.validate_project_config(
            {
                "reviewRoot": "reviews",
                "workspaceRoot": ".",
                "repoRelative": "repo",
                "personasDir": "personas",
                "sshTargets": {"docs-host": target},
                "agents": [{
                    "name": "remote", "model": "gpt", "persona": "remote.md",
                    "runner": "codex", "role": role, "sshTarget": target_name,
                }],
            },
            config_path=tmp_path / ".peanut-review.json",
        )


def test_remote_cursor_does_not_require_local_cursor_permissions(tmp_path: Path):
    agent = AgentConfig(
        name="remote", model="cursor", persona="remote.md",
        runner="cursor", ssh_target="docs-host",
    )
    validation.validate_launch_prerequisites(workspace=tmp_path, agents=[agent])
