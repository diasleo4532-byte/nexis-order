from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

import httpx

from nexis.llm.base import (
    ChatMessage,
    Completed,
    ProviderError,
    ProviderHealth,
    StreamEvent,
    TextDelta,
    ToolCall,
)


class OllamaError(ProviderError):
    pass


_CONNECTION_HINT = (
    "Ollama is not reachable at {host}. Start it with `ollama serve` "
    "(or launch the Ollama app), then pull your model with `ollama pull {model}`."
)


class OllamaProvider:
    def __init__(
        self,
        host: str = "http://127.0.0.1:11434",
        model: str = "qwen3:8b",
        temperature: float = 0.4,
        keep_alive: str = "30m",
        timeout: float = 180.0,
        think: bool | None = False,
        client: httpx.Client | None = None,
    ):
        self.host = host.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.keep_alive = keep_alive
        self.timeout = timeout
        self.think = think
        self._send_think = think is not None
        # trust_env=False: Ollama is local, and a malformed NO_PROXY in the
        # environment can crash httpx before the first request is made.
        self._client = client or httpx.Client(timeout=timeout, trust_env=False)
        self._lock = threading.Lock()
        self._response: httpx.Response | None = None
        self._aborted = False

    @property
    def client(self) -> httpx.Client:
        return self._client

    def close(self) -> None:
        self._client.close()

    def stop(self) -> None:
        """Abort an in-flight stream, even before the first token arrives."""
        with self._lock:
            self._aborted = True
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def health(self) -> ProviderHealth:
        try:
            response = self._client.get(f"{self.host}/api/tags", timeout=5.0)
            response.raise_for_status()
            payload = response.json()
            models = tuple(sorted(entry.get("name", "") for entry in payload.get("models", [])))
            return ProviderHealth(reachable=True, models=models)
        except httpx.HTTPError as exc:
            return ProviderHealth(reachable=False, detail=str(exc))

    def stream(
        self, messages: list[ChatMessage], tools: list[dict[str, Any]]
    ) -> Iterator[StreamEvent]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [message.to_wire() for message in messages],
            "stream": True,
            "options": {"temperature": self.temperature},
            "keep_alive": self.keep_alive,
        }
        if tools:
            payload["tools"] = tools
        if self._send_think:
            payload["think"] = self.think

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        with self._lock:
            self._aborted = False
        try:
            while True:
                with self._client.stream(
                    "POST", f"{self.host}/api/chat", json=payload, timeout=self.timeout
                ) as response:
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", errors="replace")
                        if "think" in payload and "think" in body.lower():
                            # Model does not accept the flag: retry once without it,
                            # and stop sending it for the rest of the session.
                            self._send_think = False
                            payload.pop("think")
                            continue
                        raise OllamaError(
                            f"Ollama returned HTTP {response.status_code}: {body[:400]}"
                        )
                    with self._lock:
                        if self._aborted:
                            raise OllamaError("Stopped by user")
                        self._response = response
                    try:
                        for line in response.iter_lines():
                            if not line.strip():
                                continue
                            chunk = json.loads(line)
                            if "error" in chunk:
                                raise OllamaError(f"Ollama error: {chunk['error']}")
                            message = chunk.get("message") or {}
                            piece = message.get("content") or ""
                            if piece:
                                content_parts.append(piece)
                                yield TextDelta(piece)
                            for call in message.get("tool_calls") or []:
                                tool_calls.append(_parse_tool_call(call))
                            if chunk.get("done"):
                                break
                    finally:
                        with self._lock:
                            if self._response is response:
                                self._response = None
                break
        except httpx.ConnectError as exc:
            raise OllamaError(
                _CONNECTION_HINT.format(host=self.host, model=self.model)
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(
                f"Ollama timed out after {self.timeout:g}s. The model may still be "
                "loading; try again, or raise llm.request_timeout in your config."
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaError(f"Ollama request failed: {exc}") from exc

        yield Completed(
            ChatMessage(
                role="assistant",
                content="".join(content_parts),
                tool_calls=tool_calls,
            )
        )


def _parse_tool_call(call: dict[str, Any]) -> ToolCall:
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    raw_args = function.get("arguments")
    if isinstance(raw_args, dict):
        arguments = raw_args
        raw_text = json.dumps(raw_args, ensure_ascii=False)
    else:
        raw_text = str(raw_args or "")
        try:
            parsed = json.loads(raw_text) if raw_text.strip() else {}
        except json.JSONDecodeError:
            parsed = {}
        arguments = parsed if isinstance(parsed, dict) else {"value": parsed}
    return ToolCall(name=name, arguments=arguments, raw_arguments=raw_text, id=str(call.get("id") or ""))
