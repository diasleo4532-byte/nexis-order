from __future__ import annotations

import re
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from nexis.config import VoiceSettings
from nexis.voice import capture as capture_module
from nexis.voice import stt as stt_module

SPEECH_RMS = 220.0
SILENCE_SECONDS = 0.7
PREROLL_BLOCKS = 3
MAX_UTTERANCE_SECONDS = 12.0
MIN_UTTERANCE_SECONDS = 0.4
BLOCK_SECONDS = 0.1

_WAKE_TOKENS = {"nexis", "nexus", "nexys", "neksis", "nexos", "nexa"}


def extract_command(transcript: str) -> str | None:
    """Strip the wake word; return the remaining command.

    Returns None when the wake word is absent, and "" when the user said only
    the wake word.
    """
    cleaned = re.sub(r"[^A-Za-z0-9' ]+", " ", transcript or "").strip()
    words = cleaned.split()
    for index, word in enumerate(words):
        token = word.lower().strip("'")
        if token in _WAKE_TOKENS or token.startswith(("nexi", "nexu")):
            return " ".join(words[index + 1 :]).strip(" ,.:;!?-")
    return None


class WakeListener:
    """Always-listening wake word detection with local whisper transcription.

    Audio stays on this machine. Utterances are only transcribed locally; no
    recording is persisted.
    """

    def __init__(
        self,
        cfg: VoiceSettings,
        on_command: Callable[[str, str], None],
        on_state: Callable[[str], None] | None = None,
    ):
        self.cfg = cfg
        self.on_command = on_command
        self.on_state = on_state or (lambda state: None)
        self._transcriber: stt_module.Transcriber | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.pause()

    @property
    def transcriber(self) -> stt_module.Transcriber:
        if self._transcriber is None:
            self._transcriber = stt_module.Transcriber(self.cfg)
        return self._transcriber

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return True
        self._stop.clear()
        self.resume()
        self._thread = threading.Thread(target=self._run, name="nexis-wake", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        self.on_state("off")

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def _run(self) -> None:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            self.on_state("unavailable")
            return

        rate = self.cfg.sample_rate
        block = int(rate * BLOCK_SECONDS)
        max_blocks = int(MAX_UTTERANCE_SECONDS / BLOCK_SECONDS)
        silence_blocks = int(SILENCE_SECONDS / BLOCK_SECONDS)
        try:
            stream = sd.InputStream(samplerate=rate, channels=1, dtype="int16")
            stream.start()
        except Exception:
            self.on_state("unavailable")
            return

        self.on_state("listening")
        pre: deque[Any] = deque(maxlen=PREROLL_BLOCKS)
        utterance: list[Any] | None = None
        quiet_run = 0
        try:
            while not self._stop.is_set():
                data, overflowed = stream.read(block)
                if self._paused.is_set():
                    utterance = None
                    quiet_run = 0
                    pre.clear()
                    self.on_state("paused")
                    continue
                samples = data[:, 0] if data.ndim > 1 else data
                rms = float(np.sqrt(np.mean(samples.astype("float64") ** 2)))
                if utterance is None:
                    pre.append(samples.copy())
                    if rms >= SPEECH_RMS:
                        utterance = list(pre)
                        quiet_run = 0
                        self.on_state("hearing")
                    continue

                utterance.append(samples.copy())
                quiet_run = quiet_run + 1 if rms < SPEECH_RMS * 0.6 else 0
                if quiet_run >= silence_blocks or len(utterance) >= max_blocks:
                    self._handle(np.concatenate(utterance), rate)
                    utterance = None
                    quiet_run = 0
                    pre.clear()
                    self.on_state("listening" if not self._paused.is_set() else "paused")
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _handle(self, data: Any, rate: int) -> None:
        processed, stats = capture_module.process_audio(data, rate)
        if processed is None or stats["seconds"] < MIN_UTTERANCE_SECONDS:
            return
        wav = capture_module.save_wav(processed, rate, prefix="nexis-wake-")
        try:
            self.on_state("transcribing")
            text = self.transcriber.transcribe(wav)
        except Exception:
            return
        finally:
            wav.unlink(missing_ok=True)
        command = extract_command(text)
        if command is None:
            return
        self.pause()
        self.on_state("wake")
        self.on_command(command, text)
