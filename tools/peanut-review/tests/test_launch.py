"""Tests for the launcher dispatch (cursor vs opencode)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from peanut_review import launch
from peanut_review.models import AgentConfig, Session


def _mock_git(workspace, *args):
    if args == ("rev-parse", "HEAD"):
        return "abc123def456"
    if args[0] == "diff" and "--stat" in args:
        return "+1 -0 1 file"
    return ""


def _make_session_dir(agents: list[AgentConfig]) -> str:
    from peanut_review.session import create_session

    sd = os.path.join(tempfile.mkdtemp(prefix="pr-launch-"), "session")
    with patch("peanut_review.session._run_git", side_effect=_mock_git):
        create_session(
            workspace="/tmp/fakerepo",
            agents=[a.to_dict() for a in agents],
            session_dir=sd,
        )
    return sd


def test_find_launcher_script_cursor():
    path = launch._find_launcher_script("cursor")
    assert path.endswith("cursor-agent-task.sh")
    assert Path(path).exists()


def test_find_launcher_script_opencode():
    path = launch._find_launcher_script("opencode")
    assert path.endswith("opencode-agent-task.sh")
    assert Path(path).exists()


def test_find_launcher_script_rejects_unknown_runner():
    try:
        launch._find_launcher_script("claude")
    except ValueError as e:
        assert "claude" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown runner")


def test_agent_config_defaults_to_cursor():
    a = AgentConfig(name="vera", model="opus", persona="vera.md")
    assert a.runner == "cursor"
    assert a.lcode_primary is None
    assert a.lcode_subagent is None


def test_launch_dry_run_cursor_agent_cmd():
    sd = _make_session_dir([AgentConfig(name="vera", model="opus-4.6-thinking", persona="vera.md")])
    results = launch.launch_agents(sd, dry_run=True)
    assert len(results) == 1
    cmd = results[0]["cmd"]
    assert cmd[0].endswith("cursor-agent-task.sh")
    assert "--model" in cmd and "opus-4.6-thinking" in cmd
    # cursor path must NOT carry lcode args
    assert "--lcode-primary" not in cmd
    assert "--lcode-subagent" not in cmd


def test_launch_dry_run_opencode_agent_cmd():
    sd = _make_session_dir([
        AgentConfig(
            name="felix", model="llama-primary/qwen3.6-35b-a3b", persona="felix.md",
            runner="opencode",
        ),
    ])
    results = launch.launch_agents(sd, dry_run=True)
    assert len(results) == 1
    cmd = results[0]["cmd"]
    assert cmd[0].endswith("opencode-agent-task.sh")
    assert "--model" in cmd and "llama-primary/qwen3.6-35b-a3b" in cmd
    # Defaults for lcode pair
    assert cmd[cmd.index("--lcode-primary") + 1] == "qwen"
    assert cmd[cmd.index("--lcode-subagent") + 1] == "null"


def test_launch_dry_run_opencode_respects_custom_lcode_pair():
    sd = _make_session_dir([
        AgentConfig(
            name="petra", model="llama-primary/gemma4-31b", persona="petra.md",
            runner="opencode", lcode_primary="gemma", lcode_subagent="qwen",
        ),
    ])
    results = launch.launch_agents(sd, dry_run=True)
    cmd = results[0]["cmd"]
    assert cmd[cmd.index("--lcode-primary") + 1] == "gemma"
    assert cmd[cmd.index("--lcode-subagent") + 1] == "qwen"


def test_launch_dry_run_mixed_runners():
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus-4.6-thinking", persona="vera.md"),
        AgentConfig(
            name="felix", model="llama-primary/qwen3.6-35b-a3b", persona="felix.md",
            runner="opencode",
        ),
    ])
    results = launch.launch_agents(sd, dry_run=True)
    assert len(results) == 2
    assert results[0]["cmd"][0].endswith("cursor-agent-task.sh")
    assert results[1]["cmd"][0].endswith("opencode-agent-task.sh")


def test_opencode_agent_uses_cli_prompt_template():
    """Opencode should always get the CLI prompt (MCP not wired up yet)."""
    sd = _make_session_dir([
        AgentConfig(
            name="felix", model="llama-primary/qwen3.6-35b-a3b", persona="felix.md",
            runner="opencode",
        ),
    ])
    prompts = launch.render_all_prompts(sd)
    rendered = prompts["felix"].read_text()
    # CLI template self-identifies by instructing the agent to execute shell commands.
    assert "Shell tool" in rendered
    # The MCP template mentions MCP tool names like add_comment (not Shell).
    assert "mcp__peanut-review" not in rendered


def test_session_roundtrip_preserves_runner_fields():
    from peanut_review import session as sess
    sd = _make_session_dir([
        AgentConfig(
            name="felix", model="llama-primary/qwen3.6-35b-a3b", persona="felix.md",
            runner="opencode", lcode_primary="qwen", lcode_subagent="gemma",
        ),
    ])
    s = sess.load_session(sd)
    assert s.agents[0].runner == "opencode"
    assert s.agents[0].lcode_primary == "qwen"
    assert s.agents[0].lcode_subagent == "gemma"
