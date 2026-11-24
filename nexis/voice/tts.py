from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from nexis.config import APP_DIR, VoiceSettings


class TTSError(RuntimeError):
    pass


def has_edge() -> bool:
    return importlib.util.find_spec("edge_tts") is not None


def find_ffplay(cfg: VoiceSettings) -> str | None:
    if cfg.ffplay_path and Path(cfg.ffplay_path).exists():
        return cfg.ffplay_path
    return shutil.which("ffplay")


def has_sapi() -> bool:
    return sys.platform == "win32" and shutil.which("powershell") is not None


class TextToSpeech:
    def __init__(self, cfg: VoiceSettings):
        self.cfg = cfg
        self.engine = self._pick_engine()
        self._process: subprocess.Popen | None = None
        self._temp_file: Path | None = None

    def _pick_engine(self) -> str:
        requested = self.cfg.tts_engine.lower()
        if requested == "auto":
            if has_edge() and find_ffplay(self.cfg):
                return "edge"
            if has_sapi():
                return "sapi"
            raise TTSError(
                "No text-to-speech backend found. Install voice extras "
                "(pip install -e .[voice]) or add ffplay.exe to PATH."
            )
        if requested == "edge":
            if not has_edge():
                raise TTSError("edge-tts is not installed. Run: pip install edge-tts")
            if not find_ffplay(self.cfg):
                raise TTSError(
                    "Edge TTS needs ffplay.exe to play audio. Install ffmpeg and add it to "
                    "PATH, or set voice.ffplay_path in your config."
                )
            return "edge"
        if requested == "sapi" and not has_sapi():
            raise TTSError("Windows SAPI text-to-speech is unavailable")
        return requested

    def speak(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.engine == "edge":
            self._speak_edge(text)
        elif self.engine == "sapi":
            self._speak_sapi(text)
        else:
            raise TTSError(f"Unknown TTS engine: {self.engine}")

    def _wait(self) -> None:
        if self._process is not None:
            try:
                self._process.wait()
            finally:
                self._process = None

    def synthesize(self, text: str) -> tuple[Path, str]:
        """Render speech to a cache file, returning (path, mime type).

        Prefers Edge TTS (natural voice, mp3) and falls back to Windows SAPI
        (wav) when offline. No ffplay is needed because the caller plays the file.
        """
        text = text.strip()
        if not text:
            raise TTSError("Nothing to synthesize")
        text = text[:2000]
        cache = APP_DIR / "tts-cache"
        cache.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(f"{self.cfg.tts_voice}|{text}".encode("utf-8")).hexdigest()
        mp3 = cache / f"{key}.mp3"
        wav = cache / f"{key}.wav"
        if mp3.exists():
            return mp3, "audio/mpeg"
        if wav.exists():
            return wav, "audio/wav"

        requested = self.cfg.tts_engine.lower()
        if requested in {"auto", "edge"} and has_edge():
            try:
                asyncio.run(self._edge_save(text, mp3, self.cfg.tts_voice))
                return mp3, "audio/mpeg"
            except Exception:
                mp3.unlink(missing_ok=True)
        if has_sapi():
            self._sapi_save(text, wav)
            return wav, "audio/wav"
        raise TTSError("No text-to-speech backend available")

    def _sapi_save(self, text: str, target: Path) -> None:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        escaped = str(target).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            f"$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
            "22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, "
            "[System.Speech.AudioFormat.AudioChannel]::Mono); "
            f"$s.SetOutputToWaveFile('{escaped}', $fmt); "
            "$s.Speak($text); $s.Dispose()"
        )
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=120,
            creationflags=creationflags,
        )
        if result.returncode != 0 or not target.exists():
            raise TTSError(f"SAPI synthesis failed: {result.stderr.decode(errors='replace')[:200]}")

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

    def _speak_edge(self, text: str) -> None:
        handle, name = tempfile.mkstemp(suffix=".mp3", prefix="nexis-tts-")
        os.close(handle)
        self._temp_file = Path(name)
        try:
            asyncio.run(self._edge_save(text, self._temp_file, self.cfg.tts_voice))
            ffplay = find_ffplay(self.cfg)
            if not ffplay:
                raise TTSError("Edge TTS needs ffplay.exe to play audio")
            self._process = subprocess.Popen(
                [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(self._temp_file)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wait()
        finally:
            if self._temp_file is not None:
                try:
                    self._temp_file.unlink(missing_ok=True)
                except OSError:
                    pass
                self._temp_file = None

    @staticmethod
    async def _edge_save(text: str, target: Path, voice: str) -> None:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice=voice)
        await communicate.save(str(target))

    def _speak_sapi(self, text: str) -> None:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        script = (
            "Add-Type -AssemblyName System.Speech; "
            f"$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')); "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$s.Speak($text)"
        )
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        self._process = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._wait()
