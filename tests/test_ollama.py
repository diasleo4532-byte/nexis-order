from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nexis.llm import ChatMessage, Completed, OllamaError, OllamaProvider, TextDelta

REQUESTS: list[dict] = []
REJECT_THINK = False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):  # keep test output clean
        pass

    def do_GET(self):
        if self.path != "/api/tags":
            self.send_error(404)
            return
        body = json.dumps(
            {"models": [{"name": "qwen3:8b"}, {"name": "llama3.1:8b"}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        REQUESTS.append(payload)
        if REJECT_THINK and "think" in payload:
            body = json.dumps({"error": "model does not support thinking"}).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        assert payload["stream"] is True
        assert payload["model"] == "qwen3:8b"
        chunks = [
            {"message": {"role": "assistant", "content": "Hello "}, "done": False},
            {
                "message": {
                    "role": "assistant",
                    "content": "there.",
                    "tool_calls": [
                        {"function": {"name": "echo", "arguments": "{\"text\": \"hi\"}"}}
                    ],
                },
                "done": False,
            },
            {"message": {"role": "assistant", "content": ""}, "done": True},
        ]
        data = "\n".join(json.dumps(chunk) for chunk in chunks) + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        self.wfile.write(data.encode("utf-8"))
        self.wfile.flush()


@pytest.fixture()
def ollama_server():
    global REJECT_THINK
    REQUESTS.clear()
    REJECT_THINK = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_health_lists_models(ollama_server):
    provider = OllamaProvider(host=ollama_server, model="qwen3:8b")
    health = provider.health()
    provider.close()
    assert health.reachable is True
    assert "qwen3:8b" in health.models


def test_stream_parses_content_and_tool_calls(ollama_server):
    provider = OllamaProvider(host=ollama_server, model="qwen3:8b")
    events = list(provider.stream([ChatMessage("user", "hi")], tools=[]))
    provider.close()

    text = "".join(event.text for event in events if isinstance(event, TextDelta))
    completed = [event for event in events if isinstance(event, Completed)]
    assert text == "Hello there."
    assert len(completed) == 1
    message = completed[0].message
    assert message.role == "assistant"
    assert len(message.tool_calls) == 1
    call = message.tool_calls[0]
    assert call.name == "echo"
    assert call.arguments == {"text": "hi"}


def test_think_flag_sent_by_default_and_optional(ollama_server):
    provider = OllamaProvider(host=ollama_server, model="qwen3:8b")
    list(provider.stream([ChatMessage("user", "hi")], tools=[]))
    provider.close()
    assert REQUESTS[-1]["think"] is False

    quiet = OllamaProvider(host=ollama_server, model="qwen3:8b", think=None)
    list(quiet.stream([ChatMessage("user", "hi")], tools=[]))
    quiet.close()
    assert "think" not in REQUESTS[-1]


def test_think_falls_back_when_model_rejects_flag(ollama_server):
    global REJECT_THINK
    REJECT_THINK = True
    provider = OllamaProvider(host=ollama_server, model="qwen3:8b")
    events = list(provider.stream([ChatMessage("user", "hi")], tools=[]))
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello there."
    assert len(REQUESTS) == 2
    assert "think" in REQUESTS[0]
    assert "think" not in REQUESTS[1]

    list(provider.stream([ChatMessage("user", "again")], tools=[]))
    provider.close()
    assert "think" not in REQUESTS[-1]


def test_unreachable_ollama_raises_helpful_error():
    provider = OllamaProvider(host="http://127.0.0.1:9", model="qwen3:8b")
    health = provider.health()
    assert health.reachable is False
    with pytest.raises(OllamaError, match="ollama serve"):
        list(provider.stream([ChatMessage("user", "hi")], tools=[]))
    provider.close()
