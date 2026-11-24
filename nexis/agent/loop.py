from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nexis.agent.prompts import build_system_prompt
from nexis.audit import AuditLog
from nexis.config import Settings
from nexis.llm.base import (
    ChatMessage,
    Completed,
    LLMProvider,
    ProviderError,
    TextDelta,
    ToolCall,
)
from nexis.memory import MemoryStore
from nexis.safety import (
    ActionBlocked,
    ConfirmationRequest,
    SafetyPolicy,
)
from nexis.tools.registry import ToolRegistry
from nexis.workspace import Workspace

MAX_TOOL_RESULT_CHARS = 12000


class AgentError(RuntimeError):
    pass


class TurnInterrupted(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentEvent:
    kind: str  # status | delta | tool_call | tool_result | notice
    text: str = ""
    tool: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnResult:
    text: str
    steps: int = 0
    tools_used: tuple[str, ...] = ()
    interrupted: bool = False


def _noop(_: AgentEvent) -> None:
    pass


class Agent:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: ToolRegistry,
        memory: MemoryStore,
        settings: Settings,
        audit: AuditLog,
        workspace: Workspace,
        safety: SafetyPolicy,
    ):
        self.provider = provider
        self.registry = registry
        self.memory = memory
        self.settings = settings
        self.audit = audit
        self.workspace = workspace
        self.safety = safety
        self._interrupt = False

    def interrupt(self) -> None:
        self._interrupt = True
        stop = getattr(self.provider, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass

    def _check_interrupt(self) -> None:
        if self._interrupt:
            raise TurnInterrupted("Interrupted by user")

    def run(
        self,
        user_text: str,
        *,
        session_id: str,
        emit: Callable[[AgentEvent], None] | None = None,
        stream: bool | None = None,
    ) -> TurnResult:
        emit = emit or _noop
        stream = self.settings.ui.stream if stream is None else stream
        self._interrupt = False

        user_text = user_text.strip()
        if not user_text:
            return TurnResult(text="")

        self.memory.add_message(session_id, "user", user_text)
        history = self.memory.history(session_id, turns=self.settings.agent.history_turns)
        facts = self.memory.facts(limit=self.settings.memory.max_facts_in_prompt)
        system_prompt = build_system_prompt(self.settings, facts, str(self.workspace.root))

        messages: list[ChatMessage] = [ChatMessage("system", system_prompt)]
        messages.extend(
            ChatMessage(role=item["role"], content=item["content"]) for item in history
        )

        tools_used: list[str] = []
        steps = 0
        final_text = ""
        partial_text = ""

        try:
            for step in range(1, self.settings.agent.max_steps + 1):
                self._check_interrupt()
                steps = step
                emit(AgentEvent("status", "Thinking…" if step == 1 else "Working…"))
                completion, streamed = self._collect(emit, messages, stream)
                partial_text = streamed
                if completion is None:
                    raise AgentError("The model returned an empty response.")

                messages.append(completion)
                if not completion.tool_calls:
                    final_text = completion.content.strip()
                    break

                for call in completion.tool_calls:
                    self._check_interrupt()
                    result_text = self._execute_tool(call, emit, tools_used)
                    messages.append(
                        ChatMessage(role="tool", content=result_text, tool_name=call.name)
                    )
            else:
                final_text = (
                    f"I reached my limit of {self.settings.agent.max_steps} steps without "
                    "finishing cleanly. Here is where things stand — tell me how you would "
                    "like me to continue."
                )
                emit(AgentEvent("notice", final_text))
        except (TurnInterrupted, KeyboardInterrupt):
            self.audit.record("turn_interrupted", session=session_id)
            final_text = partial_text.strip() or "Stopped."
            emit(AgentEvent("notice", "Stopped."))
            self.memory.add_message(session_id, "assistant", final_text)
            return TurnResult(
                text=final_text, steps=steps, tools_used=tuple(tools_used), interrupted=True
            )

        final_text = final_text or "Done."
        if not stream:
            emit(AgentEvent("delta", final_text))
        self.memory.add_message(session_id, "assistant", final_text)
        self.audit.record(
            "turn_complete",
            session=session_id,
            steps=steps,
            tools=list(tools_used),
        )
        return TurnResult(text=final_text, steps=steps, tools_used=tuple(tools_used))

    def _collect(
        self,
        emit: Callable[[AgentEvent], None],
        messages: list[ChatMessage],
        stream: bool,
    ) -> tuple[ChatMessage | None, str]:
        completion: ChatMessage | None = None
        parts: list[str] = []
        try:
            for event in self.provider.stream(messages, self.registry.schemas()):
                self._check_interrupt()
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                    if stream:
                        emit(AgentEvent("delta", event.text))
                elif isinstance(event, Completed):
                    completion = event.message
        except ProviderError as exc:
            if self._interrupt:
                raise TurnInterrupted("Interrupted by user") from exc
            self.audit.record("provider_error", error=str(exc))
            raise AgentError(str(exc)) from exc
        return completion, "".join(parts)

    def _execute_tool(
        self, call: ToolCall, emit: Callable[[AgentEvent], None], tools_used: list[str]
    ) -> str:
        tool = self.registry.get(call.name)
        if tool is None:
            error = f"Unknown tool '{call.name}'. Available tools: {', '.join(self.registry.names())}"
            self.audit.record("tool_unknown", tool=call.name)
            emit(AgentEvent("tool_result", tool=call.name, detail={"ok": False, "error": error}))
            return json.dumps({"ok": False, "error": error})

        args = call.arguments if isinstance(call.arguments, dict) else {}
        risk = tool.risk_for(args)
        summary = tool.summarize(args)
        emit(
            AgentEvent(
                "tool_call",
                tool=call.name,
                detail={"summary": summary, "risk": risk.value},
            )
        )

        request = ConfirmationRequest(tool=call.name, summary=summary, details={"arguments": args}, risk=risk)
        try:
            approved = self.safety.check(request)
        except ActionBlocked as exc:
            self.audit.record("tool_blocked", tool=call.name, summary=summary)
            payload = {"ok": False, "error": str(exc)}
            emit(AgentEvent("tool_result", tool=call.name, detail=payload))
            return json.dumps(payload)

        if not approved:
            self.audit.record("tool_denied", tool=call.name, summary=summary, args=_audit_args(args))
            payload = {
                "ok": False,
                "error": "The user declined this action. Do not retry it or work around it; acknowledge and stop.",
            }
            emit(AgentEvent("tool_result", tool=call.name, detail={"ok": False, "declined": True}))
            return json.dumps(payload)

        started = time.perf_counter()
        try:
            tools_used.append(call.name)
            result = self.registry.execute(call.name, args)
            duration_ms = int((time.perf_counter() - started) * 1000)
            payload = {"ok": True, "result": result}
            self.audit.record(
                "tool_ok",
                tool=call.name,
                summary=summary,
                risk=risk.value,
                duration_ms=duration_ms,
                args=_audit_args(args),
            )
            rendered = _render(payload)
            emit(
                AgentEvent(
                    "tool_result",
                    tool=call.name,
                    detail={"ok": True, "duration_ms": duration_ms, "result": result},
                )
            )
            return rendered
        except Exception as exc:  # tool failures go back to the model as data
            duration_ms = int((time.perf_counter() - started) * 1000)
            error = f"{type(exc).__name__}: {exc}"
            payload = {"ok": False, "error": error}
            self.audit.record(
                "tool_error",
                tool=call.name,
                summary=summary,
                risk=risk.value,
                duration_ms=duration_ms,
                error=error,
                args=_audit_args(args),
            )
            emit(AgentEvent("tool_result", tool=call.name, detail={"ok": False, "error": error}))
            return _render(payload)


def _render(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + " …[truncated]"
    return text


SENSITIVE_ARG_HINTS = ("password", "passwd", "token", "secret", "api_key", "apikey", "auth")


def _audit_args(args: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in (args or {}).items():
        if any(hint in key.lower() for hint in SENSITIVE_ARG_HINTS):
            safe[key] = "***"
        elif isinstance(value, str) and len(value) > limit:
            safe[key] = value[:limit] + f"…[{len(value)} chars]"
        else:
            safe[key] = value
    return safe
