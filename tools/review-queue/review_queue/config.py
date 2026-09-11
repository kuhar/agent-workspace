from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

from .models import ProjectConfig, QueueConfig


def xdg_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "review-queue" / "config.toml"


def xdg_state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "review-queue"


def example_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.example.toml"


def write_default_config(path: Path | None = None, *, force: bool = False) -> Path:
    destination = (path or xdg_config_path()).expanduser().resolve()
    if destination.exists() and not force:
        raise FileExistsError(f"configuration already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    shutil.copyfile(example_config_path(), temporary)
    temporary.replace(destination)
    return destination


def _positive(name: str, value: object, *, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _include_paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
        raise ValueError("include_paths must be an array of repository-relative directories")
    paths = []
    for path in value:
        clean = path.rstrip("/")
        if (
            not clean
            or clean.startswith("/")
            or any(part in {"", ".", ".."} for part in clean.split("/"))
            or any(char in path for char in "*?[]\\")
        ):
            raise ValueError(f"invalid include_paths directory: {path!r}")
        paths.append(clean + "/")
    return tuple(sorted(set(paths)))


def load_config(path: Path | None = None, *, state_dir: Path | None = None) -> QueueConfig:
    config_path = (path or xdg_config_path()).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(
            f"missing configuration: {config_path}; run `review-queue init` first"
        )
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)

    queue = raw.get("queue", {})
    ui = raw.get("ui", {})
    raw_projects = raw.get("projects", [])
    if (
        not isinstance(queue, dict)
        or not isinstance(ui, dict)
        or not isinstance(raw_projects, list)
        or not raw_projects
    ):
        raise ValueError("configuration requires [queue] and at least one [[projects]] entry")
    theme = ui.get("theme", "catppuccin-mocha")
    if not isinstance(theme, str) or not theme.strip():
        raise ValueError("ui.theme must be a non-empty string")

    projects: list[ProjectConfig] = []
    names: set[str] = set()
    repos: set[str] = set()
    for index, item in enumerate(raw_projects):
        if not isinstance(item, dict):
            raise ValueError(f"projects[{index}] must be a table")
        missing = [
            key
            for key in ("name", "repo", "query", "launcher", "wrapper_root")
            if not item.get(key)
        ]
        if missing:
            raise ValueError(f"projects[{index}] is missing: {', '.join(missing)}")
        name = str(item["name"])
        repo = str(item["repo"])
        if name in names:
            raise ValueError(f"duplicate project name: {name}")
        if repo in repos:
            raise ValueError(f"duplicate project repository: {repo}")
        names.add(name)
        repos.add(repo)
        launcher = Path(os.path.expandvars(str(item["launcher"]))).expanduser().resolve()
        wrapper_root = Path(os.path.expandvars(str(item["wrapper_root"]))).expanduser().resolve()
        if launcher.name != "review-pr.sh":
            raise ValueError(f"project {name} launcher must be named review-pr.sh")
        projects.append(
            ProjectConfig(
                name=name,
                repo=repo,
                query=str(item["query"]),
                launcher=launcher,
                wrapper_root=wrapper_root,
                estimated_wrapper_gib=_positive(
                    f"projects[{index}].estimated_wrapper_gib",
                    item.get("estimated_wrapper_gib", 160),
                ),
                priority=int(item.get("priority", 0)),
                include_paths=_include_paths(item.get("include_paths", [])),
            )
        )

    resolved_state = (state_dir or xdg_state_dir()).expanduser().resolve()
    start_mode = queue.get("start_mode")
    if start_mode is not None and start_mode not in ("active", "manual", "paused"):
        raise ValueError("queue.start_mode must be active, manual, or paused")
    return QueueConfig(
        poll_seconds=_positive("queue.poll_seconds", queue.get("poll_seconds", 120)),
        push_quiet_seconds=_positive(
            "queue.push_quiet_seconds", queue.get("push_quiet_seconds", 120), allow_zero=True
        ),
        bootstrap_hours=_positive("queue.bootstrap_hours", queue.get("bootstrap_hours", 24)),
        max_running=_positive("queue.max_running", queue.get("max_running", 2)),
        max_wrappers=_positive("queue.max_wrappers", queue.get("max_wrappers", 3)),
        min_free_gib=_positive("queue.min_free_gib", queue.get("min_free_gib", 50)),
        start_paused=bool(queue.get("start_paused", False)),
        start_mode=start_mode,
        projects=tuple(projects),
        config_path=config_path,
        state_dir=resolved_state,
        theme=theme.strip(),
    )
