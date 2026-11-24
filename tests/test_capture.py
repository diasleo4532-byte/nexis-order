from __future__ import annotations

import wave

import numpy as np
import pytest

from nexis.voice import capture


def test_quiet_audio_is_rejected():
    data = np.full(16000, 10, dtype="int16")
    processed, stats = capture.process_audio(data, 16000)
    assert processed is None
    assert stats["peak"] == 10


def test_empty_audio_is_rejected():
    processed, stats = capture.process_audio(np.zeros(0, dtype="int16"), 16000)
    assert processed is None
    assert stats["seconds"] == 0.0


def test_quiet_speech_is_normalized():
    sample_rate = 16000
    t = np.arange(sample_rate, dtype="float32") / sample_rate
    tone = (400 * np.sin(2 * np.pi * 300 * t)).astype("int16")
    processed, stats = capture.process_audio(tone, sample_rate)
    assert processed is not None
    assert stats["peak"] >= 7500
    assert processed.dtype == np.int16


def test_silence_is_trimmed():
    sample_rate = 16000
    t = np.arange(sample_rate, dtype="float32") / sample_rate
    tone = (9000 * np.sin(2 * np.pi * 440 * t)).astype("int16")
    data = np.concatenate([
        np.zeros(sample_rate, dtype="int16"),
        tone,
        np.zeros(sample_rate, dtype="int16"),
    ])
    processed, stats = capture.process_audio(data, sample_rate)
    assert processed is not None
    assert stats["seconds"] < 1.7
    assert stats["seconds"] > 1.0


def test_save_wav_writes_valid_file(tmp_path):
    data = np.full(8000, 1000, dtype="int16")
    path = capture.save_wav(data, 16000, prefix="nexis-test-")
    try:
        with wave.open(str(path), "rb") as wav_file:
            assert wav_file.getnchannels() == 1
            assert wav_file.getsampwidth() == 2
            assert wav_file.getframerate() == 16000
            assert wav_file.getnframes() == 8000
    finally:
        path.unlink(missing_ok=True)


def test_clip_recorder_stop_without_start_is_none():
    recorder = capture.ClipRecorder()
    assert recorder.active is False
    assert recorder.stop() is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Nexis, what's the weather?", "what's the weather"),
        ("nexis open my notes", "open my notes"),
        ("Hey Nexis", ""),
        ("Nexus, list my files", "list my files"),
        ("The nexus between two things", "between two things"),
        ("what time is it", None),
        ("", None),
    ],
)
def test_extract_command_trims_wake_word(text, expected):
    from nexis.voice.wake import extract_command

    assert extract_command(text) == expected
