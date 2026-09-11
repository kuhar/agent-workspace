from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace

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
        self._file_checks = asyncio.Semaphore(4)
        self._path_cache: dict[tuple[object, ...], bool] = {}

    def _fields(self, project: ProjectConfig) -> str:
        return self.fields + (",baseRefOid,changedFiles" if project.include_paths else "")

    async def _filter_paths(
        self, project: ProjectConfig, record: PullRequest, item: dict[str, object]
    ) -> PullRequest:
        if not project.include_paths or record.state != "OPEN" or record.is_draft:
            return record
        base = item.get("baseRefOid")
        count = item.get("changedFiles")
        if not isinstance(base, str) or not base or type(count) is not int or count < 0:
            raise GitHubError("missing base/head file-count metadata for path filtering")
        key = (project.repo, record.number, record.head_sha, base, count, project.include_paths)
        async with self._file_checks:
            if key not in self._path_cache:
                try:
                    pages = json.loads(
                        await self._run(
                            "gh",
                            "api",
                            "--paginate",
                            "--slurp",
                            f"repos/{project.repo}/pulls/{record.number}/files?per_page=100",
                        )
                    )
                    if not isinstance(pages, list) or any(
                        not isinstance(page, list) for page in pages
                    ):
                        raise ValueError("expected paginated file arrays")
                    files = [file for page in pages for file in page]
                    if len(files) != count:
                        raise ValueError(
                            f"incomplete PR file list: expected {count}, got {len(files)}"
                        )
                    paths = []
                    for file in files:
                        if not isinstance(file, dict) or not isinstance(file.get("filename"), str):
                            raise ValueError("invalid PR filename")
                        paths.append(file["filename"])
                        if "previous_filename" in file:
                            if not isinstance(file["previous_filename"], str):
                                raise ValueError("invalid previous PR filename")
                            paths.append(file["previous_filename"])
                    if len({file["filename"] for file in files}) != count:
                        raise ValueError("duplicate entries in PR file list")
                    current = json.loads(
                        await self._run(
                            "gh",
                            "pr",
                            "view",
                            str(record.number),
                            "--repo",
                            project.repo,
                            "--json",
                            "headRefOid,baseRefOid,changedFiles",
                        )
                    )
                    if not isinstance(current, dict) or (
                        current.get("headRefOid"),
                        current.get("baseRefOid"),
                        current.get("changedFiles"),
                    ) != (record.head_sha, base, count):
                        raise ValueError("PR changed while checking file paths; retry next poll")
                    matches = any(path.startswith(project.include_paths) for path in paths)
                except (json.JSONDecodeError, TypeError, ValueError) as error:
                    raise GitHubError(
                        f"cannot filter {record.repo}#{record.number}: {error}"
                    ) from error
                if len(self._path_cache) >= 2048:
                    self._path_cache.pop(next(iter(self._path_cache)))
                self._path_cache[key] = matches
        return replace(
            record,
            path_filter_key=project.path_filter_key,
            path_filter_passed=self._path_cache[key],
        )

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
            self._fields(project),
        )
        try:
            payload = json.loads(output)
            if not isinstance(payload, list):
                raise GitHubError("gh pr list returned a non-list payload")
            records = tuple(
                await asyncio.gather(
                    *(
                        self._filter_paths(project, self._from_gh(project, item), item)
                        for item in payload
                    )
                )
            )
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
            self._fields(project),
        )
        try:
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise TypeError("expected an object")
            return await self._filter_paths(project, self._from_gh(project, payload), payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise GitHubError(f"invalid gh pr view response: {error}") from error
