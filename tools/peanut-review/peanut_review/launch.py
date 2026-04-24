"""Spawn cursor agents for review — replaces cursor-agent-multi.py."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from string import Template

from .models import AgentStatus, SessionState
from .session import load_session, save_session, update_agent_status


_LAUNCHER_SCRIPTS = {
    "cursor": "cursor-agent-task.sh",
    "opencode": "opencode-agent-task.sh",
}


def _find_launcher_script(runner: str = "cursor") -> str:
    """Find the launcher script for a given runner ("cursor" or "opencode")."""
    script_name = _LAUNCHER_SCRIPTS.get(runner)
    if not script_name:
        raise ValueError(f"unknown runner: {runner!r} (expected one of {list(_LAUNCHER_SCRIPTS)})")
    path = (Path(__file__).resolve().parent.parent.parent.parent
            / "skills" / "ask-the-peanut-gallery" / script_name)
    if path.exists():
        return str(path)
    raise FileNotFoundError(f"{script_name} not found at {path}")


def render_prompt(template_path: str | Path, variables: dict[str, str]) -> str:
    """Render an agent prompt template with variable substitution.

    Uses $VARIABLE or ${VARIABLE} syntax (string.Template).
    """
    text = Path(template_path).read_text()
    return Template(text).safe_substitute(variables)


def _resolve_template(user_template: str | Path | None, runner: str) -> str:
    """Pick the prompt template for a given runner.

    Explicit --template always wins. Otherwise: cursor prefers MCP if the MCP
    launcher script is installed; opencode always uses the CLI template (MCP
    integration is not wired up yet).
    """
    if user_template:
        return str(user_template)
    skills_dir = Path(__file__).resolve().parent.parent.parent.parent / "skills" / "peanut-review"
    if runner == "cursor":
        mcp_script = Path(__file__).resolve().parent.parent / "bin" / "peanut-review-mcp"
        if mcp_script.exists():
            mcp_default = skills_dir / "agent-prompt-mcp.md"
            if mcp_default.exists():
                return str(mcp_default)
    default = skills_dir / "agent-prompt.md"
    if default.exists():
        return str(default)
    raise FileNotFoundError(
        f"no prompt template found for runner={runner!r} (looked in {skills_dir})"
    )


def render_all_prompts(
    session_dir: str | Path,
    template_path: str | Path | None = None,
) -> dict[str, Path]:
    """Render per-agent prompts and write to <session>/prompts/. Returns {agent: path}.

    If template_path is provided, it is used for all agents. Otherwise the
    template is picked per agent based on agent.runner.
    """
    session = load_session(session_dir)
    sdir = Path(session_dir)
    prompts_dir = sdir / "prompts"
    prompts_dir.mkdir(exist_ok=True)

    pr_bin = str(Path(__file__).resolve().parent.parent / "bin" / "peanut-review")

    result = {}
    for agent in session.agents:
        variables = {
            "SESSION": str(sdir),
            "WORKSPACE": session.workspace,
            "AGENT": agent.name,
            "DIFF_COMMANDS": " && ".join(session.diff_commands),
            "BASE_REF": session.base_ref,
            "TOPIC_REF": session.topic_ref,
            "PR_BIN": pr_bin,
        }
        tpl = _resolve_template(template_path, agent.runner)
        rendered = render_prompt(tpl, variables)
        prompt_path = prompts_dir / f"{agent.name}.md"
        prompt_path.write_text(rendered)
        result[agent.name] = prompt_path

    return result


def _validate_cli_json(workspace: str | Path) -> None:
    """Warn if cli.json is missing peanut-review permissions or has Shell(**) deny."""
    cli_json_path = Path(workspace) / ".cursor" / "cli.json"
    if not cli_json_path.exists():
        print(f"Warning: {cli_json_path} not found — agents may lack permissions", file=sys.stderr)
        return
    try:
        data = json.loads(cli_json_path.read_text())
        perms = data.get("permissions", {})
        allow = perms.get("allow", [])
        deny = perms.get("deny", [])

        has_pr = any("peanut-review" in str(a) for a in allow)
        if not has_pr:
            print("Warning: cli.json allow list does not include 'Shell(peanut-review **)' "
                  "— agents won't be able to run peanut-review", file=sys.stderr)

        has_shell_deny = any(str(d) == "Shell(**)" for d in deny)
        if has_shell_deny:
            print("Warning: cli.json deny list contains 'Shell(**)' which overrides all "
                  "Shell allows — agents won't be able to run any shell commands", file=sys.stderr)
    except json.JSONDecodeError as e:
        print(f"Warning: could not parse {cli_json_path}: {e}", file=sys.stderr)


def _find_mcp_script() -> str | None:
    """Find the peanut-review-mcp script (uses uv for zero-install)."""
    script = Path(__file__).resolve().parent.parent / "bin" / "peanut-review-mcp"
    if script.exists():
        return str(script)
    return None


def _setup_mcp_config(session_dir: Path, workspace: str, agent_name: str, mcp_script: str) -> Path:
    """Write .cursor/mcp.json for the given agent.

    Called before each agent spawn so GIT_AUTHOR_NAME is correct for that agent's
    MCP server instance. The stagger between spawns ensures each agent reads
    its own config.
    """
    mcp_config = {
        "mcpServers": {
            "peanut-review": {
                "command": mcp_script,
                "env": {
                    "PEANUT_SESSION": str(session_dir),
                    "GIT_AUTHOR_NAME": agent_name,
                },
            }
        }
    }

    cursor_dir = Path(workspace) / ".cursor"
    cursor_dir.mkdir(exist_ok=True)
    mcp_path = cursor_dir / "mcp.json"

    # Merge with existing mcp.json (preserve non-peanut-review servers)
    if mcp_path.exists():
        try:
            existing = json.loads(mcp_path.read_text())
            existing.setdefault("mcpServers", {}).update(mcp_config["mcpServers"])
            mcp_config = existing
        except json.JSONDecodeError:
            pass

    mcp_path.write_text(json.dumps(mcp_config, indent=2) + "\n")
    return mcp_path


def _build_agent_cmd(
    agent,
    *,
    session,
    session_dir: Path,
    prompt_path: Path,
) -> list[str]:
    """Build the launcher command for a single agent based on its runner."""
    launcher = _find_launcher_script(agent.runner)
    cmd = [
        launcher,
        "--model", agent.model,
        "--workspace", session.workspace,
        "--output-dir", str(session_dir / "log"),
        "--name", agent.name,
        "--timeout", str(session.timeout),
        "--prompt-file", str(prompt_path),
    ]
    if agent.runner == "opencode":
        cmd += [
            "--lcode-primary", agent.lcode_primary or "qwen",
            "--lcode-subagent", agent.lcode_subagent or "null",
        ]
    return cmd


def launch_agents(
    session_dir: str | Path,
    template_path: str | Path | None = None,
    dry_run: bool = False,
    cli_json: str | None = None,
) -> list[dict]:
    """Spawn agents for all entries in the session, dispatching by agent.runner.

    Returns list of {name, pid, cmd} dicts.
    """
    session = load_session(session_dir)
    sdir = Path(session_dir)

    runners = {a.runner for a in session.agents}
    if "cursor" in runners:
        _validate_cli_json(session.workspace)

    mcp_script = _find_mcp_script() if "cursor" in runners else None
    if "cursor" in runners and not mcp_script:
        print("  MCP: peanut-review-mcp script not found", file=sys.stderr)

    prompts = render_all_prompts(session_dir, template_path)

    session.state = SessionState.ROUND1.value
    save_session(sdir, session)

    results = []
    for agent in session.agents:
        prompt_path = prompts[agent.name]
        log_path = sdir / "log" / f"{agent.name}.log"
        cmd = _build_agent_cmd(agent, session=session, session_dir=sdir, prompt_path=prompt_path)

        env = os.environ.copy()
        bin_dir = str(Path(__file__).resolve().parent.parent / "bin")
        env["PATH"] = bin_dir + ":" + env.get("PATH", "")
        env["GIT_AUTHOR_NAME"] = agent.name
        env["GIT_AUTHOR_EMAIL"] = f"{agent.name}@peanut-review.local"
        env["GIT_COMMITTER_NAME"] = agent.name
        env["GIT_COMMITTER_EMAIL"] = f"{agent.name}@peanut-review.local"
        env["PEANUT_SESSION"] = str(sdir)

        if dry_run:
            results.append({"name": agent.name, "pid": None, "cmd": cmd})
            continue

        # MCP config is cursor-specific for now — opencode runs in CLI mode.
        if agent.runner == "cursor" and mcp_script:
            _setup_mcp_config(sdir, session.workspace, agent.name, mcp_script)

        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=session.workspace,
            )

        update_agent_status(sdir, agent.name, AgentStatus.RUNNING.value, proc.pid)
        results.append({"name": agent.name, "pid": proc.pid, "cmd": cmd})

        # Stagger launches: cursor-agent has a cli-config.json race, and lcode's
        # idempotent llama-server startup also benefits from letting the first
        # opencode agent finish booting servers before peers join.
        if agent != session.agents[-1]:
            time.sleep(1)

    return results
