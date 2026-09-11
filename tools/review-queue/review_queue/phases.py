from __future__ import annotations

from dataclasses import dataclass

from .util import isoformat


@dataclass(frozen=True, slots=True)
class PhaseChange:
    phase: str = ""
    started_at: str | None = None
    entered: bool = False
    log_offset: int = 0


class PhaseStack:
    """Interpret whole-line group markers from one launcher operation."""

    def __init__(self) -> None:
        self.stack: list[tuple[str, str, int]] = []

    def consume(self, line: str, *, log_offset: int = 0) -> PhaseChange | None:
        line = line.rstrip("\r\n")
        entered = line.startswith("::group::")
        if entered:
            title = line[len("::group::") :]
            title = title.replace("%0D", "\r").replace("%0A", "\n").replace("%25", "%")
            title = " ".join(title.split())[:256]
            if not title:
                return None
            self.stack.append((title, isoformat(), log_offset))
        elif line == "::endgroup::" and self.stack:
            self.stack.pop()
        else:
            return None
        return PhaseChange(
            phase=" › ".join(title for title, _, _ in self.stack),
            started_at=self.stack[-1][1] if self.stack else None,
            entered=entered,
            log_offset=self.stack[-1][2] if self.stack else 0,
        )
