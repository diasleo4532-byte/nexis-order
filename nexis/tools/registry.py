from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nexis.safety import Risk

Handler = Callable[..., Any]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    risk: Risk | Callable[[dict[str, Any]], Risk] = Risk.SAFE
    summarizer: Callable[[dict[str, Any]], str] | None = None

    def risk_for(self, args: dict[str, Any]) -> Risk:
        return self.risk(args) if callable(self.risk) else self.risk

    def summarize(self, args: dict[str, Any]) -> str:
        if self.summarizer:
            return self.summarizer(args)
        rendered = ", ".join(f"{key}={_short(value)}" for key, value in args.items())
        return f"{self.name}({rendered})" if rendered else self.name

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def add(self, name: str, description: str, parameters: dict[str, Any], handler: Handler, **extra: Any) -> None:
        self.register(
            Tool(
                name=name,
                description=description,
                parameters=parameters,
                handler=handler,
                risk=extra.get("risk", Risk.SAFE),
                summarizer=extra.get("summarizer"),
            )
        )

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[name].schema() for name in self.names()]

    def execute(self, name: str, args: dict[str, Any]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        allowed = set(inspect.signature(tool.handler).parameters)
        filtered = {key: value for key, value in (args or {}).items() if key in allowed}
        return tool.handler(**filtered)
