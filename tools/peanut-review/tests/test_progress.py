"""Tests for agent-derived web progress labels."""
from peanut_review.models import AgentConfig
from peanut_review.web.progress import summarize_agent_progress


def _agent(status: str, *, role: str = "reviewer") -> AgentConfig:
    return AgentConfig(name="agent", status=status, role=role)


def test_progress_counts_running_reviewers():
    progress = summarize_agent_progress([
        _agent("running"),
        _agent("running"),
        _agent("running"),
        _agent("running"),
        _agent("pending", role="curator"),
    ])

    assert progress == {
        "label": "4 review agents running",
        "status": "running",
    }


def test_progress_reports_reviewers_done_before_curator_runs():
    progress = summarize_agent_progress([
        _agent("done"),
        _agent("done"),
        _agent("pending", role="curator"),
    ])

    assert progress == {"label": "review agents done", "status": "done"}


def test_progress_reports_curator_running_and_done():
    reviewers = [_agent("done"), _agent("done")]

    assert summarize_agent_progress([
        *reviewers, _agent("running", role="curator"),
    ]) == {"label": "curator running", "status": "running"}
    assert summarize_agent_progress([
        *reviewers, _agent("done", role="curator"),
    ]) == {"label": "curator done", "status": "done"}


def test_progress_prefers_rerunning_reviewer_over_prior_curator_result():
    progress = summarize_agent_progress([
        _agent("running"),
        _agent("done"),
        _agent("done", role="curator"),
    ])

    assert progress == {
        "label": "1 review agent running",
        "status": "running",
    }


def test_progress_surfaces_failures_when_nothing_is_running():
    assert summarize_agent_progress([
        _agent("done"), _agent("failed"),
    ]) == {"label": "1 review agent failed", "status": "failed"}
    assert summarize_agent_progress([
        _agent("done"), _agent("timeout", role="curator"),
    ]) == {"label": "curator failed", "status": "failed"}
