from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = ""
    id: str = ""


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_name: str | None = None

    def to_wire(self) -> dict[str, Any]:
        if self.role == "assistant":
            wire: dict[str, Any] = {"role": "assistant", "content": self.content}
            if self.tool_calls:
                wire["tool_calls"] = [
                    {"function": {"name": call.name, "arguments": call.arguments}}
                    for call in self.tool_calls
                ]
            return wire
        if self.role == "tool":
            return {
                "role": "tool",
                "content": self.content,
                "tool_name": self.tool_name or "",
            }
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completed:
    message: ChatMessage


StreamEvent = TextDelta | Completed


@dataclass(frozen=True)
class ProviderHealth:
    reachable: bool
    models: tuple[str, ...] = ()
    detail: str = ""


class ProviderError(RuntimeError):
    pass


class LLMProvider(Protocol):
    model: str

    def stream(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> Iterator[StreamEvent]: ...

    def health(self) -> ProviderHealth: ...
