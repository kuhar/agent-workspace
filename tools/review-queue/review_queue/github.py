from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from .models import ProjectConfig, PullRequest


class GitHubError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubPoll:
    records: tuple[PullRequest, ...]
    query_numbers: frozenset[int]


class GitHubClient:
    fields = "number,url,title,author,headRefOid,headRefName,isDraft,updatedAt,reviewRequests,state"

    def __init__(self, *, timeout_seconds: int = 90):
        self.timeout_seconds = timeout_seconds

    async def _run(self, *command: str) -> str:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise GitHubError(f"missing command: {command[0]}") from error
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as error:
            process.kill()
            await process.wait()
            raise GitHubError(f"command timed out after {self.timeout_seconds}s") from error
        if process.returncode:
            detail = stderr.decode(errors="replace").strip()
            raise GitHubError(detail or f"{' '.join(command)} exited {process.returncode}")
        return stdout.decode()

    @staticmethod
    def _from_gh(project: ProjectConfig, item: dict[str, object]) -> PullRequest:
        author = item.get("author") or {}
        login = author.get("login") if isinstance(author, dict) else None
        return PullRequest(
            project=project.name,
            repo=project.repo,
            number=int(item["number"]),
            url=str(item["url"]),
            title=str(item["title"]),
            author=str(login or "unknown"),
            head_sha=str(item["headRefOid"]),
            head_ref=str(item["headRefName"]),
            updated_at=str(item["updatedAt"]),
            state=str(item.get("state", "OPEN")),
            is_draft=bool(item.get("isDraft", False)),
        )

    async def list_project(self, project: ProjectConfig) -> GitHubPoll:
        output = await self._run(
            "gh",
            "pr",
            "list",
            "--repo",
            project.repo,
            "--state",
            "open",
            "--search",
            project.query,
            "--limit",
            "1000",
            "--json",
            self.fields,
        )
        try:
            payload = json.loads(output)
            if not isinstance(payload, list):
                raise GitHubError("gh pr list returned a non-list payload")
            records = tuple(self._from_gh(project, item) for item in payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise GitHubError(f"invalid gh pr list response: {error}") from error
        return GitHubPoll(records=records, query_numbers=frozenset(pr.number for pr in records))

    async def view(self, project: ProjectConfig, spec: str | int) -> PullRequest:
        output = await self._run(
            "gh",
            "pr",
            "view",
            str(spec),
            "--repo",
            project.repo,
            "--json",
            self.fields,
        )
        try:
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise TypeError("expected an object")
            return self._from_gh(project, payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise GitHubError(f"invalid gh pr view response: {error}") from error
