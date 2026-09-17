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
            "ROCJITSU_REVIEW_LAUNCHER": str(LAUNCHER),
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


@pytest.mark.parametrize("relative", [False, True])
def test_cleanup_removes_managed_launcher_link_only(managed_wrapper, relative) -> None:
    wrapper, env = managed_wrapper
    target = LAUNCHER.resolve()
    original = target.read_bytes()
    link = wrapper / "review-pr.sh"
    link.symlink_to(os.path.relpath(target, wrapper) if relative else target)
    assert check(wrapper, env)["safe"] is True
    subprocess.run([SCRIPT, "queue-cleanup", wrapper], env=env, check=True, capture_output=True)
    assert not wrapper.exists()
    assert target.read_bytes() == original


@pytest.mark.parametrize("kind", ["file", "directory", "wrong-link", "broken-link"])
def test_cleanup_rejects_unmanaged_launcher_entry(managed_wrapper, kind) -> None:
    wrapper, env = managed_wrapper
    entry = wrapper / "review-pr.sh"
    if kind == "file":
        entry.write_text("keep me\n")
    elif kind == "directory":
        entry.mkdir()
    else:
        entry.symlink_to("/dev/null" if kind == "wrong-link" else wrapper / "missing")
    result = check(wrapper, env)
    assert result["safe"] is False
    assert "review-pr.sh" in str(result["reason"])
    cleanup = subprocess.run([SCRIPT, "queue-cleanup", wrapper], env=env, capture_output=True)
    assert cleanup.returncode != 0
    assert (wrapper / "rocm-systems/.git").exists()
    assert entry.exists() or entry.is_symlink()


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


def test_cleanup_removes_ruff_cache(managed_wrapper) -> None:
    wrapper, env = managed_wrapper
    cache = wrapper / ".ruff_cache" / "0.16.4"
    cache.mkdir(parents=True)
    (cache / "123456789").write_bytes(b"cached lint results")
    assert check(wrapper, env)["safe"] is True
    subprocess.run([SCRIPT, "queue-cleanup", wrapper], env=env, check=True, capture_output=True)
    assert not wrapper.exists()
    assert Path(env["ROCJITSU_MAIN_WORKSPACE"]).is_dir()


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


@pytest.mark.parametrize("dirty", [None, "tracked", "untracked"])
def test_cleanup_handles_initialized_submodule(managed_wrapper, tmp_path, dirty) -> None:
    wrapper, env = managed_wrapper
    repository = Path(env["ROCJITSU_MAIN_WORKSPACE"])
    checkout = wrapper / "rocm-systems"

    def git(*args):
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Review Queue Test",
                "-c",
                "user.email=review-queue@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "protocol.file.allow=always",
                *map(str, args),
            ],
            env=env,
            check=True,
            capture_output=True,
        )

    subrepo = tmp_path / "submodule-source"
    git("clone", repository, subrepo)
    git("-C", checkout, "submodule", "add", subrepo, "vendor")
    git("-C", checkout, "commit", "-am", "Add test submodule")
    if dirty:
        changed = (
            checkout
            / "vendor"
            / ("emulation/rocjitsu/CMakeLists.txt" if dirty == "tracked" else "notes.txt")
        )
        changed.write_text("keep my changes\n")
        assert check(wrapper, env)["safe"] is False
        result = subprocess.run([SCRIPT, "queue-cleanup", wrapper], env=env, capture_output=True)
        assert result.returncode != 0
        assert changed.read_text() == "keep my changes\n"
    else:
        assert check(wrapper, env)["safe"] is True
        subprocess.run([SCRIPT, "queue-cleanup", wrapper], env=env, check=True, capture_output=True)
        assert not wrapper.exists()
        assert subrepo.is_dir()


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


def test_saved_review_survives_managed_wrapper_cleanup(managed_wrapper, tmp_path) -> None:
    wrapper, env = managed_wrapper
    review_dir = tmp_path / "reviews" / "saved"
    review_bin = AGENT_WORKSPACE / "tools/peanut-review/bin/peanut-review"
    checkout = wrapper / "rocm-systems"
    source = checkout / "emulation/rocjitsu/CMakeLists.txt"
    source.write_text(source.read_text() + "# Review change\n")
    subprocess.run(
        [
            "git",
            "-C",
            checkout,
            "-c",
            "user.name=Review Queue Test",
            "-c",
            "user.email=review-queue@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-am",
            "Review change",
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            review_bin,
            "--session",
            review_dir,
            "init",
            "--workspace",
            wrapper,
            "--repo-relative",
            "rocm-systems",
            "--base",
            "HEAD~1",
            "--topic",
            "HEAD",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads((review_dir / "session.json").read_text())
    repository = Path(env["ROCJITSU_MAIN_WORKSPACE"])
    assert metadata["git_common_dir"] == str(repository / ".git")
    subprocess.run(
        [SCRIPT, "queue-cleanup", wrapper],
        env=env,
        check=True,
        capture_output=True,
    )
    assert not wrapper.exists()
    for args in (["reflog", "expire", "--expire=now", "--all"], ["gc", "--prune=now"]):
        subprocess.run(
            ["git", "-C", repository, *args],
            env=env,
            check=True,
            capture_output=True,
        )
    result = subprocess.run(
        [
            "git",
            "-C",
            metadata["git_common_dir"],
            "diff",
            f"{metadata['base_ref']}...{metadata['topic_ref']}",
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "+# Review change" in result.stdout
    subprocess.run(
        [review_bin, "--session", review_dir, "retain-git"],
        env=env,
        check=True,
        capture_output=True,
    )
