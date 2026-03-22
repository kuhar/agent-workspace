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


def _find_launcher_script() -> str:
    """Find cursor-agent-task.sh relative to known locations."""
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent
        / "skills" / "ask-the-peanut-gallery" / "cursor-agent-task.sh",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    raise FileNotFoundError(
        "cursor-agent-task.sh not found. Searched: "
        + ", ".join(str(c) for c in candidates)
    )


def render_prompt(template_path: str | Path, variables: dict[str, str]) -> str:
    """Render an agent prompt template with variable substitution.

    Uses $VARIABLE or ${VARIABLE} syntax (string.Template).
    """
    text = Path(template_path).read_text()
    return Template(text).safe_substitute(variables)


def render_all_prompts(session_dir: str | Path, template_path: str | Path) -> dict[str, Path]:
    """Render per-agent prompts and write to <session>/prompts/. Returns {agent: path}."""
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
        rendered = render_prompt(template_path, variables)
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


def launch_agents(
    session_dir: str | Path,
    template_path: str | Path,
    dry_run: bool = False,
    cli_json: str | None = None,
) -> list[dict]:
    """Spawn cursor agents for all agents in the session.

    Returns list of {name, pid, cmd} dicts.
    """
    session = load_session(session_dir)
    _validate_cli_json(session.workspace)
    sdir = Path(session_dir)

    mcp_script = _find_mcp_script()
    if not mcp_script:
        print("  MCP: peanut-review-mcp script not found", file=sys.stderr)
    launcher = _find_launcher_script()

    # Render prompts
    prompts = render_all_prompts(session_dir, template_path)

    # Transition to round1
    session.state = SessionState.ROUND1.value
    save_session(sdir, session)

    results = []
    for agent in session.agents:
        prompt_path = prompts[agent.name]
        log_path = sdir / "log" / f"{agent.name}.log"

        cmd = [
            launcher,
            "--model", agent.model,
            "--workspace", session.workspace,
            "--output-dir", str(sdir / "log"),
            "--name", agent.name,
            "--timeout", str(session.timeout),
            "--prompt-file", str(prompt_path),
        ]

        env = os.environ.copy()
        # Put peanut-review bin dir on PATH so agents can call it
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

        # Write per-agent MCP config (agent name must be baked in since
        # cursor-agent's env doesn't propagate to MCP server children)
        if mcp_script:
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

        # Stagger launches to avoid cursor-agent cli-config.json race condition
        if agent != session.agents[-1]:
            time.sleep(1)

    return results
