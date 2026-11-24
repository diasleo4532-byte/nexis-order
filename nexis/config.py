from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_DIR = Path(os.environ.get("NEXIS_HOME", str(Path.home() / ".nexis"))).expanduser()

DEFAULT_CONFIG_TEXT = """\
# Nexis Order configuration
# Location: %{app_dir}/config.toml
# Edit and restart Nexis to apply changes.

[llm]
provider = "ollama"                 # provider id; only "ollama" ships today
host = "http://127.0.0.1:11434"
model = "qwen3.5:4b"                  # any tool-calling model pulled in Ollama
temperature = 0.4
keep_alive = "30m"                  # how long Ollama keeps the model in VRAM
request_timeout = 180.0
think = false                       # false disables hidden reasoning (much faster replies)

[agent]
max_steps = 16                      # max tool-call rounds per turn
history_turns = 20                  # conversation turns replayed to the model
proactive = true                    # allow Nexis to offer one useful suggestion

[workspace]
path = "~/NexisWorkspace"           # every file operation is confined here

[memory]
max_facts_in_prompt = 40            # saved preferences injected into the prompt

[safety]
require_confirmation = true         # ask before destructive or irreversible actions

[voice]
enabled = false
stt_engine = "auto"                 # auto | faster-whisper | whisper-cpp | sapi
tts_engine = "auto"                 # auto | edge | sapi
wake_word = "nexis"
require_wake_word = false           # true -> only respond when the wake word is spoken
language = "en"
model_size = "base.en"              # faster-whisper model
whisper_cpp_path = ""               # path to whisper-cli.exe if using whisper.cpp
whisper_cpp_model = ""              # path to a ggml .bin model
tts_voice = "en-GB-RyanNeural"      # Edge TTS voice
ffplay_path = ""                    # ffplay.exe used to play Edge TTS audio
barge_in = false                    # stop speaking when the user talks (experimental, needs mic)
sample_rate = 16000

[ui]
stream = true                       # stream assistant text as it is generated
"""


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "ollama"
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:4b"
    temperature: float = 0.4
    keep_alive: str = "30m"
    request_timeout: float = 180.0
    think: bool = False


@dataclass(frozen=True)
class AgentSettings:
    max_steps: int = 16
    history_turns: int = 20
    proactive: bool = True


@dataclass(frozen=True)
class WorkspaceSettings:
    path: Path = Path.home() / "NexisWorkspace"


@dataclass(frozen=True)
class MemorySettings:
    db_path: Path = APP_DIR / "nexis.db"
    max_facts_in_prompt: int = 40


@dataclass(frozen=True)
class SafetySettings:
    require_confirmation: bool = True


@dataclass(frozen=True)
class VoiceSettings:
    enabled: bool = False
    stt_engine: str = "auto"
    tts_engine: str = "auto"
    wake_word: str = "nexis"
    require_wake_word: bool = False
    language: str = "en"
    model_size: str = "base.en"
    whisper_cpp_path: str = ""
    whisper_cpp_model: str = ""
    tts_voice: str = "en-GB-RyanNeural"
    ffplay_path: str = ""
    barge_in: bool = False
    sample_rate: int = 16000


@dataclass(frozen=True)
class UISettings:
    stream: bool = True


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    agent: AgentSettings
    workspace: WorkspaceSettings
    memory: MemorySettings
    safety: SafetySettings
    voice: VoiceSettings
    ui: UISettings
    source: Path


def _expand(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _defaults() -> dict[str, Any]:
    return {
        "llm": {
            "provider": "ollama",
            "host": "http://127.0.0.1:11434",
            "model": "qwen3.5:4b",
            "temperature": 0.4,
            "keep_alive": "30m",
            "request_timeout": 180.0,
            "think": False,
        },
        "agent": {"max_steps": 16, "history_turns": 20, "proactive": True},
        "workspace": {"path": "~/NexisWorkspace"},
        "memory": {"db_path": str(APP_DIR / "nexis.db"), "max_facts_in_prompt": 40},
        "safety": {"require_confirmation": True},
        "voice": {
            "enabled": False,
            "stt_engine": "auto",
            "tts_engine": "auto",
            "wake_word": "nexis",
            "require_wake_word": False,
            "language": "en",
            "model_size": "base.en",
            "whisper_cpp_path": "",
            "whisper_cpp_model": "",
            "tts_voice": "en-GB-RyanNeural",
            "ffplay_path": "",
            "barge_in": False,
            "sample_rate": 16000,
        },
        "ui": {"stream": True},
    }


def _ensure_default_file(path: Path) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    text = DEFAULT_CONFIG_TEXT.replace("%{app_dir}", str(path.parent))
    path.write_text(text, encoding="utf-8")


def load_settings(config_path: Path | None = None) -> Settings:
    source = _expand(config_path) if config_path else APP_DIR / "config.toml"
    _ensure_default_file(source)
    raw: dict[str, Any] = {}
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"Invalid config file {source}: {exc}") from exc
    data = _deep_merge(_defaults(), raw)

    llm = data["llm"]
    agent = data["agent"]
    workspace = data["workspace"]
    memory = data["memory"]
    safety = data["safety"]
    voice = data["voice"]
    ui = data["ui"]

    return Settings(
        llm=LLMSettings(
            provider=str(llm["provider"]),
            host=str(llm["host"]).rstrip("/"),
            model=str(llm["model"]),
            temperature=float(llm["temperature"]),
            keep_alive=str(llm["keep_alive"]),
            request_timeout=float(llm["request_timeout"]),
            think=bool(llm["think"]),
        ),
        agent=AgentSettings(
            max_steps=int(agent["max_steps"]),
            history_turns=int(agent["history_turns"]),
            proactive=bool(agent["proactive"]),
        ),
        workspace=WorkspaceSettings(path=_expand(workspace["path"])),
        memory=MemorySettings(
            db_path=_expand(memory["db_path"]),
            max_facts_in_prompt=int(memory["max_facts_in_prompt"]),
        ),
        safety=SafetySettings(require_confirmation=bool(safety["require_confirmation"])),
        voice=VoiceSettings(
            enabled=bool(voice["enabled"]),
            stt_engine=str(voice["stt_engine"]),
            tts_engine=str(voice["tts_engine"]),
            wake_word=str(voice["wake_word"]).strip().lower(),
            require_wake_word=bool(voice["require_wake_word"]),
            language=str(voice["language"]),
            model_size=str(voice["model_size"]),
            whisper_cpp_path=str(voice["whisper_cpp_path"]),
            whisper_cpp_model=str(voice["whisper_cpp_model"]),
            tts_voice=str(voice["tts_voice"]),
            ffplay_path=str(voice["ffplay_path"]),
            barge_in=bool(voice["barge_in"]),
            sample_rate=int(voice["sample_rate"]),
        ),
        ui=UISettings(stream=bool(ui["stream"])),
        source=source,
    )
