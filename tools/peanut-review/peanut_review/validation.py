"""Configuration validation for peanut-review sessions and launches."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import AgentConfig


SUPPORTED_RUNNERS = {"cursor", "opencode", "codex", "claude"}
AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ValidationError(ValueError):
    """Raised when config validation finds one or more actionable errors."""


def _format_errors(title: str, errors: Sequence[str]) -> str:
    return title + ":\n  - " + "\n  - ".join(errors)


def _require_object(value: Any, label: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{label} must be a JSON object")
        return None
    return value


def _as_non_empty_string(value: Any, label: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return ""
    return value.strip()


def _resolve_config_path(raw: str, *, base: Path) -> Path:
    path = Path(os.path.expandvars(raw)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _validate_agent_configs(
    raw_agents: Any,
    *,
    personas_dir: Path | None,
    errors: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(raw_agents, list) or not raw_agents:
        errors.append("agents must be a non-empty array")
        return []

    agents: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_agent in enumerate(raw_agents):
        label = f"agents[{index}]"
        agent = _require_object(raw_agent, label, errors)
        if agent is None:
            continue

        name = _as_non_empty_string(agent.get("name"), f"{label}.name", errors)
        if name:
            if not AGENT_NAME_RE.match(name):
                errors.append(
                    f"{label}.name {name!r} must match {AGENT_NAME_RE.pattern}"
                )
            if name in seen:
                errors.append(f"duplicate agent name: {name}")
            seen.add(name)

        _as_non_empty_string(agent.get("model"), f"{label}.model", errors)
        persona = _as_non_empty_string(agent.get("persona"), f"{label}.persona", errors)
        if persona:
            persona_path = Path(persona)
            if persona_path.is_absolute() or ".." in persona_path.parts:
                errors.append(f"{label}.persona must be a relative filename under personasDir")
            elif personas_dir and not (personas_dir / persona_path).is_file():
                errors.append(f"{label}.persona not found: {personas_dir / persona_path}")

        runner = agent.get("runner", "cursor")
        if not isinstance(runner, str) or not runner.strip():
            errors.append(f"{label}.runner must be a non-empty string")
        elif runner not in SUPPORTED_RUNNERS:
            errors.append(
                f"{label}.runner {runner!r} is unsupported "
                f"(expected one of {', '.join(sorted(SUPPORTED_RUNNERS))})"
            )

        try:
            agents.append(AgentConfig.from_dict(agent).to_dict())
        except TypeError as e:
            errors.append(f"{label} is not a valid agent config: {e}")

    return agents


def validate_project_config(
    raw: Any,
    *,
    config_path: str | Path,
    default_personas_dir: str | Path | None = None,
    personas_dir_override: str | Path | None = None,
) -> dict[str, Any]:
    """Validate and normalize `.peanut-review.json` content."""
    path = Path(config_path)
    errors: list[str] = []
    data = _require_object(raw, str(path), errors)
    if data is None:
        raise ValidationError(_format_errors(f"{path} validation failed", errors))

    required = ["reviewRoot", "workspaceRoot", "repoRelative", "agents"]
    missing = [key for key in required if key not in data]
    if missing:
        errors.append(f"missing required key(s): {', '.join(missing)}")

    base = path.parent
    review_root_raw = _as_non_empty_string(data.get("reviewRoot"), "reviewRoot", errors)
    workspace_root_raw = _as_non_empty_string(data.get("workspaceRoot"), "workspaceRoot", errors)
    repo_relative_raw = _as_non_empty_string(data.get("repoRelative"), "repoRelative", errors)

    review_root = _resolve_config_path(review_root_raw, base=base) if review_root_raw else base
    workspace_root = (
        _resolve_config_path(workspace_root_raw, base=base)
        if workspace_root_raw
        else base
    )
    repo_relative = Path(repo_relative_raw) if repo_relative_raw else Path()
    if repo_relative_raw:
        if repo_relative.is_absolute():
            errors.append("repoRelative must be relative")
        if ".." in repo_relative.parts:
            errors.append("repoRelative must stay under workspaceRoot")
    repo_path = (workspace_root / repo_relative).resolve()

    if review_root.exists() and not review_root.is_dir():
        errors.append(f"reviewRoot exists but is not a directory: {review_root}")
    if not workspace_root.is_dir():
        errors.append(f"workspaceRoot does not exist or is not a directory: {workspace_root}")
    if not repo_path.is_dir():
        errors.append(f"configured repository does not exist: {repo_path}")

    timeout = data.get("reviewAgentTimeoutSeconds", 1200)
    if not isinstance(timeout, int) or timeout <= 0:
        errors.append("reviewAgentTimeoutSeconds must be a positive integer")

    personas_raw = (
        personas_dir_override
        if personas_dir_override is not None
        else data.get("personasDir", default_personas_dir)
    )
    personas_dir: Path | None = None
    if personas_raw:
        if not isinstance(personas_raw, (str, Path)):
            errors.append("personasDir must be a path string")
        else:
            personas_dir = _resolve_config_path(str(personas_raw), base=base)
            if not personas_dir.is_dir():
                errors.append(f"personasDir does not exist or is not a directory: {personas_dir}")

    agents = _validate_agent_configs(
        data.get("agents"),
        personas_dir=personas_dir,
        errors=errors,
    )

    if errors:
        raise ValidationError(_format_errors(f"{path} validation failed", errors))

    cfg = dict(data)
    cfg["reviewRoot"] = str(review_root)
    cfg["workspaceRoot"] = str(workspace_root)
    cfg["repoRelative"] = str(repo_relative)
    cfg["workspace"] = str(workspace_root)
    cfg["repoPath"] = str(repo_path)
    cfg["reviewAgentTimeoutSeconds"] = timeout
    cfg["agents"] = agents
    if personas_dir is not None:
        cfg["personasDir"] = str(personas_dir)
    return cfg


def validate_cursor_cli_json(
    workspace: str | Path,
    *,
    cli_json: str | Path | None = None,
) -> None:
    """Validate Cursor CLI permissions needed by peanut-review agents."""
    expected = Path(workspace) / ".cursor" / "cli.json"
    path = Path(cli_json) if cli_json else expected
    errors: list[str] = []
    if cli_json and path.resolve() != expected.resolve():
        errors.append(
            f"--cli-json must point to Cursor's workspace config at {expected}; "
            "cursor-agent reads permissions from that location"
        )

    if not path.is_file():
        errors.extend(
            [
                f"{path} not found",
                "copy tools/peanut-review/peanut_review/templates/cli.sample.json "
                "to <workspace>/.cursor/cli.json",
            ]
        )
    if errors:
        raise ValidationError(
            _format_errors("Cursor CLI config validation failed", errors)
        )

    try:
        data = json.loads(path.read_text())
    except OSError as e:
        raise ValidationError(f"Cursor CLI config validation failed:\n  - could not read {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise ValidationError(f"Cursor CLI config validation failed:\n  - could not parse {path}: {e}") from e

    obj = _require_object(data, str(path), errors)
    permissions = _require_object(obj.get("permissions") if obj else None, "permissions", errors)
    allow = permissions.get("allow") if permissions else None
    deny = permissions.get("deny") if permissions else None

    if not isinstance(allow, list):
        errors.append("permissions.allow must be an array")
        allow = []
    if not isinstance(deny, list):
        errors.append("permissions.deny must be an array")
        deny = []

    allow_strings = [str(item) for item in allow]
    deny_strings = [str(item) for item in deny]
    if not any("peanut-review" in item for item in allow_strings):
        errors.append("permissions.allow must include Shell(peanut-review **)")
    if not deny_strings:
        errors.append("permissions.deny must not be empty")
    if "Shell(**)" in deny_strings:
        errors.append("permissions.deny must not include Shell(**)")

    if errors:
        raise ValidationError(_format_errors("Cursor CLI config validation failed", errors))


def validate_launch_prerequisites(
    *,
    workspace: str | Path,
    agents: Iterable[AgentConfig],
    cli_json: str | Path | None = None,
) -> None:
    """Validate runner-specific files needed before launching agents."""
    selected = list(agents)
    errors: list[str] = []
    runners = {agent.runner for agent in selected}
    unknown = sorted(runner for runner in runners if runner not in SUPPORTED_RUNNERS)
    if unknown:
        errors.append(
            f"unsupported runner(s): {', '.join(unknown)} "
            f"(expected one of {', '.join(sorted(SUPPORTED_RUNNERS))})"
        )

    if any(agent.runner == "cursor" for agent in selected):
        try:
            validate_cursor_cli_json(workspace, cli_json=cli_json)
        except ValidationError as e:
            errors.append(str(e))

    if errors:
        raise ValidationError(_format_errors("Launch configuration validation failed", errors))
