from __future__ import annotations

import os
import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

QUIET_PEAK = 40
TARGET_PEAK = 8000
TRIM_GATE_RATIO = 0.04
TRIM_PAD_SECONDS = 0.15


class CaptureError(RuntimeError):
    pass


def process_audio(data: Any, sample_rate: int) -> tuple[Any | None, dict[str, float]]:
    """Normalize quiet audio, trim edge silence, and report stats.

    Returns (processed_int16_array_or_None, stats). None means no usable speech.
    """
    import numpy as np

    data = np.asarray(data)
    if data.size == 0:
        return None, {"seconds": 0.0, "peak": 0, "rms": 0.0}
    peak = int(np.abs(data).max())
    stats = {"seconds": len(data) / sample_rate, "peak": peak, "rms": 0.0}
    if peak < QUIET_PEAK:
        return None, stats
    if peak < TARGET_PEAK:
        data = (data.astype("float32") * (TARGET_PEAK / peak)).clip(-32768, 32767).astype("int16")

    gate = max(int(np.abs(data).max() * TRIM_GATE_RATIO), 60)
    loud = np.abs(data) >= gate
    if loud.any():
        first = int(np.argmax(loud))
        last = len(loud) - int(np.argmax(loud[::-1]))
        pad = int(sample_rate * TRIM_PAD_SECONDS)
        data = data[max(0, first - pad) : min(len(data), last + pad)]

    stats["seconds"] = len(data) / sample_rate
    stats["peak"] = int(np.abs(data).max())
    stats["rms"] = float(np.sqrt(np.mean(data.astype("float64") ** 2)))
    return data, stats


def save_wav(data: Any, sample_rate: int, prefix: str = "nexis-rec-") -> Path:
    import numpy as np

    handle, name = tempfile.mkstemp(prefix=prefix, suffix=".wav")
    os.close(handle)
    path = Path(name)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(np.asarray(data, dtype="int16").tobytes())
    return path


class ClipRecorder:
    """Microphone recorder driven by explicit start/stop calls (no console)."""

    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._frames: list[Any] = []
        self._stream: Any = None
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        try:
            import numpy  # noqa: F401
            import sounddevice as sd
        except ImportError as exc:
            raise CaptureError(
                "Recording needs sounddevice and numpy. Install voice extras: "
                "pip install -e .[voice]"
            ) from exc
        with self._lock:
            if self._stream is not None:
                raise CaptureError("Already recording")
            self._frames = []

            def callback(indata, count, time_info, status) -> None:
                self._frames.append(indata.copy())

            self._stream = sd.InputStream(
                samplerate=self.sample_rate, channels=1, dtype="int16", callback=callback
            )
            self._stream.start()

    def stop(self) -> Any | None:
        import numpy as np

        with self._lock:
            stream, self._stream = self._stream, None
            frames, self._frames = self._frames, []
        if stream is None:
            return None
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        if not frames:
            return None
        return np.concatenate(frames, axis=0)
