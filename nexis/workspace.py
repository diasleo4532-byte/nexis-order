from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MAX_READ_BYTES = 2_000_000
MAX_SEARCH_FILES = 2000
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".py", ".json", ".jsonl", ".csv", ".tsv", ".log",
    ".html", ".htm", ".css", ".js", ".ts", ".toml", ".yaml", ".yml", ".xml",
    ".ini", ".cfg", ".conf", ".sh", ".ps1", ".bat", ".rst",
}


class WorkspaceViolation(PermissionError):
    """Raised when a path escapes the Nexis Workspace sandbox."""


@dataclass(frozen=True)
class FileInfo:
    path: str
    kind: str  # "file" | "directory"
    size: int
    modified: str


@dataclass(frozen=True)
class SearchHit:
    path: str
    line: int
    text: str


def _format_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


class Workspace:
    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def resolve(self, path: str | os.PathLike[str]) -> Path:
        raw = Path(os.path.expandvars(str(path))).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceViolation(
                f"'{path}' is outside the Nexis Workspace ({self.root}); "
                "move the file into the workspace first."
            ) from exc
        return resolved

    def relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def _suffix_ok(self, path: Path) -> bool:
        return path.suffix.lower() in TEXT_SUFFIXES

    def list_dir(self, sub: str = ".") -> list[FileInfo]:
        target = self.resolve(sub)
        if target.is_file():
            stat = target.stat()
            return [FileInfo(self.relative(target), "file", stat.st_size, _format_ts(stat.st_mtime))]
        if not target.exists():
            raise FileNotFoundError(f"No such path in workspace: {sub}")
        items: list[FileInfo] = []
        for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            try:
                stat = entry.stat()
            except OSError:
                continue
            items.append(
                FileInfo(
                    path=self.relative(entry),
                    kind="file" if entry.is_file() else "directory",
                    size=stat.st_size if entry.is_file() else 0,
                    modified=_format_ts(stat.st_mtime),
                )
            )
        return items

    def read_text(self, sub: str) -> str:
        target = self.resolve(sub)
        if not target.exists():
            raise FileNotFoundError(f"No such file in workspace: {sub}")
        if not target.is_file():
            raise IsADirectoryError(f"{sub} is a directory")
        size = target.stat().st_size
        with target.open("rb") as handle:
            data = handle.read(MAX_READ_BYTES)
        if b"\x00" in data[:1024]:
            raise ValueError(f"{sub} looks like a binary file and cannot be read as text")
        text = data.decode("utf-8", errors="replace")
        if size > len(data):
            text += f"\n\n[truncated: showed {len(data)} of {size} bytes]"
        return text

    def write_text(self, sub: str, content: str) -> Path:
        target = self.resolve(sub)
        if target == self.root:
            raise IsADirectoryError("Cannot overwrite the workspace root")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def delete(self, sub: str, recursive: bool = False) -> str:
        target = self.resolve(sub)
        if target == self.root:
            raise ValueError("Refusing to delete the workspace root")
        if not target.exists():
            raise FileNotFoundError(f"No such path in workspace: {sub}")
        if target.is_dir():
            if not recursive:
                raise IsADirectoryError(f"{sub} is a directory; pass recursive=true to delete it")
            for child in sorted(target.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
            target.rmdir()
        else:
            target.unlink()
        return self.relative(target)

    def search(self, query: str, limit: int = 40) -> list[SearchHit]:
        needle = query.lower()
        if not needle:
            raise ValueError("Search query must not be empty")
        hits: list[SearchHit] = []
        checked = 0
        for path in self.root.rglob("*"):
            if len(hits) >= limit or checked >= MAX_SEARCH_FILES:
                break
            if not path.is_file() or not self._suffix_ok(path):
                continue
            checked += 1
            try:
                if path.stat().st_size > MAX_READ_BYTES:
                    continue
                with path.open("r", encoding="utf-8", errors="ignore") as handle:
                    for number, line in enumerate(handle, start=1):
                        if needle in line.lower():
                            hits.append(
                                SearchHit(self.relative(path), number, line.strip()[:240])
                            )
                            if len(hits) >= limit:
                                break
            except OSError:
                continue
        return hits
