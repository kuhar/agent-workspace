"""Git diff → structured file/line data for rendering."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field


@dataclass
class DiffLine:
    kind: str  # "context" | "added" | "deleted"
    old_lineno: int | None
    new_lineno: int | None
    content: str


@dataclass
class FileDiff:
    path: str
    status: str  # "A" | "M" | "D" | "R" | "?"
    lines: list[DiffLine] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    binary: bool = False


def _run_git(workspace: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", workspace, *args],
        capture_output=True, text=True, timeout=30,
    )
    # Non-zero is common for git diff with merges; swallow.
    return result.stdout


def _name_status(workspace: str, base: str, topic: str) -> dict[str, str]:
    """Map path → status letter (A/M/D/R). For renames we map the new path to R."""
    out = _run_git(workspace, "diff", "--name-status", f"{base}...{topic}")
    status_map: dict[str, str] = {}
    for line in out.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        status_letter = parts[0][0]
        # Rename/copy rows look like: "R100\told\tnew" — attribute to the new path.
        path = parts[-1]
        status_map[path] = status_letter
    return status_map


def parse_diff(workspace: str, base: str, topic: str) -> list[FileDiff]:
    """Return a FileDiff per changed file. Full-file context via -U99999."""
    status_map = _name_status(workspace, base, topic)
    raw = _run_git(
        workspace, "diff", "-U99999", "--no-color",
        f"{base}...{topic}",
    )

    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_ln = 0
    new_ln = 0

    for line in raw.split("\n"):
        if line.startswith("diff --git"):
            if current:
                files.append(current)
            current = None
            continue
        if line.startswith("Binary files"):
            # "Binary files a/foo differ"
            if current:
                current.binary = True
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                # Deletion — defer; path will come from status_map via the prior diff --git
                continue
            # Strip the leading "b/" prefix that git adds.
            path = raw_path[2:] if raw_path.startswith("b/") else raw_path
            current = FileDiff(path=path, status=status_map.get(path, "?"))
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if m:
                old_ln = int(m.group(1))
                new_ln = int(m.group(2))
            continue
        if current is None:
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file"
            continue
        if line.startswith("+"):
            current.lines.append(DiffLine("added", None, new_ln, line[1:]))
            current.additions += 1
            new_ln += 1
        elif line.startswith("-"):
            current.lines.append(DiffLine("deleted", old_ln, None, line[1:]))
            current.deletions += 1
            old_ln += 1
        else:
            content = line[1:] if line.startswith(" ") else line
            current.lines.append(DiffLine("context", old_ln, new_ln, content))
            old_ln += 1
            new_ln += 1

    if current:
        files.append(current)

    # Files that appear only in name-status (binary, or pure delete where we
    # couldn't extract the path from the +++ header) — surface as empty entries.
    diffed = {f.path for f in files}
    for path, status in status_map.items():
        if path not in diffed:
            files.append(FileDiff(path=path, status=status, binary=True))

    return files
