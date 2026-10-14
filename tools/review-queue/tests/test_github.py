from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from review_queue.github import GitHubClient, GitHubError, PullRequestNotFound
from review_queue.models import ProjectConfig


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [
        (1, b"", b"invalid character 'd' after object key"),
        (1, b"", b"unexpected end of JSON input"),
        (0, b'{"{"data":{}}', b""),
    ],
)
async def test_malformed_json_retries_identical_read_with_private_cache(
    monkeypatch, tmp_path, returncode, stdout, stderr
):
    cache = tmp_path / "shared-cache"
    cache.mkdir()
    poisoned = cache / "schema"
    poisoned.write_bytes(b'{"{"data":{}}')
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("GH_TOKEN", "test-credential")
    calls = []
    processes = [
        Mock(returncode=returncode, communicate=AsyncMock(return_value=(stdout, stderr))),
        Mock(returncode=0, communicate=AsyncMock(return_value=(b"[]", b""))),
    ]

    async def spawn(*command, **kwargs):
        calls.append((command, kwargs["env"]))
        if len(calls) == 2:
            fresh = Path(kwargs["env"]["XDG_CACHE_HOME"])
            assert fresh.is_dir() and not list(fresh.iterdir())
        return processes[len(calls) - 1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    command = ("gh", "pr", "list", "--search", "draft:false -author:@me", "--json", "number")
    assert await GitHubClient()._run(*command) == "[]"
    assert len(calls) == 2
    assert calls[0] == (command, None)
    assert calls[1][0] == command
    retry_env = calls[1][1]
    assert retry_env == dict(os.environ, XDG_CACHE_HOME=retry_env["XDG_CACHE_HOME"])
    assert retry_env["XDG_CACHE_HOME"] != str(cache)
    assert not Path(retry_env["XDG_CACHE_HOME"]).exists()
    assert os.environ["XDG_CACHE_HOME"] == str(cache)
    assert poisoned.read_bytes() == b'{"{"data":{}}'


@pytest.mark.parametrize(
    ("stderr", "attempts", "message"),
    [
        (b"invalid character 'd' after object key", 2, "fresh GitHub CLI cache retry failed"),
        (b"HTTP 401: Bad credentials", 1, "gh pr list: HTTP 401"),
        (b"API rate limit exceeded", 1, "gh pr list: API rate limit exceeded"),
    ],
)
async def test_cache_retry_is_bounded_and_does_not_retry_auth_or_rate_errors(
    monkeypatch, stderr, attempts, message
):
    process = Mock(returncode=1, communicate=AsyncMock(return_value=(b"", stderr)))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(GitHubError, match=message):
        await GitHubClient()._run("gh", "pr", "list")
    assert spawn.await_count == attempts


@pytest.mark.parametrize(
    ("detail", "error_type"),
    [
        (
            "GraphQL: Could not resolve to a PullRequest with the number of 12245. "
            "(repository.pullRequest)",
            PullRequestNotFound,
        ),
        ("GraphQL: Could not resolve to a Repository with the name 'owner/repo'.", GitHubError),
        ("HTTP 401: Bad credentials", GitHubError),
        ("API rate limit exceeded", GitHubError),
        (
            "GraphQL: Could not resolve to a PullRequest with the number of 12245. "
            "(repository.pullRequest)\nHTTP 502: Bad Gateway",
            GitHubError,
        ),
    ],
)
async def test_only_confirmed_missing_pr_is_classified_as_not_found(
    monkeypatch, project, detail, error_type
):
    process = Mock(returncode=1, communicate=AsyncMock(return_value=(b"", detail.encode())))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(error_type) as raised:
        await GitHubClient().view(project, "12245")
    assert type(raised.value) is error_type


async def test_missing_pr_after_fresh_cache_retry_preserves_error_type(monkeypatch, project):
    detail = (
        b"GraphQL: Could not resolve to a PullRequest with the number of 12245. "
        b"(repository.pullRequest)"
    )
    processes = [
        Mock(returncode=1, communicate=AsyncMock(return_value=(b"", b"invalid character 'x'"))),
        Mock(returncode=1, communicate=AsyncMock(return_value=(b"", detail))),
    ]
    spawn = AsyncMock(side_effect=processes)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(PullRequestNotFound):
        await GitHubClient().view(project, "12245")
    assert spawn.await_count == 2


@pytest.mark.parametrize("cancelled", [False, True])
async def test_timeout_or_cancellation_reaps_command_without_retry(monkeypatch, cancelled):
    process = Mock(
        returncode=None,
        communicate=AsyncMock(side_effect=asyncio.CancelledError if cancelled else TimeoutError),
        wait=AsyncMock(),
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(asyncio.CancelledError if cancelled else GitHubError):
        await GitHubClient()._run("gh", "pr", "list")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    spawn.assert_awaited_once()


def approval_payload(head="a" * 40, nodes=()):
    return {
        "data": {
            "viewer": {"login": "me"},
            "repository": {
                "pullRequest": {
                    "headRefOid": head,
                    "latestOpinionatedReviews": {
                        "nodes": list(nodes),
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            },
        }
    }


class StubGitHub(GitHubClient):
    def __init__(self, payload: object):
        super().__init__()
        self.payload = payload
        self.commands: list[tuple[str, ...]] = []

    async def _run(self, *command: str) -> str:
        self.commands.append(command)
        if command[:3] == ("gh", "api", "graphql"):
            item = self.payload[0] if isinstance(self.payload, list) else self.payload
            return json.dumps(approval_payload(item["headRefOid"]))
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
        if command[:3] == ("gh", "api", "graphql"):
            return json.dumps(approval_payload(self.item["headRefOid"]))
        if command[:2] == ("gh", "api"):
            assert "--paginate" in command and "--slurp" in command
            self.file_calls += 1
            if self.change_during_read:
                self.item["headRefOid"] = "c" * 40
            return json.dumps(self.pages)
        if command[:3] == ("gh", "pr", "list"):
            return json.dumps([self.item])
        return json.dumps(self.item)


async def test_approvals_distinguish_viewer_and_other_current_approvers(project):
    class Reviews(FileGitHub):
        nodes = [
            {"author": {"login": login}, "state": state}
            for login, state in (
                ("me", "APPROVED"),
                ("alice", "APPROVED"),
                ("bob", "CHANGES_REQUESTED"),
                ("carol", "DISMISSED"),
                ("dave", "COMMENTED"),
            )
        ]

        async def _run(self, *command):
            if command[:3] == ("gh", "api", "graphql"):
                assert "latestOpinionatedReviews" in command[4]
                return json.dumps(approval_payload(nodes=self.nodes))
            return await super()._run(*command)

    client = Reviews([])
    listed = (await client.list_project(project)).records[0]
    assert listed.approval_viewer == "me"
    assert listed.approved_by == ("alice", "me")
    client.nodes = [{"author": {"login": "alice"}, "state": "APPROVED"}]
    viewed = await client.view(project, 42)
    assert viewed.approved_by == ("alice",)  # Refresh on the same head removes old approvals.


async def test_approvals_paginate_and_reject_partial_or_changed_responses(project):
    class Reviews(FileGitHub):
        broken = False
        changed = False

        async def _run(self, *command):
            if command[:3] != ("gh", "api", "graphql"):
                return await super()._run(*command)
            second = "cursor=next" in command
            payload = approval_payload(
                head="b" * 40 if self.changed else "a" * 40,
                nodes=[{"author": {"login": "alice" if second else "me"}, "state": "APPROVED"}],
            )
            if not second:
                page = payload["data"]["repository"]["pullRequest"]["latestOpinionatedReviews"][
                    "pageInfo"
                ]
                page.update(hasNextPage=True, endCursor="next")
            elif self.broken:
                return json.dumps({"errors": [{"message": "rate limited"}]})
            return json.dumps(payload)

    client = Reviews([])
    assert (await client.view(project, 42)).approved_by == ("alice", "me")
    client.broken = True
    with pytest.raises(GitHubError, match="review query errors"):
        await client.view(project, 42)
    client.changed = True
    with pytest.raises(GitHubError, match="PR changed"):
        await client.view(project, 42)


@pytest.mark.parametrize(
    "files, allowed",
    [
        ([{"filename": "docs/emulation/guide.md"}], False),
        ([{"filename": "emulation-other/foo.cpp"}], False),
        ([{"filename": "website-other/App.jsx"}], False),
        ([{"filename": "docs/website/guide.md"}], False),
        ([{"filename": "website/src/App.jsx"}], True),
        ([{"filename": "emulation/rocjitsu/website/src/App.jsx"}], True),
        ([{"filename": "emulation/foo.cpp", "status": "removed"}], True),
        ([{"filename": "docs/foo.cpp", "previous_filename": "emulation/foo.cpp"}], True),
        ([{"filename": "emulation/foo.cpp", "previous_filename": "docs/foo.cpp"}], True),
        ([], False),
    ],
)
async def test_required_directory_filters_list_and_manual_view(project, files, allowed):
    from dataclasses import replace

    project = replace(project, include_paths=("emulation/", "website/"))
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


@pytest.mark.parametrize(
    "matching_file",
    [
        {"filename": "emulation/foo.cpp"},
        {"filename": "emulation/foo.cpp", "status": "removed"},
        {"filename": "docs/foo.cpp", "previous_filename": "emulation/foo.cpp"},
    ],
)
async def test_capped_file_list_accepts_confirmed_match_and_caches(project, matching_file):
    from dataclasses import replace

    project = replace(project, include_paths=("emulation/",))
    client = FileGitHub([{"filename": f"docs/{n}"} for n in range(2999)] + [matching_file])
    client.item["changedFiles"] = 3019
    assert (await client.list_project(project)).records[0].path_filter_passed
    assert (await client.view(project, 42)).path_filter_passed
    assert client.file_calls == 1


@pytest.mark.parametrize("failure", ["no-match", "changed", "invalid", "duplicate"])
async def test_capped_file_list_does_not_hide_missing_evidence_or_bad_data(project, failure):
    from dataclasses import replace

    project = replace(project, include_paths=("emulation/",))
    client = FileGitHub(
        [{"filename": "emulation/foo.cpp"}]
        + [{"filename": f"docs/{n}"} for n in range(2999)]
    )
    client.item["changedFiles"] = 3019
    if failure == "no-match":
        client.pages[0][0] = {"filename": "docs/foo.cpp"}
        reason = "3000-file limit without a matching path"
    elif failure == "changed":
        client.change_during_read = True
        reason = "PR changed"
    elif failure == "invalid":
        client.pages[-1][-1] = {"filename": None}
        reason = "invalid PR filename"
    else:
        client.pages[-1][-1] = client.pages[0][0]
        reason = "duplicate entries"
    with pytest.raises(GitHubError, match=reason):
        await client.list_project(project)
    assert not client._path_cache


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
