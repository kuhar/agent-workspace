from __future__ import annotations

import json

import pytest

from review_queue.github import GitHubClient, GitHubError
from review_queue.models import ProjectConfig


class StubGitHub(GitHubClient):
    def __init__(self, payload: object):
        super().__init__()
        self.payload = payload
        self.commands: list[tuple[str, ...]] = []

    async def _run(self, *command: str) -> str:
        self.commands.append(command)
        return json.dumps(self.payload)


async def test_list_project_uses_exact_head_identity(project: ProjectConfig) -> None:
    client = StubGitHub(
        [
            {
                "number": 42,
                "url": "https://github.com/ROCm/rocm-systems/pull/42",
                "title": "Change",
                "author": {"login": "alice"},
                "headRefOid": "f" * 40,
                "headRefName": "users/alice/change",
                "isDraft": False,
                "updatedAt": "2026-08-22T00:00:00Z",
                "reviewRequests": [],
                "state": "OPEN",
            }
        ]
    )
    poll = await client.list_project(project)
    assert poll.query_numbers == {42}
    assert poll.records[0].head_sha == "f" * 40
    command = client.commands[0]
    assert any("team-review-requested:ROCm/rocjitsu-core-team" in argument for argument in command)
    assert "headRefOid" in command[-1]


async def test_malformed_list_response_does_not_change_query_semantics(
    project: ProjectConfig,
) -> None:
    client = StubGitHub([])
    client.payload = object()

    async def malformed(*_command: str) -> str:
        return "not json"

    client._run = malformed  # type: ignore[method-assign]
    with pytest.raises(GitHubError, match="invalid gh pr list response"):
        await client.list_project(project)


async def test_list_command_failure_does_not_change_discovery_mode(
    project: ProjectConfig,
) -> None:
    class FailingGitHub(GitHubClient):
        async def _run(self, *_command: str) -> str:
            raise GitHubError("search unavailable")

    with pytest.raises(GitHubError, match="search unavailable"):
        await FailingGitHub().list_project(project)
