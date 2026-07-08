"""Tests for the launcher dispatch (cursor vs opencode)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from peanut_review import launch
from peanut_review.models import AgentConfig, AgentRole, Comment


def _mock_git(workspace, *args):
    if args == ("rev-parse", "HEAD"):
        return "abc123def456"
    if args[0] == "diff" and "--stat" in args:
        return "+1 -0 1 file"
    return ""


def _write_cursor_config(workspace: Path, allow=None, deny=None) -> None:
    cursor_dir = workspace / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    (cursor_dir / "cli.json").write_text(json.dumps({
        "permissions": {
            "allow": ["Shell(peanut-review **)"] if allow is None else allow,
            "deny": ["Write(**)"] if deny is None else deny,
        }
    }))


def _default_workspace() -> str:
    workspace = Path(tempfile.mkdtemp(prefix="pr-launch-workspace-"))
    _write_cursor_config(workspace)
    return str(workspace)


def _make_session_dir(
    agents: list[AgentConfig],
    workspace: str | None = None,
    repo_relative: str | None = None,
) -> str:
    from peanut_review.session import create_session

    sd = os.path.join(tempfile.mkdtemp(prefix="pr-launch-"), "session")
    with patch("peanut_review.session._run_git", side_effect=_mock_git):
        create_session(
            workspace=workspace or _default_workspace(),
            repo_relative=repo_relative,
            agents=[a.to_dict() for a in agents],
            session_dir=sd,
        )
    return sd


def _workspace_with_cursor_config(tmp_path: Path) -> str:
    workspace = tmp_path / "repo"
    _write_cursor_config(workspace)
    return str(workspace)


class DummyProc:
    def __init__(self, pid: int = 424242):
        self.pid = pid


def test_find_launcher_script_cursor():
    path = launch._find_launcher_script("cursor")
    assert path.endswith("cursor-agent-task.sh")
    assert Path(path).parent.name == "runners"
    assert "ask-the-peanut-gallery" not in path
    assert Path(path).exists()


def test_find_launcher_script_opencode():
    path = launch._find_launcher_script("opencode")
    assert path.endswith("opencode-agent-task.sh")
    assert Path(path).parent.name == "runners"
    assert "ask-the-peanut-gallery" not in path
    assert Path(path).exists()


def test_find_launcher_script_codex():
    path = launch._find_launcher_script("codex")
    assert path.endswith("codex-agent-task.sh")
    assert Path(path).parent.name == "runners"
    assert "ask-the-peanut-gallery" not in path
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


def test_launch_dry_run_cursor_agent_cmd():
    sd = _make_session_dir([AgentConfig(name="vera", model="opus-4.6-thinking", persona="vera.md")])
    results = launch.launch_agents(sd, dry_run=True)
    assert len(results) == 1
    cmd = results[0]["cmd"]
    assert cmd[0].endswith("cursor-agent-task.sh")
    assert "--model" in cmd and "opus-4.6-thinking" in cmd


def test_launch_uses_workspace_root_for_cursor_and_nested_repo_for_prompt(tmp_path):
    workspace = tmp_path / "review"
    repo = workspace / "rocm-systems"
    repo.mkdir(parents=True)
    (workspace / "build").mkdir()
    (workspace / "build" / "compile_commands.json").write_text("[]\n")
    (workspace / "build-clang-asan").mkdir()
    (workspace / "venv").mkdir()
    (workspace / "compile_commands.json").symlink_to(workspace / "build" / "compile_commands.json")
    _write_cursor_config(workspace)
    sd = _make_session_dir(
        [AgentConfig(name="vera", model="opus", persona="vera.md")],
        workspace=str(workspace),
        repo_relative="rocm-systems",
    )

    results = launch.launch_agents(sd, dry_run=True)

    cmd = results[0]["cmd"]
    assert cmd[cmd.index("--workspace") + 1] == str(workspace)
    supervisor_cmd = results[0]["supervisor_cmd"]
    assert supervisor_cmd[supervisor_cmd.index("--cwd") + 1] == str(workspace)
    rendered = (Path(sd) / "prompts" / "vera.md").read_text()
    assert f"Workspace: `{workspace}`" in rendered
    assert f"Repository: `{repo}`" in rendered
    assert "Workspace is the runner/build/tool root" in rendered
    assert "Do not assume `build/` is inside Repository" in rendered
    assert f"- `{workspace / 'build'}`" in rendered
    assert f"- `{workspace / 'build-clang-asan'}`" in rendered
    assert f"Compilation database: `{workspace / 'compile_commands.json'}` -> `{workspace / 'build' / 'compile_commands.json'}`" in rendered
    assert f"Python venv: `{workspace / 'venv'}`" in rendered
    assert f"git -C {repo} diff" in rendered
    assert f"cd {repo}" not in rendered


def test_launch_dry_run_opencode_agent_cmd():
    sd = _make_session_dir([
        AgentConfig(
            name="felix", model="openai/gpt-5.5", persona="felix.md",
            runner="opencode",
        ),
    ])
    results = launch.launch_agents(sd, dry_run=True)
    assert len(results) == 1
    cmd = results[0]["cmd"]
    assert cmd[0].endswith("opencode-agent-task.sh")
    assert "--model" in cmd and "openai/gpt-5.5" in cmd


def test_launch_dry_run_mixed_runners():
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus-4.6-thinking", persona="vera.md"),
        AgentConfig(
            name="felix", model="openai/gpt-5.5", persona="felix.md",
            runner="opencode",
        ),
    ])
    results = launch.launch_agents(sd, dry_run=True)
    assert len(results) == 2
    assert results[0]["cmd"][0].endswith("cursor-agent-task.sh")
    assert results[1]["cmd"][0].endswith("opencode-agent-task.sh")


def test_launch_dry_run_can_target_single_agent():
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus-4.6-thinking", persona="vera.md"),
        AgentConfig(
            name="felix", model="openai/gpt-5.5", persona="felix.md",
            runner="opencode",
        ),
    ])

    results = launch.launch_agents(sd, dry_run=True, agent_names=["felix"])

    assert [r["name"] for r in results] == ["felix"]
    assert results[0]["cmd"][0].endswith("opencode-agent-task.sh")
    assert (Path(sd) / "prompts" / "felix.md").exists()
    assert not (Path(sd) / "prompts" / "vera.md").exists()


def test_launch_default_excludes_curator_and_curate_uses_dedicated_prompt(tmp_path):
    workspace = _workspace_with_cursor_config(tmp_path)
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
        AgentConfig(
            name="Curator", model="opus",
            role=AgentRole.CURATOR.value,
        ),
    ], workspace=workspace)

    reviewer_results = launch.launch_agents(sd, dry_run=True)
    curator_results = launch.launch_curator(sd, dry_run=True)

    assert [r["name"] for r in reviewer_results] == ["vera"]
    assert [r["name"] for r in curator_results] == ["Curator"]
    reviewer_prompt = (Path(sd) / "prompts" / "vera.md").read_text()
    curator_prompt = (Path(sd) / "prompts" / "Curator.md").read_text()
    assert "Read your persona" in reviewer_prompt
    assert "comment curator" in curator_prompt
    assert "Reviewer agents: `vera`" in curator_prompt
    assert "Optimize for a small, high-signal final comment set" in curator_prompt
    assert "collapse similar low-level findings into one concise global comment" in curator_prompt
    assert curator_prompt.index("Optimize for a small") < curator_prompt.index(
        "Classify reviewer comments as"
    )
    assert "friendly, conversational reviewer voice" in curator_prompt
    assert "choose the opening shape from the finding's confidence" in curator_prompt
    assert "We should ..." in curator_prompt
    assert "Should we ..." in curator_prompt
    assert "The risk here is that ..." in curator_prompt
    assert "If I am reading this right, ..." in curator_prompt
    assert "I'm not sure if this works when ..." in curator_prompt
    assert "Can we make this ...?" in curator_prompt
    assert "do not use question form as the default" in curator_prompt
    assert "Record one brief summary in agent activity" in curator_prompt
    assert "note --message" in curator_prompt
    assert "Read your persona" not in curator_prompt


def test_launch_curator_requires_configured_curator():
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
    ])

    with pytest.raises(ValueError, match="curator agent is not configured"):
        launch.launch_curator(sd, dry_run=True)


def test_launch_rejects_unknown_target_agent():
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus-4.6-thinking", persona="vera.md"),
    ])

    try:
        launch.launch_agents(sd, dry_run=True, agent_names=["irene"])
    except ValueError as e:
        assert "unknown agent" in str(e)
        assert "vera" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown agent")


def test_reviewer_launch_records_curation_baseline_and_resets_curator(tmp_path):
    from peanut_review import polling, runtime, session as sess, store

    workspace = _workspace_with_cursor_config(tmp_path)
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
        AgentConfig(
            name="Curator", model="opus",
            role=AgentRole.CURATOR.value,
        ),
    ], workspace=workspace)
    existing = Comment(author="gh:alice", body="already imported")
    store.append_comment(sd, existing)
    polling.write_signal(sd, "Curator", "round-done")
    (Path(sd) / "signals" / "Curator.auto-launching").write_text("old\n")
    runtime.update_agent_meta(sd, "Curator", {
        "process_state": "exited",
        "exit_code": 0,
        "pid": 999999,
    })

    with patch("peanut_review.launch.subprocess.Popen", return_value=DummyProc()):
        results = launch.launch_agents(sd)

    assert [r["name"] for r in results] == ["vera"]
    stored = sess.load_session(sd)
    assert stored.curation_since_comment_id == existing.id
    assert not (Path(sd) / "signals" / "Curator.round-done").exists()
    assert not (Path(sd) / "signals" / "Curator.auto-launching").exists()
    curator_agent = next(a for a in stored.agents if a.name == "Curator")
    assert curator_agent.status == "pending"
    assert curator_agent.pid is None


def test_launch_dry_run_codex_agent_cmd():
    sd = _make_session_dir([
        AgentConfig(name="cleo", model="gpt-5.5", persona="vera.md", runner="codex"),
    ])
    results = launch.launch_agents(sd, dry_run=True)
    assert len(results) == 1
    cmd = results[0]["cmd"]
    assert cmd[0].endswith("codex-agent-task.sh")
    assert "--model" in cmd and "gpt-5.5" in cmd
    # Codex needs unrestricted writes because workspace-write + --add-dir is
    # still read-only for the out-of-workspace session dir in current Codex.
    assert "--sandbox" in cmd and "danger-full-access" in cmd
    assert "--add-dir" in cmd
    assert sd in cmd


def test_launch_uses_python_supervisor_for_non_dry_run():
    sd = _make_session_dir([
        AgentConfig(
            name="felix", model="openai/gpt-5.5", persona="felix.md",
            runner="opencode",
        ),
    ])

    with patch("peanut_review.launch.subprocess.Popen", return_value=DummyProc()) as popen:
        results = launch.launch_agents(sd)

    assert results[0]["pid"] is None
    assert results[0]["supervisor_pid"] == 424242
    supervisor_cmd = popen.call_args.args[0]
    assert supervisor_cmd[:3] == [sys.executable, "-m", "peanut_review.supervisor"]
    assert "--session" in supervisor_cmd and sd in supervisor_cmd
    separator = supervisor_cmd.index("--")
    assert supervisor_cmd[separator + 1].endswith("opencode-agent-task.sh")

    from peanut_review import session as sess
    stored = sess.load_session(sd)
    assert stored.agents[0].status == "running"
    assert stored.agents[0].pid is None
    assert stored.agents[0].supervisor_pid == 424242


def test_rerun_resets_only_selected_agent_round_state():
    from peanut_review import polling, runtime, session as sess

    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
        AgentConfig(name="irene", model="opus", persona="irene.md"),
    ])
    polling.write_signal(sd, "vera", "round-done")
    polling.write_signal(sd, "irene", "round-done")
    polling.write_signal(sd, "irene", "next-round")
    runtime.update_agent_meta(sd, "irene", {
        "process_state": "exited",
        "pid": 999999999,
        "pgid": 999999999,
        "exit_code": 0,
    })
    stored = sess.load_session(sd)
    stored.agents[1].status = "done"
    stored.agents[1].pid = 999999999
    stored.agents[1].pgid = 999999999
    stored.agents[1].supervisor_pid = 999999998
    sess.save_session(sd, stored)

    with patch("peanut_review.launch.subprocess.Popen", return_value=DummyProc()):
        results = launch.rerun_agents(sd, agent_names=["irene"])

    assert [r["name"] for r in results] == ["irene"]
    sigs = Path(sd) / "signals"
    assert (sigs / "vera.round-done").exists()
    assert not (sigs / "irene.round-done").exists()
    assert not (sigs / "irene.next-round").exists()
    assert not runtime.agent_meta_path(sd, "irene").exists()

    stored = sess.load_session(sd)
    irene = next(a for a in stored.agents if a.name == "irene")
    assert irene.status == "running"
    assert irene.pid is None
    assert irene.pgid is None
    assert irene.supervisor_pid == 424242


def test_rerun_refuses_live_agent(monkeypatch):
    from peanut_review import runtime, session as sess

    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
    ])
    runtime.update_agent_meta(sd, "vera", {
        "process_state": "running",
        "pid": 123456,
    })
    stored = sess.load_session(sd)
    stored.agents[0].status = "running"
    stored.agents[0].pid = 123456
    sess.save_session(sd, stored)
    monkeypatch.setattr(
        "peanut_review.runtime.is_process_live",
        lambda pid: pid == 123456,
    )

    with patch("peanut_review.launch.subprocess.Popen") as popen:
        try:
            launch.rerun_agents(sd, agent_names=["vera"])
        except ValueError as e:
            assert "cannot rerun live agent" in str(e)
            assert "vera" in str(e)
        else:
            raise AssertionError("expected ValueError for live rerun")

    popen.assert_not_called()


def test_cursor_agents_get_isolated_homes_without_mcp_config(tmp_path):
    workspace = _workspace_with_cursor_config(tmp_path)
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
        AgentConfig(name="irene", model="opus", persona="irene.md"),
    ], workspace=workspace)

    with (
        patch.dict(os.environ, {
            "HOME": "/home/original",
            "XDG_CONFIG_HOME": "/home/original/.config",
        }, clear=False),
        patch("peanut_review.launch.subprocess.Popen",
              side_effect=[DummyProc(111), DummyProc(222)]) as popen,
        patch("peanut_review.launch.time.sleep"),
    ):
        results = launch.launch_agents(sd)

    assert [r["name"] for r in results] == ["vera", "irene"]
    vera_env = popen.call_args_list[0].kwargs["env"]
    irene_env = popen.call_args_list[1].kwargs["env"]
    assert vera_env["HOME"] == str(Path(sd) / "runtime" / "cursor" / "vera")
    assert irene_env["HOME"] == str(Path(sd) / "runtime" / "cursor" / "irene")
    assert vera_env["HOME"] != irene_env["HOME"]
    assert vera_env["CURSOR_CONFIG_DIR"] == str(Path(vera_env["HOME"]) / ".cursor")
    assert vera_env["CURSOR_DATA_DIR"] == vera_env["CURSOR_CONFIG_DIR"]
    assert irene_env["CURSOR_CONFIG_DIR"] == str(Path(irene_env["HOME"]) / ".cursor")
    assert irene_env["CURSOR_DATA_DIR"] == irene_env["CURSOR_CONFIG_DIR"]
    assert vera_env["XDG_CONFIG_HOME"] == "/home/original/.config"
    assert irene_env["XDG_CONFIG_HOME"] == "/home/original/.config"
    assert vera_env["PEANUT_CURSOR_HOME"] == results[0]["cursor_home"]
    assert irene_env["PEANUT_CURSOR_HOME"] == results[1]["cursor_home"]
    assert "PEANUT_CURSOR_MCP_CONFIG" not in vera_env
    assert "PEANUT_CURSOR_MCP_CONFIG" not in irene_env
    assert "mcp_config" not in results[0]
    assert "mcp_config" not in results[1]
    assert not (Path(results[0]["cursor_home"]) / ".cursor" / "mcp.json").exists()
    assert not (Path(results[1]["cursor_home"]) / ".cursor" / "mcp.json").exists()


def test_cursor_launch_fails_when_cli_json_missing(tmp_path):
    workspace = str(tmp_path / "repo")
    Path(workspace).mkdir()
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
    ], workspace=workspace)

    with patch("peanut_review.launch.subprocess.Popen") as popen:
        try:
            launch.launch_agents(sd)
        except ValueError as e:
            message = str(e)
        else:
            raise AssertionError("expected validation error")

    assert "Cursor CLI config validation failed" in message
    assert ".cursor/cli.json" in message
    popen.assert_not_called()


def test_cursor_launch_does_not_manage_workspace_mcp_config(tmp_path):
    workspace = _workspace_with_cursor_config(tmp_path)
    workspace_mcp = Path(workspace) / ".cursor" / "mcp.json"
    original = json.dumps({
        "mcpServers": {
            "peanut-review": {
                "command": "custom-peanut-review-server",
                "env": {
                    "PEANUT_SESSION": "/old/session",
                    "GIT_AUTHOR_NAME": "old-agent",
                },
            },
            "unrelated": {
                "command": "example-mcp",
                "env": {"KEEP": "1"},
            },
        }
    }, indent=2) + "\n"
    workspace_mcp.write_text(original)
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
    ], workspace=workspace)

    with patch("peanut_review.launch.subprocess.Popen", return_value=DummyProc()):
        launch.launch_agents(sd)

    assert workspace_mcp.read_text() == original


def test_cursor_dry_run_does_not_mutate_workspace_mcp(tmp_path):
    workspace = _workspace_with_cursor_config(tmp_path)
    workspace_mcp = Path(workspace) / ".cursor" / "mcp.json"
    original = json.dumps({
        "mcpServers": {
            "peanut-review": {
                "command": "legacy-peanut-review-mcp",
                "env": {
                    "PEANUT_SESSION": "/old/session",
                    "GIT_AUTHOR_NAME": "old-agent",
                },
            },
            "unrelated": {"command": "example-mcp"},
        }
    }, indent=2) + "\n"
    workspace_mcp.write_text(original)
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
    ], workspace=workspace)

    launch.launch_agents(sd, dry_run=True)

    assert workspace_mcp.read_text() == original


def test_runner_wrappers_exec_without_shell_timeout():
    base = Path(launch._find_launcher_script("cursor")).parent
    cursor = (base / "cursor-agent-task.sh").read_text()
    opencode = (base / "opencode-agent-task.sh").read_text()
    codex = (base / "codex-agent-task.sh").read_text()

    for text in (cursor, opencode, codex):
        assert '\ntimeout "$timeout_secs"' not in text

    assert "exec cursor-agent --print" in cursor
    assert "PEANUT_CURSOR_HOME" in cursor
    assert "PEANUT_CURSOR_MCP_CONFIG" not in cursor
    assert "--approve-mcps" not in cursor
    assert 'exec "${cmd[@]}" > "$output_file"' in opencode
    assert 'exec "${cmd[@]}" > "$stream_file"' in codex
    assert 'agent: (if $agent == "" then null else $agent end)' in opencode


def test_agents_use_cli_prompt_template():
    """Agents should always get the CLI prompt."""
    sd = _make_session_dir([
        AgentConfig(name="vera", model="opus", persona="vera.md"),
        AgentConfig(
            name="felix", model="openai/gpt-5.5", persona="felix.md",
            runner="opencode",
        ),
    ])
    template = launch._resolve_template(None, "cursor")
    assert Path(template).parent.name == "templates"
    assert "skills/peanut-review" not in template
    prompts = launch.render_all_prompts(sd)
    cursor_rendered = prompts["vera"].read_text()
    rendered = prompts["felix"].read_text()
    # CLI template self-identifies by instructing the agent to execute shell commands.
    assert "Shell tool" in cursor_rendered
    assert "Shell tool" in rendered


def test_prompt_uses_persona_filename_independent_of_agent_display_name():
    sd = _make_session_dir([
        AgentConfig(name="Felix", model="openai/gpt-5.5", persona="felix.md"),
    ])

    prompts = launch.render_all_prompts(sd)
    rendered = prompts["Felix"].read_text()

    assert "cat " in rendered
    assert "/personas/felix.md" in rendered
    assert "/personas/Felix.md" not in rendered


def test_session_roundtrip_preserves_runner():
    from peanut_review import session as sess
    sd = _make_session_dir([
        AgentConfig(
            name="felix", model="openai/gpt-5.5", persona="felix.md",
            runner="opencode",
        ),
    ])
    s = sess.load_session(sd)
    assert s.agents[0].runner == "opencode"
    assert s.agents[0].model == "openai/gpt-5.5"
