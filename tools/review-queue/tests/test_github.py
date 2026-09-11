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


class FileGitHub(GitHubClient):
    def __init__(self, files: list[dict[str, str]]):
        super().__init__()
        self.item = {
            "number": 42,
            "url": "https://github.com/ROCm/rocm-systems/pull/42",
            "title": "Change",
            "author": {"login": "alice"},
            "headRefOid": "a" * 40,
            "baseRefOid": "b" * 40,
            "headRefName": "topic",
            "updatedAt": "2026-09-10T00:00:00Z",
            "state": "OPEN",
            "changedFiles": len(files),
        }
        self.pages = [files[:100], files[100:]]
        self.file_calls = 0
        self.change_during_read = False

    async def _run(self, *command: str) -> str:
        if command[:2] == ("gh", "api"):
            assert "--paginate" in command and "--slurp" in command
            self.file_calls += 1
            if self.change_during_read:
                self.item["headRefOid"] = "c" * 40
            return json.dumps(self.pages)
        if command[:3] == ("gh", "pr", "list"):
            return json.dumps([self.item])
        return json.dumps(self.item)


@pytest.mark.parametrize(
    "files, allowed",
    [
        ([{"filename": "docs/emulation/guide.md"}], False),
        ([{"filename": "emulation-other/foo.cpp"}], False),
        ([{"filename": "emulation/foo.cpp", "status": "removed"}], True),
        ([{"filename": "docs/foo.cpp", "previous_filename": "emulation/foo.cpp"}], True),
        ([{"filename": "emulation/foo.cpp", "previous_filename": "docs/foo.cpp"}], True),
        ([], False),
    ],
)
async def test_required_directory_filters_list_and_manual_view(project, files, allowed):
    from dataclasses import replace

    project = replace(project, include_paths=("emulation/",))
    client = FileGitHub(files)
    poll = await client.list_project(project)
    assert poll.query_numbers == {42}
    assert poll.records[0].path_filter_passed is allowed
    assert poll.records[0].path_filter_key == project.path_filter_key
    viewed = await client.view(project, 42)
    assert viewed.path_filter_passed is allowed
    assert client.file_calls == 1


async def test_path_filter_reads_later_pages_and_invalidates_cache(project):
    from dataclasses import replace

    project = replace(project, include_paths=("emulation/",))
    client = FileGitHub(
        [{"filename": f"docs/{n}"} for n in range(100)]
        + [
            {"filename": "emulation/last.cpp"},
        ]
    )
    assert (await client.list_project(project)).records[0].path_filter_passed
    client.item["baseRefOid"] = "d" * 40
    client.pages[-1] = [{"filename": "docs/last.cpp"}]
    assert not (await client.list_project(project)).records[0].path_filter_passed
    client.item["headRefOid"] = "e" * 40
    client.pages[-1] = [{"filename": "emulation/last.cpp"}]
    assert (await client.list_project(project)).records[0].path_filter_passed
    assert client.file_calls == 3


@pytest.mark.parametrize("failure", ["incomplete", "changed", "invalid", "duplicate"])
async def test_path_filter_fails_closed(project, failure):
    from dataclasses import replace

    project = replace(project, include_paths=("emulation/",))
    client = FileGitHub([{"filename": "emulation/foo.cpp"}])
    if failure == "incomplete":
        client.item["changedFiles"] = 3001
    elif failure == "changed":
        client.change_during_read = True
    elif failure == "invalid":
        client.pages = [[{"filename": None}]]
    elif failure == "duplicate":
        client.item["changedFiles"] = 2
        client.pages = [[{"filename": "emulation/foo.cpp"}] * 2]
    with pytest.raises(GitHubError):
        await client.list_project(project)
    assert not client._path_cache
