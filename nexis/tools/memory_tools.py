from __future__ import annotations

from typing import Any

from nexis.memory import MemoryStore
from nexis.tools.registry import ToolRegistry


def register(registry: ToolRegistry, memory: MemoryStore) -> None:
    def remember(key: str, value: str) -> dict[str, Any]:
        memory.save_fact(key, value)
        return {"remembered": key.strip().lower(), "value": value.strip()}

    def recall(query: str | None = None) -> dict[str, Any]:
        facts = memory.facts()
        payload: dict[str, Any] = {"facts": facts}
        if query:
            payload["history"] = memory.search_history(query)
        return payload

    def forget(key: str) -> dict[str, Any]:
        removed = memory.forget_fact(key)
        return {"forgotten": key.strip().lower(), "found": removed}

    registry.add(
        "remember",
        "Persist a durable preference or fact about the user across sessions, e.g. "
        "remember('music taste', 'likes lo-fi jazz and Radiohead'). Use when the user says "
        "'remember that...' or shares a stable preference.",
        {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short label, e.g. 'music taste'"},
                "value": {"type": "string", "description": "What to remember"},
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
        remember,
    )
    registry.add(
        "recall",
        "Look up saved preferences and (optionally) search past conversation history.",
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional phrase to search in past conversations",
                }
            },
            "additionalProperties": False,
        },
        recall,
    )
    registry.add(
        "forget",
        "Remove a previously saved preference by key.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        forget,
    )
