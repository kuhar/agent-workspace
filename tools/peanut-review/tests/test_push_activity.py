"""Tests for GitHub push activity shown on the session index."""
from peanut_review.models import Comment, GitHubPR, Session
from peanut_review.web.push_activity import summarize_push_activity


def _github_session(*, pushed_at: str | None = None) -> Session:
    return Session(
        id="review",
        github=GitHubPR(repo="acme/foo", number=42),
        last_github_push_at=pushed_at,
    )


def test_push_activity_reports_no_push():
    activity = summarize_push_activity(
        _github_session(),
        [Comment(author="vera", timestamp="2026-08-09T12:00:00+00:00")],
    )

    assert activity == {
        "status": "never",
        "label": "not pushed yet",
        "count": 0,
    }


def test_push_activity_only_counts_local_comments_created_after_push():
    comments = [
        # Deliberately omitted from the push: old, still local, and not new.
        Comment(author="vera", timestamp="2026-08-09T11:59:00+00:00"),
        Comment(author="felix", timestamp="2026-08-09T12:01:00+00:00"),
        Comment(
            author="gh:octocat",
            timestamp="2026-08-09T12:02:00Z",
            external_source="github",
            external_id="123",
        ),
        Comment(
            author="petra",
            timestamp="2026-08-09T12:03:00+00:00",
            deleted=True,
        ),
    ]

    activity = summarize_push_activity(
        _github_session(pushed_at="2026-08-09T12:00:00+00:00"),
        comments,
    )

    assert activity == {
        "status": "new",
        "label": "1 new comment since push",
        "count": 1,
    }


def test_push_activity_stays_neutral_for_legacy_push_without_watermark():
    activity = summarize_push_activity(
        _github_session(),
        [Comment(
            author="vera",
            external_source="github",
            external_id="123",
        )],
    )

    assert activity is None


def test_push_activity_is_hidden_when_nothing_is_new_or_session_is_local():
    pushed = _github_session(pushed_at="2026-08-09T12:00:00+00:00")
    assert summarize_push_activity(pushed, [
        Comment(author="vera", timestamp="2026-08-09T11:59:00+00:00"),
    ]) is None
    assert summarize_push_activity(Session(id="local"), []) is None
