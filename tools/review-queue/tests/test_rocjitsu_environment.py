from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_cleanup_script import LAUNCHER


def test_launcher_environment_runs_shared_asan_binary(tmp_path: Path) -> None:
    compiler = shutil.which("clang++-23")
    if compiler is None:
        pytest.skip("clang++-23 is required for the sanitizer runtime integration test")
    source = tmp_path / "probe.cpp"
    source.write_text("int main() { return 0; }\n")
    binary = tmp_path / "probe"
    subprocess.run(
        [compiler, "-fsanitize=address", "-shared-libsan", str(source), "-o", str(binary)],
        check=True,
        capture_output=True,
    )
    # Exercise the actual setup function without fetching a PR or launching reviewers.
    script = LAUNCHER.read_text()
    setup = (
        "setup_rocjitsu_env() {"
        + script.split("setup_rocjitsu_env() {", 1)[1].split("\n}\n", 1)[0]
        + "\n}\n"
    )
    env = os.environ.copy()
    env.pop("LD_LIBRARY_PATH", None)
    env.update(
        REVIEW_PARENT=str(tmp_path),
        BUILD_DIR=str(tmp_path / "build"),
        VENV_DIR=str(tmp_path / "venv"),
        ASAN_OPTIONS="detect_leaks=0",
    )
    result = subprocess.run(
        ["bash", "-euc", setup + '\nsetup_rocjitsu_env\nexec "$1"', "probe", str(binary)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
