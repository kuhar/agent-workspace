"""Tests for signal/wait primitives."""
import tempfile
import threading
import time
from pathlib import Path

from peanut_review.polling import (
    check_signal,
    signal_all,
    wait_all_signals,
    wait_signal,
    write_signal,
)


def _make_session() -> str:
    d = tempfile.mkdtemp(prefix="pr-test-")
    (Path(d) / "signals").mkdir()
    return d


def test_signal_write_and_check():
    sd = _make_session()
    assert not check_signal(sd, "vera", "round-done")
    write_signal(sd, "vera", "round-done")
    assert check_signal(sd, "vera", "round-done")


def test_wait_signal_immediate():
    sd = _make_session()
    write_signal(sd, "vera", "round-done")
    assert wait_signal(sd, "vera", "round-done", timeout=1)


def test_wait_signal_timeout():
    sd = _make_session()
    assert not wait_signal(sd, "vera", "round-done", timeout=0.1, poll_interval=0.05)


def test_wait_signal_delayed():
    sd = _make_session()

    def signal_later():
        time.sleep(0.2)
        write_signal(sd, "vera", "round-done")

    t = threading.Thread(target=signal_later)
    t.start()

    assert wait_signal(sd, "vera", "round-done", timeout=5, poll_interval=0.1)
    t.join()


def test_signal_all_and_wait_all():
    sd = _make_session()
    agents = ["vera", "felix", "petra"]
    signal_all(sd, agents, "round-done")

    timed_out = wait_all_signals(sd, agents, "round-done", timeout=1)
    assert timed_out == []


def test_wait_all_partial_timeout():
    sd = _make_session()
    write_signal(sd, "vera", "round-done")
    # felix and petra never signal

    timed_out = wait_all_signals(
        sd, ["vera", "felix", "petra"], "round-done",
        timeout=0.2, poll_interval=0.05,
    )
    assert set(timed_out) == {"felix", "petra"}
