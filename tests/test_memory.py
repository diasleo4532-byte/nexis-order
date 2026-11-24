from __future__ import annotations

import pytest

from nexis.memory import MemoryStore


@pytest.fixture()
def memory(tmp_path):
    store = MemoryStore(tmp_path / "nexis.db")
    yield store
    store.close()


def test_history_is_chronological(memory):
    memory.add_message("s1", "user", "first")
    memory.add_message("s1", "assistant", "second")
    memory.add_message("s1", "user", "third")
    history = memory.history("s1", turns=5)
    assert [item["content"] for item in history] == ["first", "second", "third"]


def test_history_is_limited_to_recent_turns(memory):
    for index in range(10):
        memory.add_message("s1", "user", f"msg {index}")
    history = memory.history("s1", turns=2)
    assert len(history) == 4
    assert history[-1]["content"] == "msg 9"


def test_sessions_are_isolated(memory):
    memory.add_message("s1", "user", "alpha")
    memory.add_message("s2", "user", "beta")
    assert memory.history("s2") == [{"role": "user", "content": "beta"}]


def test_facts_upsert_and_forget(memory):
    memory.save_fact("Music", "lo-fi")
    memory.save_fact("music", "jazz")
    assert memory.facts() == {"music": "jazz"}
    assert memory.forget_fact("MUSIC") is True
    assert memory.facts() == {}
    assert memory.forget_fact("music") is False


def test_fact_key_cannot_be_empty(memory):
    with pytest.raises(ValueError):
        memory.save_fact("  ", "value")


def test_search_history_finds_past_messages(memory):
    memory.add_message("s1", "user", "I prefer coffee in the morning")
    memory.add_message("s1", "assistant", "Noted, coffee it is")
    results = memory.search_history("coffee")
    assert any("coffee" in row["content"].lower() for row in results)


def test_search_history_handles_punctuation(memory):
    memory.add_message("s1", "user", "What's the weather like?")
    assert memory.search_history("weather?") != []


def test_sessions_listing(memory):
    memory.add_message("s1", "user", "hi")
    memory.touch_session("s2")
    ids = {row["session_id"] for row in memory.sessions()}
    assert {"s1", "s2"} <= ids
