"""Spawn cursor agents for review — replaces cursor-agent-multi.py."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from string import Template
from typing import Sequence

from . import curator, store
from .models import AgentStatus, SessionState
from .session import (
    load_session,
    repo_path,
    reset_agent_runtime,
    save_session,
    update_agent_status,
)
from .validation import validate_launch_prerequisites


_LAUNCHER_SCRIPTS = {
    "cursor": "cursor-agent-task.sh",
    "opencode": "opencode-agent-task.sh",
    "codex": "codex-agent-task.sh",
}


def _find_launcher_script(runner: str = "cursor") -> str:
    """Find the bundled launcher script for a given runner."""
    script_name = _LAUNCHER_SCRIPTS.get(runner)
    if not script_name:
        raise ValueError(f"unknown runner: {runner!r} (expected one of {list(_LAUNCHER_SCRIPTS)})")
    path = Path(__file__).resolve().parent / "runners" / script_name
    if path.exists():
        return str(path)
    raise FileNotFoundError(f"{script_name} not found at {path}")


def render_prompt(template_path: str | Path, variables: dict[str, str]) -> str:
    """Render an agent prompt template with variable substitution.

    Uses $VARIABLE or ${VARIABLE} syntax (string.Template).
    """
    text = Path(template_path).read_text()
    return Template(text).safe_substitute(variables)


def _format_workspace_layout(workspace: str, repo: str, repo_relative: str) -> str:
    workspace_path = Path(workspace)
    repo_path = Path(repo)
    try:
        nested_repo = workspace_path.resolve() != repo_path.resolve()
    except OSError:
        nested_repo = workspace_path != repo_path

    if not nested_repo:
        return "Workspace and Repository are the same path; shell commands start there."

    rel = repo_relative or str(repo_path)
    return "\n".join([
        "Workspace and Repository are different:",
        "- Workspace is the runner/build/tool root; shell commands start there.",
        f"- Repository is the Git/source root under `{rel}`; use `git -C` for Git commands there.",
        "- Do not assume `build/` is inside Repository; wrapper-level build dirs are under Workspace.",
    ])


def _format_workspace_artifacts(workspace: str) -> str:
    workspace_path = Path(workspace)
    lines: list[str] = []

    try:
        children = sorted(workspace_path.iterdir(), key=lambda p: p.name)
    except OSError:
        children = []

    build_dirs = [
        child
        for child in children
        if child.is_dir() and (child.name == "build" or child.name.startswith("build-"))
    ]
    if build_dirs:
        lines.append("Detected build dirs under Workspace:")
        for build_dir in build_dirs[:8]:
            lines.append(f"- `{build_dir}`")
        if len(build_dirs) > 8:
            lines.append(f"- ... {len(build_dirs) - 8} more")

    compile_commands = workspace_path / "compile_commands.json"
    if compile_commands.exists():
        try:
            resolved = compile_commands.resolve()
        except OSError:
            resolved = compile_commands
        if resolved != compile_commands:
            lines.append(f"Compilation database: `{compile_commands}` -> `{resolved}`")
        else:
            lines.append(f"Compilation database: `{compile_commands}`")

    venv = workspace_path / "venv"
    if venv.is_dir():
        lines.append(f"Python venv: `{venv}`")

    if not lines:
        return (
            "No build dirs were detected directly under Workspace. "
            "If you need a build, search from Workspace before assuming an in-source build."
        )
    return "\n".join(lines)


def _format_git_commands(repo: str, commands: Sequence[str]) -> str:
    rendered: list[str] = []
    repo_arg = shlex.quote(repo)
    for command in commands:
        stripped = command.strip()
        if stripped.startswith("git "):
            rendered.append(f"git -C {repo_arg} {stripped[len('git '):]}")
        else:
            rendered.append(stripped)
    return " && ".join(rendered)


def _resolve_template(user_template: str | Path | None, agent) -> str:
    """Pick the prompt template for a given runner.

    Explicit --template always wins. Otherwise all runners use the CLI prompt.
    """
    if user_template:
        return str(user_template)
    template_name = (
        "curator-prompt.md"
        if curator.is_curator(agent)
        else "agent-prompt.md"
    )
    default = Path(__file__).resolve().parent / "templates" / template_name
    if default.exists():
        return str(default)
    raise FileNotFoundError(
        f"no prompt template found for agent={agent.name!r} (looked at {default})"
    )


def render_all_prompts(
    session_dir: str | Path,
    template_path: str | Path | None = None,
    agent_names: Sequence[str] | None = None,
) -> dict[str, Path]:
    """Render per-agent prompts and write to <session>/prompts/. Returns {agent: path}.

    If template_path is provided, it is used for all agents. Otherwise the
    template is picked per agent based on agent.runner.
    """
    session = load_session(session_dir)
    agents = _select_agents(session.agents, agent_names)
    sdir = Path(session_dir)
    prompts_dir = sdir / "prompts"
    prompts_dir.mkdir(exist_ok=True)

    pr_bin = str(Path(__file__).resolve().parent.parent / "bin" / "peanut-review")

    result = {}
    repo = repo_path(session)
    reviewer_names = ", ".join(a.name for a in curator.reviewers(session.agents))
    if not reviewer_names:
        reviewer_names = "<none>"
    curation_since = session.curation_since_comment_id or ""
    if curation_since:
        curation_scope = (
            "Focus on reviewer comments after "
            f"`{curation_since}`; still inspect all visible and deleted "
            "comments when deduplicating."
        )
        curation_since_command = (
            f"{pr_bin} --session {sdir} comments --since {curation_since} --format json"
        )
    else:
        curation_scope = (
            "No comment baseline is recorded; inspect the current visible "
            "and deleted comments and curate the local reviewer-authored set."
        )
        curation_since_command = (
            f"{pr_bin} --session {sdir} comments --format json"
        )
    for agent in agents:
        variables = {
            "SESSION": str(sdir),
            "WORKSPACE": session.workspace,
            "REPO_PATH": repo,
            "REPO_RELATIVE": session.repo_relative or ".",
            "WORKSPACE_LAYOUT": _format_workspace_layout(
                session.workspace,
                repo,
                session.repo_relative or ".",
            ),
            "WORKSPACE_ARTIFACTS": _format_workspace_artifacts(session.workspace),
            "AGENT": agent.name,
            "PERSONA": agent.persona,
            "REVIEWER_AGENTS": reviewer_names,
            "CURATION_SINCE_COMMENT_ID": curation_since,
            "CURATION_SCOPE": curation_scope,
            "CURATION_SINCE_COMMAND": curation_since_command,
            "DIFF_COMMANDS": " && ".join(session.diff_commands),
            "GIT_DIFF_COMMANDS": _format_git_commands(repo, session.diff_commands),
            "BASE_REF": session.base_ref,
            "TOPIC_REF": session.topic_ref,
            "PR_BIN": pr_bin,
        }
        tpl = _resolve_template(template_path, agent)
        rendered = render_prompt(tpl, variables)
        prompt_path = prompts_dir / f"{agent.name}.md"
        prompt_path.write_text(rendered)
        result[agent.name] = prompt_path

    return result


def _cursor_runtime_paths(session_dir: Path, agent_name: str) -> dict[str, Path]:
    cursor_home = session_dir / "runtime" / "cursor" / agent_name
    cursor_dir = cursor_home / ".cursor"
    return {
        "cursor_home": cursor_home,
        "cursor_dir": cursor_dir,
    }


def _setup_cursor_runtime(
    session_dir: Path,
    agent_name: str,
    *,
    dry_run: bool = False,
) -> dict[str, str]:
    """Prepare an isolated Cursor home/config directory for one agent."""
    paths = _cursor_runtime_paths(session_dir, agent_name)
    if not dry_run:
        paths["cursor_dir"].mkdir(parents=True, exist_ok=True)
    return {"cursor_home": str(paths["cursor_home"])}


def _apply_cursor_env(env: dict[str, str], cursor_runtime: dict[str, str]) -> None:
    original_home = env.get("HOME") or str(Path.home())
    original_xdg_config = env.get("XDG_CONFIG_HOME") or str(Path(original_home) / ".config")
    cursor_home = cursor_runtime["cursor_home"]
    cursor_config_dir = str(Path(cursor_home) / ".cursor")

    env["HOME"] = cursor_home
    env["CURSOR_CONFIG_DIR"] = cursor_config_dir
    env["CURSOR_DATA_DIR"] = cursor_config_dir
    env["XDG_CONFIG_HOME"] = original_xdg_config
    env["PEANUT_CURSOR_HOME"] = cursor_home


def _normalize_agent_names(agent_names: Sequence[str] | None) -> list[str] | None:
    if agent_names is None:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for raw in agent_names:
        name = raw.strip()
        if not name or name in seen:
            continue
        result.append(name)
        seen.add(name)
    return result


def _select_agents(agents, agent_names: Sequence[str] | None):
    requested = _normalize_agent_names(agent_names)
    if requested is None:
        return curator.reviewers(agents)

    available = {agent.name for agent in agents}
    unknown = [name for name in requested if name not in available]
    if unknown:
        known = ", ".join(agent.name for agent in agents) or "<none>"
        raise ValueError(
            f"unknown agent(s): {', '.join(unknown)} "
            f"(available: {known})"
        )

    requested_set = set(requested)
    return [agent for agent in agents if agent.name in requested_set]


def _latest_comment_id(session_dir: Path) -> str | None:
    comments = store.read_all_comments(session_dir)
    return comments[-1].id if comments else None


def _round_signal_path(session_dir: Path, agent_name: str, event: str) -> Path:
    return session_dir / "signals" / f"{agent_name}.{event}"


def _clear_agent_round_state(session_dir: Path, agent_names: Sequence[str]) -> None:
    """Clear runtime metadata and round-bound signals for selected agents."""
    from . import runtime

    for agent_name in agent_names:
        for event in ("round-done", "next-round"):
            try:
                _round_signal_path(session_dir, agent_name, event).unlink()
            except FileNotFoundError:
                pass
        try:
            runtime.agent_meta_path(session_dir, agent_name).unlink()
        except FileNotFoundError:
            pass
    reset_agent_runtime(session_dir, list(agent_names))


def _clear_curator_auto_launch_markers(session_dir: Path, agents) -> None:
    for agent in agents:
        try:
            (session_dir / "signals" / f"{agent.name}.auto-launching").unlink()
        except FileNotFoundError:
            pass


def _prepare_curation_baseline(session_dir: Path, session, agents) -> None:
    if not agents or any(curator.is_curator(agent) for agent in agents):
        return
    session.curation_since_comment_id = _latest_comment_id(session_dir)
    curators = curator.curators(session.agents)
    if curators:
        _clear_curator_auto_launch_markers(session_dir, curators)
        _clear_agent_round_state(session_dir, [agent.name for agent in curators])
        refreshed = load_session(session_dir)
        refreshed_by_name = {agent.name: agent for agent in refreshed.agents}
        for index, agent in enumerate(session.agents):
            replacement = refreshed_by_name.get(agent.name)
            if replacement is not None:
                session.agents[index] = replacement


def _ensure_agents_not_live(session_dir: Path, agents) -> None:
    from . import runtime

    live: list[str] = []
    for agent in agents:
        snapshot = runtime.inspect_agent_runtime(session_dir, agent)
        if snapshot["process_state"] in {
            runtime.PROCESS_LAUNCHING,
            runtime.PROCESS_RUNNING,
        }:
            details = [f"process={snapshot['process_state']}"]
            if snapshot["pid"]:
                details.append(f"pid={snapshot['pid']}")
            if snapshot["supervisor_pid"] and snapshot["supervisor_live"]:
                details.append(f"supervisor={snapshot['supervisor_pid']}")
            live.append(f"{agent.name} ({' '.join(details)})")
    if live:
        raise ValueError(
            "cannot rerun live agent(s): "
            + ", ".join(live)
            + "; wait for them to finish or stop them first"
        )


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
    if agent.runner == "codex":
        # Codex must be able to write comments/signals to the session dir,
        # which lives outside the reviewed workspace. In current local Codex,
        # workspace-write + --add-dir still leaves that path read-only for
        # shell commands, so use the unrestricted sandbox for this runner.
        cmd += [
            "--sandbox", "danger-full-access",
            "--add-dir", str(session_dir),
            "--add-dir", "/tmp",
        ]
        if agent.reasoning_effort:
            cmd += ["--reasoning-effort", agent.reasoning_effort]
        cmd.append("--fast-mode" if agent.fast_mode is True else "--no-fast-mode")
    return cmd


def _build_supervisor_cmd(
    *,
    session_dir: Path,
    agent_name: str,
    timeout: int,
    workspace: str,
    wrapper_cmd: list[str],
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "peanut_review.supervisor",
        "--session",
        str(session_dir),
        "--agent",
        agent_name,
        "--timeout",
        str(timeout),
        "--cwd",
        workspace,
        "--",
        *wrapper_cmd,
    ]


def launch_agents(
    session_dir: str | Path,
    template_path: str | Path | None = None,
    dry_run: bool = False,
    cli_json: str | None = None,
    agent_names: Sequence[str] | None = None,
) -> list[dict]:
    """Spawn selected agents from the session, dispatching by agent.runner.

    Returns list of {name, pid, cmd} dicts.
    """
    session = load_session(session_dir)
    sdir = Path(session_dir)
    agents = _select_agents(session.agents, agent_names)

    validate_launch_prerequisites(
        workspace=session.workspace,
        agents=agents,
        cli_json=cli_json,
    )

    prompts = render_all_prompts(session_dir, template_path, agent_names=agent_names)

    session.state = SessionState.ROUND.value
    if not dry_run:
        _prepare_curation_baseline(sdir, session, agents)
    save_session(sdir, session)

    results = []
    for index, agent in enumerate(agents):
        prompt_path = prompts[agent.name]
        log_path = sdir / "log" / f"{agent.name}.log"
        cmd = _build_agent_cmd(agent, session=session, session_dir=sdir, prompt_path=prompt_path)
        supervisor_cmd = _build_supervisor_cmd(
            session_dir=sdir,
            agent_name=agent.name,
            timeout=session.timeout,
            workspace=session.workspace,
            wrapper_cmd=cmd,
        )

        env = os.environ.copy()
        bin_dir = str(Path(__file__).resolve().parent.parent / "bin")
        env["PATH"] = bin_dir + ":" + env.get("PATH", "")
        env["GIT_AUTHOR_NAME"] = agent.name
        env["GIT_AUTHOR_EMAIL"] = f"{agent.name}@peanut-review.local"
        env["GIT_COMMITTER_NAME"] = agent.name
        env["GIT_COMMITTER_EMAIL"] = f"{agent.name}@peanut-review.local"
        env["PEANUT_SESSION"] = str(sdir)
        cursor_runtime = None
        if agent.runner == "cursor":
            cursor_runtime = _setup_cursor_runtime(
                sdir,
                agent.name,
                dry_run=dry_run,
            )
            _apply_cursor_env(env, cursor_runtime)

        if dry_run:
            result = {
                "name": agent.name,
                "pid": None,
                "pgid": None,
                "supervisor_pid": None,
                "cmd": cmd,
                "supervisor_cmd": supervisor_cmd,
            }
            if cursor_runtime:
                result.update(cursor_runtime)
            results.append(result)
            continue

        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                supervisor_cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=session.workspace,
                start_new_session=True,
            )

        update_agent_status(
            sdir,
            agent.name,
            AgentStatus.RUNNING.value,
            supervisor_pid=proc.pid,
        )
        result = {
            "name": agent.name,
            "pid": None,
            "pgid": None,
            "supervisor_pid": proc.pid,
            "cmd": cmd,
            "supervisor_cmd": supervisor_cmd,
        }
        if cursor_runtime:
            result.update(cursor_runtime)
        results.append(result)

        # Stagger launches: cursor-agent has a cli-config.json race, and lcode's
        # idempotent llama-server startup also benefits from letting the first
        # opencode agent finish booting servers before peers join.
        if index != len(agents) - 1:
            time.sleep(1)

    return results


def rerun_agents(
    session_dir: str | Path,
    *,
    agent_names: Sequence[str],
    template_path: str | Path | None = None,
    dry_run: bool = False,
    cli_json: str | None = None,
) -> list[dict]:
    """Reset selected agents' round state and launch them again."""
    session = load_session(session_dir)
    sdir = Path(session_dir)
    agents = _select_agents(session.agents, agent_names)
    selected_names = [agent.name for agent in agents]

    if not dry_run:
        _ensure_agents_not_live(sdir, agents)
        _clear_agent_round_state(sdir, selected_names)

    return launch_agents(
        sdir,
        template_path=template_path,
        dry_run=dry_run,
        cli_json=cli_json,
        agent_names=selected_names,
    )


def ensure_curator_agent(session_dir: str | Path) -> str:
    sdir = Path(session_dir)
    session = load_session(sdir)
    agent = curator.ensure_curator_agent(session.agents)
    save_session(sdir, session)
    return agent.name


def launch_curator(
    session_dir: str | Path,
    *,
    template_path: str | Path | None = None,
    dry_run: bool = False,
    cli_json: str | None = None,
) -> list[dict]:
    """Reset and launch the dedicated comment curator agent."""
    sdir = Path(session_dir)
    curator_name = ensure_curator_agent(sdir)
    session = load_session(sdir)
    agents = _select_agents(session.agents, [curator_name])
    if not dry_run:
        _ensure_agents_not_live(sdir, agents)
        _clear_curator_auto_launch_markers(sdir, agents)
        _clear_agent_round_state(sdir, [curator_name])
    return launch_agents(
        sdir,
        template_path=template_path,
        dry_run=dry_run,
        cli_json=cli_json,
        agent_names=[curator_name],
    )
