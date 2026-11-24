from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
import uuid
from typing import Any

from nexis.agent import Agent, AgentEvent
from nexis.audit import AuditLog
from nexis.config import APP_DIR, Settings
from nexis.llm import OllamaProvider
from nexis.memory import MemoryStore
from nexis.safety import ConfirmationRequest, SafetyPolicy
from nexis.tools import build_registry
from nexis.tools.browser import WorkspaceBrowser
from nexis.voice import capture as capture_module
from nexis.voice import stt as stt_module
from nexis.voice import tts as tts_module
from nexis.voice.wake import WakeListener
from nexis.workspace import Workspace

CONFIRM_TIMEOUT = 180.0


class NexisService:
    """Headless Nexis runtime shared by the desktop app (and its web UI)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.audit = AuditLog(APP_DIR / "audit.jsonl")
        self.workspace = Workspace(settings.workspace.path)
        self.memory = MemoryStore(settings.memory.db_path)
        self.browser = WorkspaceBrowser(
            self.workspace,
            user_data_dir=APP_DIR / "browser",
            headless=False,
        )
        self.registry = build_registry(settings, self.workspace, self.memory, self.browser)
        self.provider = OllamaProvider(
            host=settings.llm.host,
            model=settings.llm.model,
            temperature=settings.llm.temperature,
            keep_alive=settings.llm.keep_alive,
            timeout=settings.llm.request_timeout,
            think=settings.llm.think,
        )
        self.safety = SafetyPolicy(
            require_confirmation=settings.safety.require_confirmation,
            confirmer=self._confirm,
        )
        self.agent = Agent(
            provider=self.provider,
            registry=self.registry,
            memory=self.memory,
            settings=settings,
            audit=self.audit,
            workspace=self.workspace,
            safety=self.safety,
        )
        self.session_id = "app"
        self.memory.touch_session(self.session_id)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._clients: set[asyncio.Queue] = set()
        self._turn_lock = threading.Lock()
        self._turn_active = False
        self._turn_started = 0.0
        self._turn_cancel = threading.Event()
        self._confirms: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._recorder = capture_module.ClipRecorder(settings.voice.sample_rate)
        self._transcriber: stt_module.Transcriber | None = None
        try:
            self._tts: tts_module.TextToSpeech | None = tts_module.TextToSpeech(settings.voice)
        except Exception:
            self._tts = None
        self._wake: WakeListener | None = None
        self._handsfree = False
        self._voice_lock = threading.Lock()

    # ---------- event plumbing ----------

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._clients.discard(queue)

    def emit(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        for queue in list(self._clients):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                self._clients.discard(queue)

    # ---------- chat ----------

    @property
    def turn_active(self) -> bool:
        return self._turn_active

    def start_turn(self, text: str) -> bool:
        text = text.strip()
        if not text or len(text) > 8000:
            return False
        with self._turn_lock:
            if self._turn_active:
                return False
            self._turn_active = True
            self._turn_started = time.time()
            self._turn_cancel.clear()
        threading.Thread(
            target=self._run_turn, args=(text,), name="nexis-turn", daemon=True
        ).start()
        return True

    def _run_turn(self, text: str) -> None:
        started = time.time()
        try:
            result = self.agent.run(
                text, session_id=self.session_id, emit=self._emit_agent, stream=True
            )
            self.emit(
                {
                    "type": "done",
                    "text": result.text,
                    "steps": result.steps,
                    "tools": len(result.tools_used),
                    "seconds": round(time.time() - started, 1),
                    "interrupted": result.interrupted,
                }
            )
        except Exception as exc:
            self.emit({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
            self.emit(
                {
                    "type": "done",
                    "text": "",
                    "steps": 0,
                    "tools": 0,
                    "seconds": round(time.time() - started, 1),
                    "interrupted": True,
                }
            )
        finally:
            with self._turn_lock:
                self._turn_active = False

    def stop_turn(self) -> None:
        if not self._turn_active:
            return
        self._turn_cancel.set()
        self.agent.interrupt()
        for waiter, entry in list(self._confirms.values()):
            entry["approved"] = False
            waiter.set()

    def _emit_agent(self, event: AgentEvent) -> None:
        self.emit(
            {
                "type": event.kind,
                "text": event.text,
                "tool": event.tool,
                "detail": event.detail,
            }
        )

    # ---------- confirmations ----------

    def _confirm(self, request: ConfirmationRequest) -> bool:
        token = uuid.uuid4().hex[:10]
        entry: dict[str, Any] = {"approved": False}
        waiter = threading.Event()
        self._confirms[token] = (waiter, entry)
        self.emit(
            {
                "type": "confirm",
                "id": token,
                "tool": request.tool,
                "summary": request.summary,
                "details": request.details,
                "risk": request.risk.value,
            }
        )
        deadline = time.monotonic() + CONFIRM_TIMEOUT
        got_answer = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if waiter.wait(min(0.2, remaining)):
                got_answer = True
                break
            if self._turn_cancel.is_set():
                break
        self._confirms.pop(token, None)
        if self._turn_cancel.is_set():
            return False
        return bool(got_answer and entry["approved"])

    def resolve_confirm(self, token: str, approved: bool) -> bool:
        found = self._confirms.get(token)
        if not found:
            return False
        waiter, entry = found
        entry["approved"] = bool(approved)
        waiter.set()
        return True

    # ---------- voice ----------

    @property
    def transcriber(self) -> stt_module.Transcriber:
        if self._transcriber is None:
            self._transcriber = stt_module.Transcriber(self.settings.voice)
        return self._transcriber

    def voice_start(self) -> str:
        with self._voice_lock:
            if self._wake is not None:
                self._wake.pause()
            self.emit({"type": "voice_state", "state": "recording"})
            self._recorder.start()
        return "recording"

    def voice_stop(self) -> str:
        with self._voice_lock:
            data = self._recorder.stop()
        self._resume_listening()
        if data is None:
            self.emit({"type": "voice_state", "state": "idle"})
            return ""
        processed, _stats = capture_module.process_audio(data, self.settings.voice.sample_rate)
        if processed is None:
            self.emit({"type": "voice_state", "state": "idle"})
            return ""
        self.emit({"type": "voice_state", "state": "transcribing"})
        wav = capture_module.save_wav(processed, self.settings.voice.sample_rate)
        try:
            text = self.transcriber.transcribe(wav).strip()
        except Exception as exc:
            self.emit({"type": "notice", "text": f"Transcription failed: {exc}"})
            text = ""
        finally:
            wav.unlink(missing_ok=True)
        self.emit({"type": "voice_state", "state": "idle"})
        return text

    def set_listening_paused(self, paused: bool) -> None:
        if self._wake is None:
            return
        if paused:
            self._wake.pause()
        else:
            self._wake.resume()

    def _resume_listening(self) -> None:
        if self._wake is not None:
            self._wake.resume()

    @property
    def handsfree(self) -> bool:
        return self._handsfree

    def set_handsfree(self, on: bool) -> bool:
        if on and not self._handsfree:
            if self._wake is None:
                self._wake = WakeListener(
                    self.settings.voice, self._on_wake, self._on_wake_state
                )
            self._handsfree = True
            self._wake.start()
        elif not on and self._handsfree:
            self._handsfree = False
            if self._wake is not None:
                self._wake.stop()
                self._wake = None
        self.emit({"type": "handsfree", "on": self._handsfree})
        return self._handsfree

    def _on_wake(self, command: str, transcript: str) -> None:
        self.emit({"type": "wake", "command": command, "transcript": transcript})

    def _on_wake_state(self, state: str) -> None:
        self.emit({"type": "wake_state", "state": state})

    def synthesize(self, text: str) -> tuple[Any, str]:
        if self._tts is None:
            raise RuntimeError("No text-to-speech backend is available")
        return self._tts.synthesize(text)

    # ---------- settings / data ----------

    def set_model(self, name: str) -> None:
        name = name.strip()
        if not name:
            return
        self.provider.model = name
        self.settings = dataclasses.replace(
            self.settings, llm=dataclasses.replace(self.settings.llm, model=name)
        )
        self.agent.settings = self.settings
        self.audit.record("model_changed", model=name)
        self.emit({"type": "model", "model": name})

    def state(self) -> dict[str, Any]:
        health = self.provider.health()
        return {
            "model": self.provider.model,
            "models": list(health.models) if health.reachable else [],
            "ollama": health.reachable,
            "ollama_detail": health.detail if not health.reachable else "",
            "handsfree": self._handsfree,
            "wake_word": self.settings.voice.wake_word,
            "workspace": str(self.workspace.root),
            "session": self.session_id,
            "turn_active": self._turn_active,
            "version": _version(),
        }

    def history(self, turns: int = 40) -> list[dict[str, str]]:
        return self.memory.history(self.session_id, turns=turns)

    def new_session(self) -> str:
        self.session_id = uuid.uuid4().hex[:12]
        self.memory.touch_session(self.session_id)
        self.emit({"type": "session", "session": self.session_id})
        return self.session_id

    def open_workspace(self) -> None:
        import os
        import subprocess
        import sys

        target = str(self.workspace.root)
        if sys.platform == "win32":
            os.startfile(target)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", target])

    # ---------- lifecycle ----------

    def close(self) -> None:
        try:
            if self._handsfree:
                self.set_handsfree(False)
            if self._recorder.active:
                self._recorder.stop()
        except Exception:
            pass
        try:
            self.browser.close()
        except Exception:
            pass
        try:
            self.provider.close()
        except Exception:
            pass
        try:
            self.memory.close()
        except Exception:
            pass


def _version() -> str:
    from nexis import __version__

    return __version__
