from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from nexis.agent import Agent
from nexis.audit import AuditLog
from nexis.config import (
    AgentSettings,
    LLMSettings,
    MemorySettings,
    SafetySettings,
    Settings,
    UISettings,
    VoiceSettings,
    WorkspaceSettings,
)
from nexis.llm.base import ChatMessage, Completed, ProviderHealth, TextDelta
from nexis.memory import MemoryStore
from nexis.safety import SafetyPolicy
from nexis.tools.registry import ToolRegistry
from nexis.workspace import Workspace


class FakeProvider:
    model = "fake"

    def __init__(self, replies: list[ChatMessage | Exception]):
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def stream(self, messages, tools):
        self.calls.append(list(messages))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if reply.content:
            yield TextDelta(reply.content)
        yield Completed(reply)

    def health(self):
        return ProviderHealth(reachable=True, models=("fake",))


@pytest.fixture()
def settings_factory(tmp_path):
    def make(**agent_overrides):
        settings = Settings(
            llm=LLMSettings(),
            agent=AgentSettings(max_steps=4, history_turns=10, proactive=True),
            workspace=WorkspaceSettings(path=tmp_path / "workspace"),
            memory=MemorySettings(db_path=tmp_path / "nexis.db", max_facts_in_prompt=20),
            safety=SafetySettings(require_confirmation=True),
            voice=VoiceSettings(),
            ui=UISettings(stream=False),
            source=tmp_path / "config.toml",
        )
        if agent_overrides:
            settings = dataclasses.replace(
                settings, agent=dataclasses.replace(settings.agent, **agent_overrides)
            )
        return settings

    return make


@pytest.fixture()
def build_env(tmp_path, settings_factory):
    envs: list[SimpleNamespace] = []

    def make(
        replies,
        *,
        register=None,
        confirmer=None,
        max_steps=4,
        require_confirmation=True,
    ):
        settings = settings_factory(max_steps=max_steps)
        workspace = Workspace(settings.workspace.path)
        memory = MemoryStore(settings.memory.db_path)
        audit = AuditLog(tmp_path / "audit.jsonl")
        registry = ToolRegistry()
        if register is not None:
            register(registry)
        provider = FakeProvider(replies)
        safety = SafetyPolicy(
            require_confirmation=require_confirmation,
            confirmer=confirmer or (lambda request: True),
        )
        agent = Agent(
            provider=provider,
            registry=registry,
            memory=memory,
            settings=settings,
            audit=audit,
            workspace=workspace,
            safety=safety,
        )
        events: list = []
        env = SimpleNamespace(
            agent=agent,
            provider=provider,
            memory=memory,
            audit=audit,
            workspace=workspace,
            registry=registry,
            settings=settings,
            events=events,
            emit=events.append,
        )
        envs.append(env)
        return env

    yield make
    for env in envs:
        env.memory.close()
