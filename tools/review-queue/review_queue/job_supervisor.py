from __future__ import annotations

import argparse
import ctypes
import os
import signal
import subprocess
import time
from contextlib import suppress


def _set_parent_death_signal() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def supervise(command: list[str], grace_seconds: float) -> int:
    parent = os.getppid()
    _set_parent_death_signal()
    if os.getppid() != parent:
        return 143

    child: subprocess.Popen[bytes] | None = None
    requested_signal = 0
    deadline: float | None = None

    def stop(signum: int, _frame: object) -> None:
        nonlocal requested_signal, deadline
        if requested_signal:
            return
        requested_signal = signum
        deadline = time.monotonic() + grace_seconds
        if child is not None and child.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, stop)
    child = subprocess.Popen(command, start_new_session=True)

    while child.poll() is None:
        if requested_signal and deadline is not None and time.monotonic() >= deadline:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            deadline = None
        time.sleep(0.1)
    return 128 + requested_signal if requested_signal else int(child.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grace-seconds", type=float, default=30.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    return supervise(command, args.grace_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
