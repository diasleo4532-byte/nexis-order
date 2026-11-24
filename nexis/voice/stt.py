from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

from nexis.config import APP_DIR, VoiceSettings


class TranscriptionError(RuntimeError):
    pass


def has_faster_whisper() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def has_whisper_cpp(cfg: VoiceSettings) -> bool:
    return bool(
        cfg.whisper_cpp_path
        and Path(cfg.whisper_cpp_path).exists()
        and cfg.whisper_cpp_model
        and Path(cfg.whisper_cpp_model).exists()
    )


def has_sapi() -> bool:
    return sys.platform == "win32" and shutil.which("powershell") is not None


class Transcriber:
    def __init__(self, cfg: VoiceSettings):
        self.cfg = cfg
        self.engine = self._pick_engine()
        self._whisper = None

    def _pick_engine(self) -> str:
        requested = self.cfg.stt_engine.lower()
        if requested == "auto":
            if has_faster_whisper():
                return "faster-whisper"
            if has_whisper_cpp(self.cfg):
                return "whisper-cpp"
            if has_sapi():
                return "sapi"
            raise TranscriptionError(
                "No speech-to-text backend found. Install voice extras: "
                "pip install -e .[voice]"
            )
        if requested == "faster-whisper" and not has_faster_whisper():
            raise TranscriptionError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            )
        if requested == "whisper-cpp" and not has_whisper_cpp(self.cfg):
            raise TranscriptionError(
                "whisper.cpp is selected but voice.whisper_cpp_path / "
                "voice.whisper_cpp_model are not set to existing files."
            )
        if requested == "sapi" and not has_sapi():
            raise TranscriptionError("Windows SAPI speech recognition is unavailable")
        if requested not in {"faster-whisper", "whisper-cpp", "sapi"}:
            raise TranscriptionError(f"Unknown speech-to-text engine: {requested}")
        return requested

    def transcribe(self, wav_path: Path) -> str:
        if self.engine == "faster-whisper":
            return self._faster_whisper(wav_path)
        if self.engine == "whisper-cpp":
            return self._whisper_cpp(wav_path)
        return self._sapi(wav_path)

    def _faster_whisper(self, wav_path: Path) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError("faster-whisper is not installed") from exc
        if self._whisper is None:
            model_dir = APP_DIR / "models" / "whisper"
            model_dir.mkdir(parents=True, exist_ok=True)
            self._whisper = WhisperModel(
                self.cfg.model_size,
                device="cpu",
                compute_type="int8",
                download_root=str(model_dir),
            )
        segments, _info = self._whisper.transcribe(
            str(wav_path),
            beam_size=5,
            language=self.cfg.language if self.cfg.language != "auto" else None,
            vad_filter=True,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _whisper_cpp(self, wav_path: Path) -> str:
        command = [
            self.cfg.whisper_cpp_path,
            "-m",
            self.cfg.whisper_cpp_model,
            "-f",
            str(wav_path),
            "-nt",
            "-l",
            self.cfg.language,
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode != 0:
            raise TranscriptionError(
                f"whisper.cpp failed: {result.stderr.strip()[:300] or 'unknown error'}"
            )
        return result.stdout.strip()

    def _sapi(self, wav_path: Path) -> str:
        escaped = str(wav_path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$r = New-Object System.Speech.Recognition.SpeechRecognitionEngine; "
            f"$r.SetInputToWaveFile('{escaped}'); "
            "$r.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar)); "
            "$result = $r.Recognize(); "
            "if ($result -ne $null) { [Console]::Out.Write($result.Text) }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            raise TranscriptionError(
                "Windows speech recognition failed. Make sure a speech language is "
                f"installed. Details: {result.stderr.strip()[:300]}"
            )
        return result.stdout.strip()
