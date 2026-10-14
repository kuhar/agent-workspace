from __future__ import annotations

import errno
from pathlib import Path

import pytest

from review_queue.util import process_has_token, process_start_ticks


@pytest.mark.parametrize(
    ("check", "reader", "expected"),
    [
        (lambda: process_start_ticks(424242), "read_text", None),
        (lambda: process_has_token(424242, "owned-test-token"), "read_bytes", False),
    ],
)
def test_exited_process_during_proc_read_is_not_live(monkeypatch, check, reader, expected):
    original = getattr(Path, reader)

    def read(path, *args, **kwargs):
        if str(path).startswith("/proc/424242/"):
            raise ProcessLookupError(errno.ESRCH, "process exited during ownership check")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, reader, read)
    assert check() is expected
