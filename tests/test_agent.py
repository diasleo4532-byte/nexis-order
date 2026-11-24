from __future__ import annotations

import json

from nexis.llm.base import ChatMessage, ProviderError, ToolCall
from nexis.safety import Risk


def echo_tool(calls):
    def register(registry):
        registry.add(
            "echo",
            "Echo text back",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            lambda text: calls.append(text) or {"echoed": text},
        )

    return register


def test_tool_call_then_final_answer(build_env):
    calls: list[str] = []
    env = build_env(
        [
            ChatMessage("assistant", "", [ToolCall("echo", {"text": "hi"})]),
            ChatMessage("assistant", "All done."),
        ],
        register=echo_tool(calls),
    )
    result = env.agent.run("say hi", session_id="s1", emit=env.emit)

    assert result.text == "All done."
    assert result.tools_used == ("echo",)
    assert calls == ["hi"]
    kinds = [event.kind for event in env.events]
    assert "tool_call" in kinds and "tool_result" in kinds

    history = env.memory.history("s1")
    assert history[0] == {"role": "user", "content": "say hi"}
    assert history[-1]["role"] == "assistant"

    events = {entry["event"] for entry in env.audit.tail(50)}
    assert {"tool_ok", "turn_complete"} <= events


def test_confirmation_denied_blocks_tool(build_env):
    calls: list[str] = []

    def register(registry):
        registry.add(
            "danger",
            "dangerous",
            {"type": "object", "properties": {}},
            lambda: calls.append("ran"),
            risk=Risk.CONFIRM,
            summarizer=lambda args: "Do something risky",
        )

    env = build_env(
        [
            ChatMessage("assistant", "", [ToolCall("danger", {})]),
            ChatMessage("assistant", "Understood, I won't."),
        ],
        register=register,
        confirmer=lambda request: False,
    )
    result = env.agent.run("do it", session_id="s1", emit=env.emit)

    assert calls == []
    assert "won't" in result.text
    tool_messages = [
        message for message in env.provider.calls[1] if message.role == "tool"
    ]
    assert "declined" in tool_messages[-1].content.lower()
    audit_events = {entry["event"] for entry in env.audit.tail(50)}
    assert "tool_denied" in audit_events


def test_confirmation_granted_executes(build_env):
    calls: list[str] = []
    approvals: list[str] = []

    def register(registry):
        registry.add(
            "danger",
            "dangerous",
            {"type": "object", "properties": {}},
            lambda: calls.append("ran") or {"ok": True},
            risk=Risk.CONFIRM,
            summarizer=lambda args: "Risky action",
        )

    def confirmer(request):
        approvals.append(request.summary)
        return True

    env = build_env(
        [
            ChatMessage("assistant", "", [ToolCall("danger", {})]),
            ChatMessage("assistant", "Done."),
        ],
        register=register,
        confirmer=confirmer,
    )
    env.agent.run("do it", session_id="s1", emit=env.emit)

    assert calls == ["ran"]
    assert approvals == ["Risky action"]


def test_tool_error_goes_back_to_model(build_env):
    def register(registry):
        def explode():
            raise RuntimeError("boom")

        registry.add(
            "explode",
            "explodes",
            {"type": "object", "properties": {}},
            explode,
        )

    env = build_env(
        [
            ChatMessage("assistant", "", [ToolCall("explode", {})]),
            ChatMessage("assistant", "That failed, sorry."),
        ],
        register=register,
    )
    result = env.agent.run("try", session_id="s1", emit=env.emit)

    assert result.text == "That failed, sorry."
    tool_message = [m for m in env.provider.calls[1] if m.role == "tool"][-1]
    payload = json.loads(tool_message.content)
    assert payload["ok"] is False
    assert "boom" in payload["error"]
    audit_events = {entry["event"] for entry in env.audit.tail(50)}
    assert "tool_error" in audit_events


def test_unknown_tool_is_reported(build_env):
    env = build_env(
        [
            ChatMessage("assistant", "", [ToolCall("nope", {})]),
            ChatMessage("assistant", "I don't have that tool."),
        ]
    )
    env.agent.run("try", session_id="s1", emit=env.emit)
    tool_message = [m for m in env.provider.calls[1] if m.role == "tool"][-1]
    assert "Unknown tool" in tool_message.content


def test_max_steps_falls_back_gracefully(build_env):
    calls: list[str] = []
    env = build_env(
        [ChatMessage("assistant", "", [ToolCall("echo", {"text": str(i)})]) for i in range(5)],
        register=echo_tool(calls),
        max_steps=2,
    )
    result = env.agent.run("loop", session_id="s1", emit=env.emit)
    assert "limit" in result.text.lower()
    assert len(calls) == 2


def test_saved_facts_are_injected_into_system_prompt(build_env):
    env = build_env([ChatMessage("assistant", "Hello.")])
    env.memory.save_fact("music taste", "lo-fi jazz")
    env.agent.run("hello", session_id="s1", emit=env.emit)

    system = env.provider.calls[0][0]
    assert system.role == "system"
    assert "music taste" in system.content
    assert "lo-fi jazz" in system.content


def test_history_is_replayed_to_model(build_env):
    calls: list[str] = []
    env = build_env(
        [
            ChatMessage("assistant", "First answer."),
            ChatMessage("assistant", "Second answer."),
        ],
        register=echo_tool(calls),
    )
    env.agent.run("one", session_id="s1", emit=env.emit)
    env.agent.run("two", session_id="s1", emit=env.emit)

    second_call = env.provider.calls[1]
    contents = [message.content for message in second_call]
    assert "one" in contents and "First answer." in contents and "two" in contents


def test_provider_error_is_wrapped(build_env):
    env = build_env([ProviderError("Ollama is down")])
    try:
        env.agent.run("hello", session_id="s1", emit=env.emit)
    except Exception as exc:
        assert "Ollama is down" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected an AgentError")
    audit_events = {entry["event"] for entry in env.audit.tail(50)}
    assert "provider_error" in audit_events
