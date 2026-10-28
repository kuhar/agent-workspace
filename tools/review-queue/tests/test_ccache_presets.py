from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_cleanup_script import JAKUB_ENV_ROOT


def test_direct_builds_share_ccache_across_wrappers(tmp_path: Path) -> None:
    for tool in ("cmake", "ninja", "ccache", "clang-23", "clang++-23"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is required for the compiler cache integration test")

    # Direct Ninja builds must work without the preset or direnv environment.
    env = {key: value for key, value in os.environ.items() if not key.startswith("CCACHE_")}
    config = tmp_path / "ccache.conf"
    config.write_text("")
    env.update(CCACHE_DIR=str(tmp_path / "cache"), CCACHE_CONFIGPATH=str(config))

    def run(*args: str | Path, cwd: Path | None = None) -> str:
        return subprocess.run(
            args, cwd=cwd, env=env, check=True, capture_output=True, text=True
        ).stdout

    def stats() -> dict[str, int]:
        return {
            key: int(value)
            for key, value in (line.split() for line in run("ccache", "--print-stats").splitlines())
        }

    for name in ("pr-1", "pr-2"):
        source = tmp_path / name / "rocm-systems/emulation/rocjitsu"
        source.mkdir(parents=True)
        shutil.copy2(
            JAKUB_ENV_ROOT / "worktree-scripts/rocjitsu/CMakePresets.json",
            source / "CMakePresets.json",
        )
        (source / "CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.25)\n"
            "project(CacheProbe LANGUAGES C CXX)\n"
            "add_library(probe OBJECT probe.c probe.cpp)\n"
            "target_include_directories(probe PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})\n"
        )
        (source / "value.h").write_text("#define VALUE 42\n")
        for suffix in ("c", "cpp"):
            (source / f"probe.{suffix}").write_text(
                '#include "value.h"\nint value(void) { return VALUE; }\n'
            )
        run("cmake", "--preset", "default", cwd=source)
        # Ignore compiler detection: measure only compilation of our objects.
        before = stats()
        run("ninja", "-C", tmp_path / name / "build")
        after = stats()
        if name == "pr-1":
            assert after["cache_miss"] - before["cache_miss"] == 2
        else:
            assert after["direct_cache_hit"] - before["direct_cache_hit"] == 2
            assert after["cache_miss"] == before["cache_miss"]

    # Path normalization must still notice changed headers.
    (source / "value.h").write_text("#define VALUE 43\n")
    run("ninja", "-C", tmp_path / "pr-2/build")
    assert stats()["cache_miss"] - after["cache_miss"] == 2
