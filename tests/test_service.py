from __future__ import annotations

import asyncio
import dataclasses
import threading
import time

import pytest

import nexis.web.service as service_module
from nexis.agent.loop import TurnResult
from nexis.safety import ConfirmationRequest
from nexis.web.service import NexisService


@pytest.fixture()
def service(tmp_path, settings_factory, monkeypatch):
    monkeypatch.setattr(service_module, "APP_DIR", tmp_path / "nexis-home")
    (tmp_path / "nexis-home").mkdir()
    settings = settings_factory()
    settings = dataclasses.replace(settings, ui=dataclasses.replace(settings.ui, stream=False))
    runtime = NexisService(settings)
    yield runtime
    runtime.close()


def test_emit_reaches_subscribed_queues(service):
    async def scenario():
        service.attach_loop(asyncio.get_running_loop())
        queue = service.subscribe()
        service.emit({"type": "delta", "text": "hi"})
        event = await asyncio.wait_for(queue.get(), timeout=2)
        assert event == {"type": "delta", "text": "hi"}
        service.unsubscribe(queue)

    asyncio.run(scenario())


def test_start_turn_is_single_flight(service, monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    def fake_run(text, *, session_id, emit=None, stream=None):
        release.wait(2)
        finished.set()
        return TurnResult(text="ok", steps=1)

    monkeypatch.setattr(service.agent, "run", fake_run)
    assert service.start_turn("hello") is True
    assert service.start_turn("again") is False
    release.set()
    assert finished.wait(2)
    deadline = threading.Event()
    for _ in range(100):
        if not service.turn_active:
            break
        deadline.wait(0.02)
    assert service.turn_active is False


def test_confirmation_roundtrip(service):
    async def scenario():
        service.attach_loop(asyncio.get_running_loop())
        queue = service.subscribe()
        answers: dict[str, bool] = {}

        def worker():
            request = ConfirmationRequest(tool="files_delete", summary="Delete notes.txt")
            answers["approved"] = service._confirm(request)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        event = await asyncio.wait_for(queue.get(), timeout=2)
        assert event["type"] == "confirm"
        assert event["tool"] == "files_delete"
        assert service.resolve_confirm(event["id"], True) is True
        thread.join(2)
        assert answers["approved"] is True
        service.unsubscribe(queue)

    asyncio.run(scenario())


def test_unknown_confirmation_is_ignored(service):
    assert service.resolve_confirm("missing", True) is False


def test_stop_turn_denies_pending_confirmation(service):
    async def scenario():
        service.attach_loop(asyncio.get_running_loop())
        service._turn_active = True
        queue = service.subscribe()
        answers: dict[str, bool] = {}

        def worker():
            request = ConfirmationRequest(tool="files_delete", summary="Delete notes.txt")
            answers["approved"] = service._confirm(request)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        await asyncio.wait_for(queue.get(), timeout=2)
        service.stop_turn()
        thread.join(2)
        assert answers["approved"] is False
        service.unsubscribe(queue)

    asyncio.run(scenario())


def test_cancelled_turn_skips_confirmation(service):
    service._turn_cancel.set()
    started = time.monotonic()
    request = ConfirmationRequest(tool="files_delete", summary="Delete notes.txt")
    assert service._confirm(request) is False
    assert time.monotonic() - started < 1.5


def test_set_model_updates_provider_and_settings(service):
    service.set_model("qwen3:4b")
    assert service.provider.model == "qwen3:4b"
    assert service.settings.llm.model == "qwen3:4b"


def test_history_and_new_session(service):
    service.memory.add_message(service.session_id, "user", "remember me")
    assert any(item["content"] == "remember me" for item in service.history())
    first = service.session_id
    second = service.new_session()
    assert second != first
    assert service.history() == []
