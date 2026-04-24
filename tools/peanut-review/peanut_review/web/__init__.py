"""Browser-based human review UI for peanut-review sessions.

Humans and agents share the same session storage: comments land in the
session's JSONL files via peanut_review.store regardless of who authored
them. The server is session-indexed from day 1
(/sessions/<id>/...) so a future multi-session daemon is a one-line
change (drop the single-session redirect in app.py).
"""
