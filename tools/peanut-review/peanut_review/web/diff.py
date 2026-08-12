"""Build a bounded, revision-pinned Git diff model for web rendering."""
from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


DIFF_CONTEXT_LINES = 32


@dataclass(frozen=True)
class DiffLine:
    kind: str  # "context" | "added" | "deleted"
    old_lineno: int | None
    new_lineno: int | None
    content: str
    # Position in the logical full-context diff. Unlike a list offset this
    # remains stable when unchanged ranges are represented by DiffGap.
    full_index: int = 0


@dataclass(frozen=True)
class DiffGap:
    """An unchanged range omitted from the bounded Git diff."""

    start_index: int
    count: int
    old_start: int
    new_start: int

    @property
    def end_index(self) -> int:
        return self.start_index + self.count


@dataclass
class FileDiff:
    path: str
    status: str  # "A" | "M" | "D" | "R" | "?"
    lines: list[DiffLine] = field(default_factory=list)
    gaps: list[DiffGap] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    binary: bool = False
    total_lines: int = 0
    workspace: str = ""
    topic_oid: str = ""
    blob_oid: str = ""


def _run_git(workspace: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", workspace, *args],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {detail}"
        )
    return result.stdout


def _resolve_commit(workspace: str, ref: str) -> str:
    return _run_git(workspace, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def _name_status(workspace: str, base: str, topic: str) -> dict[str, str]:
    """Map rendered path to status letter, attributing renames to the new path."""
    out = subprocess.run(
        [
            "git", "-C", workspace, "diff", "--name-status", "-z",
            f"{base}...{topic}",
        ],
        capture_output=True, timeout=30,
    )
    if out.returncode != 0:
        detail = (out.stderr or out.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"git diff --name-status failed: {detail}")
    fields = out.stdout.split(b"\0")
    status_map: dict[str, str] = {}
    idx = 0
    while idx < len(fields) and fields[idx]:
        status = fields[idx].decode(errors="replace")
        idx += 1
        if status[:1] in {"R", "C"}:
            if idx + 1 >= len(fields):
                break
            idx += 1  # old path
            path = fields[idx].decode(errors="replace")
            idx += 1
        else:
            if idx >= len(fields):
                break
            path = fields[idx].decode(errors="replace")
            idx += 1
        status_map[path] = status[:1]
    return status_map


def _tree_blob_oids(workspace: str, ref: str, paths: set[str]) -> dict[str, str]:
    if not paths:
        return {}
    result = subprocess.run(
        [
            "git", "-C", workspace, "ls-tree", "-r", "-z", ref, "--",
            *sorted(paths),
        ],
        capture_output=True, timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode(errors="replace").strip()
        raise RuntimeError(f"git ls-tree failed: {detail}")
    found: dict[str, str] = {}
    for record in result.stdout.split(b"\0"):
        if not record or b"\t" not in record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        path = raw_path.decode(errors="replace")
        if path not in paths:
            continue
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == b"blob":
            found[path] = parts[2].decode("ascii")
    return found


def _blob_line_counts(workspace: str, oids: set[str]) -> dict[str, int]:
    """Count blob lines in one cat-file process without retaining blob contents."""
    if not oids:
        return {}
    ordered = sorted(oids)
    proc = subprocess.Popen(
        ["git", "-C", workspace, "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    for oid in ordered:
        proc.stdin.write(oid.encode("ascii") + b"\n")
    proc.stdin.close()

    counts: dict[str, int] = {}
    try:
        for requested_oid in ordered:
            header = proc.stdout.readline()
            parts = header.split()
            if len(parts) < 3 or parts[1] != b"blob":
                raise RuntimeError(
                    f"git cat-file returned an invalid header for {requested_oid}: "
                    f"{header.decode(errors='replace').strip()}"
                )
            actual_oid = parts[0].decode("ascii")
            remaining = int(parts[2])
            newline_count = 0
            last_byte = b""
            while remaining:
                chunk = proc.stdout.read(min(1 << 20, remaining))
                if not chunk:
                    raise RuntimeError("git cat-file ended while reading a blob")
                remaining -= len(chunk)
                newline_count += chunk.count(b"\n")
                last_byte = chunk[-1:]
            if proc.stdout.read(1) != b"\n":
                raise RuntimeError("git cat-file blob framing was invalid")
            if last_byte and last_byte != b"\n":
                newline_count += 1
            counts[actual_oid] = newline_count
    finally:
        if proc.stdout:
            proc.stdout.close()
    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    returncode = proc.wait(timeout=30)
    if returncode != 0:
        raise RuntimeError(f"git cat-file failed (rc={returncode}): {stderr.strip()}")
    return counts


_HUNK_RE = re.compile(
    r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@"
)


def _diff_header_paths(line: str) -> tuple[str, str] | None:
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    if len(parts) < 4:
        return None
    old_path, new_path = parts[-2], parts[-1]
    if old_path.startswith("a/"):
        old_path = old_path[2:]
    if new_path.startswith("b/"):
        new_path = new_path[2:]
    return old_path, new_path


@lru_cache(maxsize=24)
def _parse_diff_cached(
    workspace: str, base_oid: str, topic_oid: str,
) -> tuple[FileDiff, ...]:
    status_map = _name_status(workspace, base_oid, topic_oid)
    blob_oids = _tree_blob_oids(workspace, topic_oid, set(status_map))
    line_counts = _blob_line_counts(workspace, set(blob_oids.values()))
    raw = _run_git(
        workspace, "diff", f"-U{DIFF_CONTEXT_LINES}", "--no-color",
        "--no-ext-diff", f"{base_oid}...{topic_oid}",
    )

    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_ln = 1
    new_ln = 1
    logical_index = 0
    in_hunk = False

    def finish_current() -> None:
        nonlocal current, logical_index
        if current is None:
            return
        if not current.binary and current.status != "D" and current.blob_oid:
            new_total = line_counts.get(current.blob_oid, 0)
            trailing = max(0, new_total - new_ln + 1)
            if trailing:
                current.gaps.append(DiffGap(
                    start_index=logical_index,
                    count=trailing,
                    old_start=old_ln,
                    new_start=new_ln,
                ))
                logical_index += trailing
        current.total_lines = logical_index
        files.append(current)
        current = None

    for line in raw.splitlines():
        if line.startswith("diff --git"):
            finish_current()
            paths = _diff_header_paths(line)
            if paths is None:
                continue
            old_path, new_path = paths
            path = new_path if new_path in status_map else old_path
            current = FileDiff(
                path=path,
                status=status_map.get(path, "?"),
                workspace=workspace,
                topic_oid=topic_oid,
                blob_oid=blob_oids.get(path, ""),
            )
            old_ln = 1
            new_ln = 1
            logical_index = 0
            in_hunk = False
            continue
        if current is None:
            continue
        if line.startswith("Binary files") or line.startswith("GIT binary patch"):
            current.binary = True
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith("@@"):
            match = _HUNK_RE.match(line)
            if not match:
                continue
            hunk_old = int(match.group(1))
            hunk_new = int(match.group(3))
            old_gap = max(0, hunk_old - old_ln)
            new_gap = max(0, hunk_new - new_ln)
            gap_count = min(old_gap, new_gap)
            if gap_count:
                current.gaps.append(DiffGap(
                    start_index=logical_index,
                    count=gap_count,
                    old_start=old_ln,
                    new_start=new_ln,
                ))
                logical_index += gap_count
            old_ln = hunk_old
            new_ln = hunk_new
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+"):
            current.lines.append(DiffLine(
                "added", None, new_ln, line[1:], logical_index,
            ))
            current.additions += 1
            new_ln += 1
        elif line.startswith("-"):
            current.lines.append(DiffLine(
                "deleted", old_ln, None, line[1:], logical_index,
            ))
            current.deletions += 1
            old_ln += 1
        else:
            content = line[1:] if line.startswith(" ") else line
            current.lines.append(DiffLine(
                "context", old_ln, new_ln, content, logical_index,
            ))
            old_ln += 1
            new_ln += 1
        logical_index += 1

    finish_current()

    diffed = {f.path for f in files}
    for path, status in status_map.items():
        if path not in diffed:
            files.append(FileDiff(
                path=path,
                status=status,
                binary=True,
                workspace=workspace,
                topic_oid=topic_oid,
                blob_oid=blob_oids.get(path, ""),
            ))
    return tuple(files)


def parse_diff(workspace: str, base: str, topic: str) -> list[FileDiff]:
    """Return a cached bounded diff keyed by resolved immutable revisions."""
    resolved_workspace = str(Path(workspace).resolve())
    base_oid = _resolve_commit(resolved_workspace, base)
    topic_oid = _resolve_commit(resolved_workspace, topic)
    return list(_parse_diff_cached(resolved_workspace, base_oid, topic_oid))


def clear_diff_cache() -> None:
    _parse_diff_cached.cache_clear()
    _read_blob_range.cache_clear()


def diff_cache_info():
    return _parse_diff_cached.cache_info()


@lru_cache(maxsize=512)
def _read_blob_range(
    workspace: str, blob_oid: str, start_line: int, end_line: int,
) -> tuple[str, ...]:
    """Read an inclusive, one-based line range from one pinned blob."""
    if not blob_oid or start_line <= 0 or end_line < start_line:
        return ()
    cat = subprocess.Popen(
        ["git", "-C", workspace, "cat-file", "blob", blob_oid],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert cat.stdout is not None
    selected = subprocess.run(
        ["sed", "-n", f"{start_line},{end_line}p"],
        stdin=cat.stdout, capture_output=True, timeout=30,
    )
    cat.stdout.close()
    stderr = cat.stderr.read().decode(errors="replace") if cat.stderr else ""
    cat_returncode = cat.wait(timeout=30)
    if cat_returncode != 0:
        raise RuntimeError(
            f"git cat-file blob failed (rc={cat_returncode}): {stderr.strip()}"
        )
    if selected.returncode != 0:
        raise RuntimeError(
            f"sed failed while reading pinned blob (rc={selected.returncode})"
        )
    return tuple(selected.stdout.decode(errors="replace").splitlines())


def _gap_lines(fd: FileDiff, gap: DiffGap, start: int, end: int) -> list[DiffLine]:
    lo = max(start, gap.start_index)
    hi = min(end, gap.end_index)
    if hi <= lo:
        return []
    offset = lo - gap.start_index
    new_start = gap.new_start + offset
    old_start = gap.old_start + offset
    contents = _read_blob_range(
        fd.workspace, fd.blob_oid, new_start, new_start + (hi - lo) - 1,
    )
    return [
        DiffLine(
            "context",
            old_start + idx,
            new_start + idx,
            content,
            lo + idx,
        )
        for idx, content in enumerate(contents)
    ]


def slice_diff(fd: FileDiff, start: int, end: int) -> list[DiffLine]:
    """Return a bounded logical diff slice, filling virtual gaps from the blob."""
    lo = max(0, start)
    hi = min(max(lo, end), fd.total_lines)
    lines = [line for line in fd.lines if lo <= line.full_index < hi]
    for gap in fd.gaps:
        if gap.end_index <= lo:
            continue
        if gap.start_index >= hi:
            break
        lines.extend(_gap_lines(fd, gap, lo, hi))
    lines.sort(key=lambda line: line.full_index)
    return lines


def materialized_view(
    fd: FileDiff,
    comment_lines: set[int],
    *,
    context_lines: int = DIFF_CONTEXT_LINES,
) -> tuple[list[DiffLine], list[DiffGap]]:
    """Add bounded comment windows and split virtual gaps around those windows."""
    extra: list[DiffLine] = []
    for gap in fd.gaps:
        requested: list[tuple[int, int]] = []
        for line_no in sorted(comment_lines):
            if gap.new_start <= line_no < gap.new_start + gap.count:
                start_new = max(gap.new_start, line_no - context_lines)
                end_new = min(
                    gap.new_start + gap.count - 1,
                    line_no + context_lines,
                )
                requested.append((start_new, end_new))
        if not requested:
            continue
        merged: list[tuple[int, int]] = []
        for start_new, end_new in requested:
            if merged and start_new <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end_new))
            else:
                merged.append((start_new, end_new))
        for start_new, end_new in merged:
            start_index = gap.start_index + (start_new - gap.new_start)
            extra.extend(_gap_lines(
                fd, gap, start_index, start_index + end_new - start_new + 1,
            ))

    if not extra:
        return fd.lines, fd.gaps

    by_index = {line.full_index: line for line in fd.lines}
    by_index.update({line.full_index: line for line in extra})
    lines = [by_index[index] for index in sorted(by_index)]
    materialized = {line.full_index for line in extra}
    gaps: list[DiffGap] = []
    for gap in fd.gaps:
        cursor = gap.start_index
        for index in sorted(
            idx for idx in materialized
            if gap.start_index <= idx < gap.end_index
        ):
            if cursor < index:
                offset = cursor - gap.start_index
                gaps.append(DiffGap(
                    cursor, index - cursor,
                    gap.old_start + offset,
                    gap.new_start + offset,
                ))
            cursor = index + 1
        if cursor < gap.end_index:
            offset = cursor - gap.start_index
            gaps.append(DiffGap(
                cursor, gap.end_index - cursor,
                gap.old_start + offset,
                gap.new_start + offset,
            ))
    return lines, gaps
