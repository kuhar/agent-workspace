"""Tests for signal/wait/ask/reply primitives."""
import tempfile
import threading
import time
from pathlib import Path

from peanut_review.polling import (
    check_signal,
    list_unanswered,
    signal_all,
    wait_all_signals,
    wait_reply,
    wait_signal,
    write_question,
    write_reply,
    write_signal,
)


def _make_session() -> str:
    d = tempfile.mkdtemp(prefix="pr-test-")
    for sub in ["signals", "messages"]:
        (Path(d) / sub).mkdir()
    return d


def test_signal_write_and_check():
    sd = _make_session()
    assert not check_signal(sd, "vera", "round1-done")
    write_signal(sd, "vera", "round1-done")
    assert check_signal(sd, "vera", "round1-done")


def test_wait_signal_immediate():
    sd = _make_session()
    write_signal(sd, "vera", "round1-done")
    assert wait_signal(sd, "vera", "round1-done", timeout=1)


def test_wait_signal_timeout():
    sd = _make_session()
    assert not wait_signal(sd, "vera", "round1-done", timeout=0.1, poll_interval=0.05)


def test_wait_signal_delayed():
    sd = _make_session()

    def signal_later():
        time.sleep(0.2)
        write_signal(sd, "vera", "round1-done")

    t = threading.Thread(target=signal_later)
    t.start()

    assert wait_signal(sd, "vera", "round1-done", timeout=5, poll_interval=0.1)
    t.join()


def test_signal_all_and_wait_all():
    sd = _make_session()
    agents = ["vera", "felix", "petra"]
    signal_all(sd, agents, "round1-done")

    timed_out = wait_all_signals(sd, agents, "round1-done", timeout=1)
    assert timed_out == []


def test_wait_all_partial_timeout():
    sd = _make_session()
    write_signal(sd, "vera", "round1-done")
    # felix and petra never signal

    timed_out = wait_all_signals(
        sd, ["vera", "felix", "petra"], "round1-done",
        timeout=0.2, poll_interval=0.05,
    )
    assert set(timed_out) == {"felix", "petra"}


def test_question_and_reply():
    sd = _make_session()
    q = write_question(sd, "vera", "Where is the build dir?")
    assert q.id == "q_001"
    assert q.agent == "vera"

    # Verify question file exists
    qpath = Path(sd) / "messages" / "vera" / "q_001.json"
    assert qpath.exists()

    # Reply
    write_reply(sd, "vera", q.id, "It's in ../build-release/")

    # Read reply directly
    reply_path = qpath.with_suffix(".reply")
    assert reply_path.exists()


def test_wait_reply_immediate():
    sd = _make_session()
    q = write_question(sd, "vera", "Question?")
    write_reply(sd, "vera", q.id, "Answer!")

    reply = wait_reply(sd, "vera", q.id, timeout=1)
    assert reply is not None
    assert reply.answer == "Answer!"


def test_wait_reply_timeout():
    sd = _make_session()
    q = write_question(sd, "vera", "Question?")
    reply = wait_reply(sd, "vera", q.id, timeout=0.1, poll_interval=0.05)
    assert reply is None


def test_wait_reply_delayed():
    sd = _make_session()
    q = write_question(sd, "vera", "Help?")

    def reply_later():
        time.sleep(0.2)
        write_reply(sd, "vera", q.id, "Here you go")

    t = threading.Thread(target=reply_later)
    t.start()

    reply = wait_reply(sd, "vera", q.id, timeout=5, poll_interval=0.1)
    assert reply is not None
    assert reply.answer == "Here you go"
    t.join()


def test_list_unanswered():
    sd = _make_session()
    write_question(sd, "vera", "Q1")
    q2 = write_question(sd, "vera", "Q2")
    write_question(sd, "felix", "Q3")

    # Answer vera's second question
    write_reply(sd, "vera", q2.id, "A2")

    unanswered = list_unanswered(sd)
    assert len(unanswered) == 2  # vera/q_001 and felix/q_001

    vera_q = list_unanswered(sd, agent="vera")
    assert len(vera_q) == 1
    assert vera_q[0].question == "Q1"


def test_multiple_questions_sequencing():
    sd = _make_session()
    q1 = write_question(sd, "vera", "First")
    q2 = write_question(sd, "vera", "Second")
    q3 = write_question(sd, "vera", "Third")

    assert q1.id == "q_001"
    assert q2.id == "q_002"
    assert q3.id == "q_003"
