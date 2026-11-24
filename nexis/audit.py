from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    """Append-only JSONL log of every action Nexis takes."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, event: str, **fields: Any) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "event": event,
            **fields,
        }
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        entries: list[dict[str, Any]] = []
        for line in lines[-max(1, count) :]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
