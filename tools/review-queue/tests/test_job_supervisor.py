from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_supervisor_terminates_child_process_group(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "review_queue.job_supervisor",
            "--grace-seconds",
            "0.2",
            "--",
            "bash",
            "-c",
            f"echo $$ > {child_pid_file}; exec sleep 60",
        ]
    )
    deadline = time.monotonic() + 5
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert child_pid_file.exists()
    child_pid = int(child_pid_file.read_text())

    process.terminate()
    assert process.wait(timeout=5) == 143
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
