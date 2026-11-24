from __future__ import annotations

import asyncio

import pytest
from starlette.testclient import TestClient

from nexis.web.server import create_app


class FakeService:
    def __init__(self, tmp_path):
        self.session_id = "fake-session"
        self.attached = None
        self.sent: list[str] = []
        self.stopped = False
        self.model = "qwen3.5:4b"
        self.handsfree = False
        self.paused = False
        self.voice_started = False
        self.confirms: dict[str, bool] = {}
        self._tmp = tmp_path
        self._clients = set()

    def attach_loop(self, loop):
        self.attached = loop

    def subscribe(self):
        queue = asyncio.Queue()
        self._clients.add(queue)
        return queue

    def unsubscribe(self, queue):
        self._clients.discard(queue)

    def emit(self, event):
        for queue in list(self._clients):
            self.attached.call_soon_threadsafe(queue.put_nowait, event)

    def state(self):
        return {
            "model": self.model,
            "models": [self.model],
            "ollama": True,
            "ollama_detail": "",
            "handsfree": self.handsfree,
            "wake_word": "nexis",
            "workspace": "C:/workspace",
            "session": self.session_id,
            "turn_active": False,
            "version": "0.1.0",
        }

    def history(self, turns=40):
        return [{"role": "user", "content": "hello"}]

    def start_turn(self, text):
        if not text.strip() or len(text) > 8000:
            return False
        self.sent.append(text)
        return True

    def stop_turn(self):
        self.stopped = True

    def resolve_confirm(self, token, approved):
        if token not in self.confirms:
            return False
        self.confirms[token] = approved
        return True

    def set_model(self, name):
        self.model = name

    def voice_start(self):
        self.voice_started = True

    def voice_stop(self):
        return "hello world"

    def set_handsfree(self, on):
        self.handsfree = on
        return on

    def set_listening_paused(self, paused):
        self.paused = paused

    def synthesize(self, text):
        path = self._tmp / "speech.wav"
        path.write_bytes(b"RIFFfake")
        return path, "audio/wav"

    def new_session(self):
        self.session_id = "fresh"
        return self.session_id

    def open_workspace(self):
        self.opened = True

    def close(self):
        pass


@pytest.fixture()
def client(tmp_path):
    service = FakeService(tmp_path)
    app = create_app(service)
    with TestClient(app) as test_client:
        test_client.service = service
        yield test_client


def test_index_and_favicon_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Nexis" in page.text
    assert client.get("/favicon.svg").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_state_and_history(client):
    state = client.get("/api/state").json()
    assert state["model"] == "qwen3.5:4b"
    assert state["handsfree"] is False
    history = client.get("/api/history").json()
    assert history["messages"][0]["content"] == "hello"


def test_chat_requires_text_and_starts_turn(client):
    assert client.post("/api/chat", json={"text": "  "}).status_code == 400
    response = client.post("/api/chat", json={"text": "list my files"})
    assert response.status_code == 200
    assert client.service.sent == ["list my files"]


def test_stop_and_model_switch(client):
    assert client.post("/api/stop").json() == {"stopping": True}
    assert client.service.stopped is True
    assert client.post("/api/model", json={"model": "qwen3:4b"}).status_code == 200
    assert client.get("/api/state").json()["model"] == "qwen3:4b"
    assert client.post("/api/model", json={}).status_code == 400


def test_confirm_unknown_and_known(client):
    assert client.post("/api/confirm", json={"id": "nope", "approved": True}).status_code == 404
    client.service.confirms["abc"] = False
    response = client.post("/api/confirm", json={"id": "abc", "approved": True})
    assert response.status_code == 200
    assert client.service.confirms["abc"] is True


def test_voice_handsfree_and_listen(client):
    assert client.post("/api/voice", json={"action": "start"}).json() == {"state": "recording"}
    assert client.service.voice_started is True
    assert client.post("/api/voice", json={"action": "stop"}).json() == {"text": "hello world"}
    assert client.post("/api/voice", json={"action": "dance"}).status_code == 400
    assert client.post("/api/handsfree", json={"on": True}).json() == {"on": True}
    assert client.post("/api/listen", json={"paused": True}).json() == {"paused": True}
    assert client.service.paused is True


def test_tts_requires_text_and_returns_audio(client):
    assert client.get("/api/tts").status_code == 400
    response = client.get("/api/tts", params={"text": "hello"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")


def test_new_session_and_open_workspace(client):
    assert client.post("/api/session/new").json() == {"session": "fresh"}
    assert client.post("/api/workspace/open").status_code == 200
    assert client.service.opened is True
