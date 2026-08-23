from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def slugify(value: str, *, fallback: str = "pr", limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or fallback)[:limit].rstrip("-")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def process_start_ticks(pid: int) -> int | None:
    try:
        # The comm field may contain spaces and parentheses; fields after the
        # final ')' start at proc field 3. Start time is field 22.
        tail = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return int(tail[19])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def process_has_token(pid: int, token: str) -> bool:
    try:
        entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except (FileNotFoundError, PermissionError):
        return False
    expected = f"REVIEW_QUEUE_OWNER_TOKEN={token}".encode()
    return expected in entries


def free_bytes(path: Path) -> int:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return os.statvfs(existing).f_bavail * os.statvfs(existing).f_frsize
