from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

AGENT_WORKSPACE = Path(__file__).resolve().parents[3]
JAKUB_ENV_ROOT = Path(os.environ.get("JAKUB_ENV_ROOT", AGENT_WORKSPACE.parent))
SCRIPT = Path(
    os.environ.get(
        "ROCJITSU_WORKTREE_SCRIPT",
        JAKUB_ENV_ROOT / "worktree-scripts/rocjitsu/rocjitsu-worktree.sh",
    )
)
LAUNCHER = Path(
    os.environ.get("ROCJITSU_REVIEW_LAUNCHER", AGENT_WORKSPACE / "rocjitsu/review-pr.sh")
)


@pytest.fixture
def managed_wrapper(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    main_root = tmp_path / "rocjitsu" / "develop"
    repository = main_root / "rocm-systems"
    source = repository / "emulation" / "rocjitsu"
    source.mkdir(parents=True)
    (source / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.25)\n")
    (source / ".gitignore").write_text("/CMakePresets.json\n")
    subprocess.run(["git", "init", "-b", "develop", repository], check=True, capture_output=True)
    subprocess.run(["git", "-C", repository, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repository,
            "-c",
            "user.name=Review Queue Test",
            "-c",
            "user.email=review-queue@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    root = tmp_path / "managed"
    wrapper = root / "pr-1"
    wrapper.mkdir(parents=True)
    token = "owned-token"
    (wrapper / ".review-queue.json").write_text(json.dumps({"protocol": 1, "owner_token": token}))
    (wrapper / ".review-queue.lock").touch()
    subprocess.run(
        ["git", "-C", repository, "worktree", "add", "--detach", wrapper / "rocm-systems", "HEAD"],
        check=True,
        capture_output=True,
    )
    env = os.environ.copy()
    env.update(
        {
            "ROCJITSUS": str(tmp_path / "rocjitsu"),
            "ROCJITSU_MAIN_ROOT": str(main_root),
            "ROCJITSU_MAIN_WORKSPACE": str(repository),
            "REVIEW_QUEUE_ROOT": str(root),
            "REVIEW_QUEUE_OWNER_TOKEN": token,
        }
    )
    return wrapper, env


def check(wrapper: Path, env: dict[str, str]) -> dict[str, object]:
    result = subprocess.run(
        [SCRIPT, "queue-cleanup", "--check", wrapper],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_cleanup_preflight_accepts_only_clean_owned_wrapper(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    assert check(wrapper, env)["safe"] is True

    (wrapper / "rocm-systems" / "scratch.txt").write_text("dirty")
    dirty = check(wrapper, env)
    assert dirty["safe"] is False
    assert "changes" in str(dirty["reason"])


def test_cleanup_preflight_rejects_unknown_entry_and_wrong_token(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    (wrapper / "notes.txt").write_text("keep me")
    assert check(wrapper, env)["safe"] is False
    (wrapper / "notes.txt").unlink()
    wrong = env | {"REVIEW_QUEUE_OWNER_TOKEN": "wrong"}
    assert check(wrapper, wrong)["safe"] is False


def test_cleanup_preflight_rejects_active_lock(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    with (wrapper / ".review-queue.lock").open("r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = check(wrapper, env)
        assert result["safe"] is False
        assert "lock" in str(result["reason"])


def test_cleanup_removes_clean_managed_wrapper(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    beads = wrapper / ".beads"
    beads.mkdir()
    (beads / "issues.jsonl").write_text("review bookkeeping\n")
    assert check(wrapper, env)["safe"] is True
    subprocess.run(
        [SCRIPT, "queue-cleanup", wrapper],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert not wrapper.exists()


def test_cleanup_accepts_owned_marker_only_wrapper(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    repository = Path(env["ROCJITSU_MAIN_WORKSPACE"])
    subprocess.run(
        ["git", "-C", repository, "worktree", "remove", wrapper / "rocm-systems"],
        check=True,
        capture_output=True,
    )
    assert check(wrapper, env)["safe"] is True
    subprocess.run([SCRIPT, "queue-cleanup", wrapper], env=env, check=True, capture_output=True)
    assert not wrapper.exists()


def test_queue_launcher_protocol_and_cleanup_adapter(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    protocol = subprocess.run(
        [LAUNCHER, "queue", "protocol"], text=True, capture_output=True, check=True
    )
    assert json.loads(protocol.stdout)["version"] == 1

    queue_env = env | {
        "REVIEW_QUEUE_PROTOCOL": "1",
        "REVIEW_QUEUE_WRAPPER": str(wrapper),
        "REVIEW_QUEUE_TARGET_HEAD": "cleanup",
        "ROCJITSU_WORKTREE_SCRIPT": str(SCRIPT),
    }
    result = subprocess.run(
        [LAUNCHER, "queue", "cleanup", "--check"],
        env=queue_env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout)["safe"] is True


def test_setup_adapter_rejects_wrong_owner(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    head = subprocess.run(
        ["git", "-C", wrapper / "rocm-systems", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    result = subprocess.run(
        [SCRIPT, "queue-setup", wrapper, head],
        env=env | {"REVIEW_QUEUE_OWNER_TOKEN": "wrong"},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "ownership marker" in result.stderr


def test_queue_setup_assets_remain_safely_recyclable(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    head = subprocess.run(
        ["git", "-C", wrapper / "rocm-systems", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    sdk = wrapper / "venv/bin/rocm-sdk"
    sdk.parent.mkdir(parents=True)
    sdk.touch(mode=0o755)
    (wrapper / "build").mkdir()
    (wrapper / ".envrc").touch()

    subprocess.run(
        [SCRIPT, "queue-setup", wrapper, head],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    user_presets = wrapper / "rocm-systems/emulation/rocjitsu/CMakeUserPresets.json"
    assert user_presets.is_symlink()
    assert check(wrapper, env)["safe"] is True

    subprocess.run(
        [SCRIPT, "queue-cleanup", wrapper],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert not wrapper.exists()


def test_cleanup_rejects_replaced_managed_source_asset(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    user_presets = wrapper / "rocm-systems/emulation/rocjitsu/CMakeUserPresets.json"
    user_presets.symlink_to("/dev/null")
    result = check(wrapper, env)
    assert result["safe"] is False
    assert "assets" in str(result["reason"])


def test_queue_setup_refuses_dirty_source_before_switching_head(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    repository = Path(env["ROCJITSU_MAIN_WORKSPACE"])
    original_head = subprocess.run(
        ["git", "-C", wrapper / "rocm-systems", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    (repository / "next.txt").write_text("next\n")
    subprocess.run(["git", "-C", repository, "add", "next.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            repository,
            "-c",
            "user.name=Review Queue Test",
            "-c",
            "user.email=review-queue@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "next",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    next_head = subprocess.run(
        ["git", "-C", repository, "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    (wrapper / "rocm-systems/scratch.txt").write_text("keep\n")

    result = subprocess.run(
        [SCRIPT, "queue-setup", wrapper, next_head],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "tracked or untracked changes" in result.stderr
    current_head = subprocess.run(
        ["git", "-C", wrapper / "rocm-systems", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert current_head == original_head
