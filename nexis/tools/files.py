from __future__ import annotations

from typing import Any

from nexis.safety import Risk
from nexis.tools.registry import ToolRegistry
from nexis.workspace import Workspace


def register(registry: ToolRegistry, workspace: Workspace) -> None:
    def list_files(path: str = ".") -> dict[str, Any]:
        items = workspace.list_dir(path)
        return {
            "workspace": str(workspace.root),
            "path": path,
            "entries": [
                {"path": item.path, "kind": item.kind, "size": item.size, "modified": item.modified}
                for item in items
            ],
        }

    def read_file(path: str, max_chars: int = 20000) -> dict[str, Any]:
        text = workspace.read_text(path)
        truncated = len(text) > max_chars
        return {
            "path": workspace.relative(workspace.resolve(path)),
            "content": text[:max_chars],
            "truncated": truncated,
        }

    def write_file(path: str, content: str) -> dict[str, Any]:
        target = workspace.write_text(path, content)
        return {
            "path": workspace.relative(target),
            "bytes_written": len(content.encode("utf-8")),
        }

    def delete_file(path: str, recursive: bool = False) -> dict[str, Any]:
        deleted = workspace.delete(path, recursive=recursive)
        return {"deleted": deleted}

    def search_files(query: str, limit: int = 20) -> dict[str, Any]:
        hits = workspace.search(query, limit=limit)
        return {
            "query": query,
            "hits": [{"path": hit.path, "line": hit.line, "text": hit.text} for hit in hits],
        }

    registry.add(
        "list_files",
        "List files and folders inside the Nexis Workspace. Paths are relative to the workspace.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative folder path, default '.'"}
            },
            "additionalProperties": False,
        },
        list_files,
    )
    registry.add(
        "read_file",
        "Read a UTF-8 text file from the Nexis Workspace. Returns the content, truncated if large.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside the workspace"},
                "max_chars": {"type": "integer", "description": "Maximum characters to return"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        read_file,
    )
    registry.add(
        "write_file",
        "Create or overwrite a UTF-8 text file inside the Nexis Workspace. Creates parent folders.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside the workspace"},
                "content": {"type": "string", "description": "Full file content"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        write_file,
    )
    registry.add(
        "delete_file",
        "Delete a file or folder inside the Nexis Workspace. Requires explicit user confirmation.",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the workspace"},
                "recursive": {
                    "type": "boolean",
                    "description": "Set true to delete a non-empty folder",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        delete_file,
        risk=Risk.CONFIRM,
        summarizer=lambda args: "Delete "
        + (f"folder '{args.get('path')}' and everything inside it"
           if args.get("recursive") else f"'{args.get('path')}'")
        + " from the Nexis Workspace",
    )
    registry.add(
        "search_files",
        "Search text files in the Nexis Workspace for a substring and return matching lines.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for (case-insensitive)"},
                "limit": {"type": "integer", "description": "Maximum hits, default 20"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        search_files,
    )
