from __future__ import annotations

import importlib.util
import os
import threading
from pathlib import Path

from rich.console import Console

from nexis.config import APP_DIR, VoiceSettings
from nexis.voice import capture as capture_module
from nexis.voice import stt as stt_module
from nexis.voice import tts as tts_module


class VoiceError(RuntimeError):
    pass


class VoiceSession:
    """Push-to-talk voice loop: record -> transcribe -> (agent) -> speak."""

    def __init__(self, cfg: VoiceSettings, console: Console):
        self.cfg = cfg
        self.console = console
        self._transcriber: stt_module.Transcriber | None = None
        self._tts: tts_module.TextToSpeech | None = None
        self._stream = None
        self._last_stats: dict[str, float] = {}

    @property
    def transcriber(self) -> stt_module.Transcriber:
        if self._transcriber is None:
            self._transcriber = stt_module.Transcriber(self.cfg)
        return self._transcriber

    @property
    def tts(self) -> tts_module.TextToSpeech:
        if self._tts is None:
            self._tts = tts_module.TextToSpeech(self.cfg)
        return self._tts

    def status(self) -> dict[str, object]:
        mic = importlib.util.find_spec("sounddevice") is not None
        problems: list[str] = []
        if not mic:
            problems.append("sounddevice/numpy missing (pip install -e .[voice])")

        if stt_module.has_faster_whisper():
            stt_backend = "faster-whisper"
        elif stt_module.has_whisper_cpp(self.cfg):
            stt_backend = "whisper.cpp"
        elif stt_module.has_sapi():
            stt_backend = "windows-sapi"
        else:
            stt_backend = "none"
            problems.append("no speech-to-text backend")

        if tts_module.has_edge() and tts_module.find_ffplay(self.cfg):
            tts_backend = "edge-tts"
        elif tts_module.has_sapi():
            tts_backend = "windows-sapi"
        elif tts_module.has_edge():
            tts_backend = "edge-tts (ffplay missing, will not play)"
            problems.append("ffplay.exe not found for Edge TTS")
        else:
            tts_backend = "none"
            problems.append("no text-to-speech backend")

        return {
            "microphone": mic,
            "stt": stt_backend,
            "tts": tts_backend,
            "problems": problems,
        }

    def available(self) -> tuple[bool, str]:
        info = self.status()
        problems = info["problems"]
        if problems:
            return False, "; ".join(str(p) for p in problems)
        return True, f"STT: {info['stt']}, TTS: {info['tts']}"

    def listen(self) -> str | None:
        try:
            wav = self._record()
        except VoiceError as exc:
            self.console.print(f"[red]Voice input error:[/] {exc}")
            return None
        try:
            with self.console.status("[dim]Transcribing…[/]", spinner="dots"):
                try:
                    text = self.transcriber.transcribe(wav)
                except Exception as exc:
                    self.console.print(f"[red]Transcription failed:[/] {exc}")
                    return None
        finally:
            wav.unlink(missing_ok=True)

        text = text.strip()
        if not text:
            self.console.print("[dim]I did not catch that.[/]")
            return None
        if self.cfg.require_wake_word:
            lowered = text.lower()
            index = lowered.find(self.cfg.wake_word)
            if index < 0:
                self.console.print(
                    f"[dim]Ignored (wake word '{self.cfg.wake_word}' not detected).[/]"
                )
                return None
            text = text[index + len(self.cfg.wake_word) :].lstrip(" ,.:;!?-")
            if not text:
                return None
        self.console.print(f"[bold cyan]You (voice) ›[/] {text}")
        return text

    def _record(self) -> Path:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise VoiceError(
                "Recording needs sounddevice and numpy. Install voice extras: "
                "pip install -e .[voice]"
            ) from exc

        sample_rate = self.cfg.sample_rate
        frames: list = []

        def callback(indata, count, time_info, status) -> None:
            frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=sample_rate, channels=1, dtype="int16", callback=callback
        )
        self._stream.start()
        try:
            self.console.input(
                "[bold red]● Recording[/] — speak now, press [bold]Enter[/] to stop."
            )
        finally:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        if not frames:
            raise VoiceError("No audio was captured. Check your microphone.")
        data, stats = capture_module.process_audio(np.concatenate(frames, axis=0), sample_rate)
        if data is None:
            raise VoiceError(
                "I did not hear anything. Check that your microphone is not muted "
                "and raise its input volume in Windows sound settings."
            )
        self._last_stats = stats
        path = capture_module.save_wav(data, sample_rate)

        if os.environ.get("NEXIS_KEEP_AUDIO"):
            kept = APP_DIR / "last-recording.wav"
            try:
                kept.write_bytes(path.read_bytes())
                self.console.print(f"[dim]Recording kept at {kept}[/]")
            except OSError:
                pass
        return path

    def test_mic(self) -> None:
        try:
            wav = self._record()
        except VoiceError as exc:
            self.console.print(f"[red]Mic test failed:[/] {exc}")
            return
        try:
            stats = self._last_stats
            self.console.print(
                f"[dim]Captured {stats.get('seconds', 0.0):.1f}s of audio "
                f"(peak {stats.get('peak', 0)}, rms {stats.get('rms', 0):.0f}), "
                f"engine: {self.transcriber.engine}[/]"
            )
            with self.console.status("[dim]Transcribing…[/]", spinner="dots"):
                text = self.transcriber.transcribe(wav)
            if text.strip():
                self.console.print(f"[bold]Heard:[/] {text.strip()}")
            else:
                self.console.print(
                    "[yellow]No text recognized.[/] Try speaking closer to the microphone."
                )
        except Exception as exc:
            self.console.print(f"[red]Transcription failed:[/] {exc}")
        finally:
            wav.unlink(missing_ok=True)

    def say(self, text: str) -> None:
        if not text.strip():
            return
        engine = self.tts
        if not self.cfg.barge_in:
            try:
                engine.speak(text)
            except Exception as exc:
                self.console.print(f"[red]Speech output failed:[/] {exc}")
            return

        stop_monitor = threading.Event()

        def monitor() -> None:
            try:
                import numpy as np
                import sounddevice as sd
            except ImportError:
                return
            try:
                with sd.InputStream(
                    samplerate=self.cfg.sample_rate, channels=1, dtype="int16"
                ) as stream:
                    loud = 0
                    while not stop_monitor.is_set():
                        data, _ = stream.read(int(self.cfg.sample_rate * 0.1))
                        rms = float(np.sqrt(np.mean(data.astype("float64") ** 2)))
                        if rms > 1200:
                            loud += 1
                            if loud >= 2:
                                engine.stop()
                                return
                        else:
                            loud = 0
            except Exception:
                return

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        try:
            engine.speak(text)
        except Exception as exc:
            self.console.print(f"[red]Speech output failed:[/] {exc}")
        finally:
            stop_monitor.set()
            thread.join(timeout=0.5)

    def stop_speaking(self) -> None:
        if self._tts is not None:
            self._tts.stop()
        if self._stream is not None:
            try:
                self._stream.abort()
            except Exception:
                pass
