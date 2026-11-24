from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from nexis import IDENTITY, __version__
from nexis.agent import Agent, AgentError, AgentEvent
from nexis.audit import AuditLog
from nexis.config import APP_DIR, Settings, load_settings
from nexis.llm import OllamaProvider
from nexis.memory import MemoryStore
from nexis.safety import ConfirmationRequest, SafetyPolicy
from nexis.tools import build_registry
from nexis.tools.browser import WorkspaceBrowser
from nexis.workspace import Workspace

HELP_ROWS = [
    ("/help", "Show this help"),
    ("/new", "Start a fresh conversation session"),
    ("/sessions", "List recent sessions"),
    ("/facts", "Show saved preferences"),
    ("/forget <key>", "Delete a saved preference"),
    ("/model [name]", "Show or switch the Ollama model"),
    ("/workspace [open]", "Show the Nexis Workspace, optionally open it"),
    ("/audit [n]", "Show the last n audit entries (default 10)"),
    ("/mic", "Record once and show what speech-to-text heard"),
    ("/voice [on|off]", "Toggle push-to-talk voice mode"),
    ("/check", "Show environment diagnostics"),
    ("/quit", "Exit (Ctrl+C also works)"),
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexis",
        description="Nexis Order - a local-first, voice-controlled personal AI agent.",
    )
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    parser.add_argument("--model", help="Ollama model to use for this run")
    parser.add_argument("--workspace", type=Path, help="Override the Nexis Workspace path")
    parser.add_argument("--session", default="default", help="Session id (default: default)")
    parser.add_argument("--voice", action="store_true", help="Start in voice mode")
    parser.add_argument("--no-voice", action="store_true", help="Disable voice mode")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming output")
    parser.add_argument("--check", action="store_true", help="Print environment diagnostics and exit")
    parser.add_argument("--version", action="version", version=f"Nexis Order {__version__}")
    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    if args.model:
        settings = dataclasses.replace(
            settings, llm=dataclasses.replace(settings.llm, model=args.model)
        )
    if args.workspace:
        settings = dataclasses.replace(
            settings,
            workspace=dataclasses.replace(
                settings.workspace, path=Path(args.workspace).expanduser()
            ),
        )
    if args.no_stream:
        settings = dataclasses.replace(
            settings, ui=dataclasses.replace(settings.ui, stream=False)
        )
    return settings


def check_environment(settings: Settings, console: Console) -> int:
    table = Table(title="Nexis environment check", show_lines=False)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details", style="dim")

    table.add_row("Python", "ok", f"{sys.version.split()[0]} on {sys.platform}")

    config_ok = settings.source.exists()
    table.add_row("Config", "ok" if config_ok else "missing", str(settings.source))

    workspace = Workspace(settings.workspace.path)
    try:
        probe = workspace.root / ".nexis-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        table.add_row("Workspace", "ok", str(workspace.root))
    except OSError as exc:
        table.add_row("Workspace", "error", str(exc))

    provider = OllamaProvider(
        host=settings.llm.host,
        model=settings.llm.model,
        timeout=5.0,
    )
    try:
        health = provider.health()
    finally:
        provider.close()
    if health.reachable:
        table.add_row("Ollama", "ok", f"{settings.llm.host} ({len(health.models)} models)")
        if settings.llm.model in health.models:
            table.add_row("Model", "ok", settings.llm.model)
        else:
            table.add_row(
                "Model",
                "missing",
                f"{settings.llm.model} not pulled — run: ollama pull {settings.llm.model}",
            )
    else:
        table.add_row(
            "Ollama",
            "offline",
            f"{settings.llm.host} — start it with `ollama serve`",
        )

    try:
        import playwright  # noqa: F401

        table.add_row("Playwright", "ok", "python package installed")
    except ImportError:
        table.add_row("Playwright", "missing", "pip install playwright")

    cache = Path.home() / "AppData" / "Local" / "ms-playwright"
    chromium = sorted(cache.glob("chromium*")) if cache.exists() else []
    table.add_row(
        "Chromium",
        "ok" if chromium else "missing",
        str(chromium[0]) if chromium else "python -m playwright install chromium",
    )

    try:
        import sounddevice  # noqa: F401

        table.add_row("Microphone", "ok", "installed")
    except Exception:
        table.add_row("Microphone", "optional", "pip install sounddevice")

    from nexis.voice.stt import has_faster_whisper, has_whisper_cpp

    if has_whisper_cpp(settings.voice):
        table.add_row("Whisper STT", "ok", "whisper.cpp (whisper-cli.exe)")
    elif has_faster_whisper():
        table.add_row("Whisper STT", "ok", "faster-whisper")
    else:
        table.add_row(
            "Whisper STT",
            "optional",
            "set voice.whisper_cpp_path + voice.whisper_cpp_model, or pip install faster-whisper",
        )

    try:
        import edge_tts  # noqa: F401

        table.add_row("Edge TTS", "ok", "installed")
    except Exception:
        table.add_row("Edge TTS", "optional", "pip install edge-tts")

    console.print(table)
    if not health.reachable:
        console.print(
            "\n[yellow]Start Ollama, then run Nexis again.[/] "
            "See [bold]README.md[/] for setup steps."
        )
    elif settings.llm.model not in health.models:
        console.print(f"\n[yellow]Pull the model first:[/] ollama pull {settings.llm.model}")
    return 0 if health.reachable and settings.llm.model in health.models else 1


class NexisApp:
    def __init__(self, settings: Settings, console: Console, session_id: str = "default"):
        self.settings = settings
        self.console = console
        self.session_id = session_id
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
        self._voice = None
        self.voice_enabled = False
        self._line_open = False
        self._printed = False
        self._turn_started = 0.0

    # ---------- startup / shutdown ----------

    def _banner(self) -> None:
        body = Text()
        body.append("Nexis Order", style="bold cyan")
        body.append(f"  v{__version__}\n", style="dim")
        body.append(IDENTITY, style="italic")
        self.console.print(Panel(body, border_style="cyan"))
        facts = len(self.memory.facts())
        self.console.print(
            f"[dim]model[/] {self.settings.llm.model}   "
            f"[dim]workspace[/] {self.workspace.root}   "
            f"[dim]session[/] {self.session_id}   "
            f"[dim]facts[/] {facts}"
        )
        self.console.print(
            f"[dim]config[/] {self.settings.source}   "
            f"[dim]audit[/] {self.audit.path}   "
            "[dim]type /help for commands[/]\n"
        )

    def _ollama_help(self, detail: str) -> None:
        message = Text()
        message.append("Nexis could not reach Ollama", style="bold yellow")
        message.append(f" at {self.settings.llm.host}.\n\n")
        message.append("1. Install Ollama:  ", style="bold")
        message.append("winget install Ollama.Ollama   (or https://ollama.com)\n")
        message.append("2. Start it:        ", style="bold")
        message.append("ollama serve   (or launch the Ollama desktop app)\n")
        message.append("3. Pull a model:    ", style="bold")
        message.append(f"ollama pull {self.settings.llm.model}\n")
        message.append("4. Run Nexis again: ", style="bold")
        message.append("nexis\n")
        if detail:
            message.append(f"\nTechnical detail: {detail}", style="dim")
        self.console.print(Panel(message, title="Setup needed", border_style="yellow"))

    def _init_voice(self) -> bool:
        from nexis.voice import VoiceSession

        try:
            session = VoiceSession(self.settings.voice, self.console)
            ok, message = session.available()
        except Exception as exc:
            self.console.print(f"[yellow]Voice unavailable:[/] {exc}")
            self._voice = None
            self.voice_enabled = False
            return False
        if not ok:
            self.console.print(f"[yellow]Voice unavailable:[/] {message}")
            self._voice = None
            self.voice_enabled = False
            return False
        self._voice = session
        self.voice_enabled = True
        self.console.print(f"[green]Voice ready[/] — {message}")
        self.console.print(
            "[dim]Push to talk: press Enter on an empty line, speak, press Enter to stop.[/]"
        )
        return True

    def _shutdown(self) -> None:
        try:
            if self._voice is not None:
                self._voice.stop_speaking()
        finally:
            try:
                self.browser.close()
            except Exception:
                pass
            try:
                self.provider.close()
            except Exception:
                pass
            self.memory.close()

    # ---------- main loop ----------

    def run(self, voice: bool) -> int:
        self._banner()
        health = self.provider.health()
        if not health.reachable:
            self._ollama_help(health.detail)
            self._shutdown()
            return 2
        if health.models and self.settings.llm.model not in health.models:
            self.console.print(
                f"[yellow]Model '{self.settings.llm.model}' is not pulled yet.[/] "
                f"Run: [bold]ollama pull {self.settings.llm.model}[/] — "
                f"available: {', '.join(health.models[:8]) or 'none'}\n"
            )
        if voice or self.settings.voice.enabled:
            self._init_voice()

        try:
            while True:
                try:
                    raw = self._read_input()
                except (EOFError, KeyboardInterrupt):
                    self.console.print("\n[dim]Goodbye.[/]")
                    break
                if raw is None:
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                if raw.startswith("/"):
                    if self._command(raw):
                        self.console.print("[dim]Goodbye.[/]")
                        break
                    continue
                self._turn(raw)
        finally:
            self._shutdown()
        return 0

    def _read_input(self) -> str | None:
        if self._voice is not None:
            prompt = (
                "[bold cyan]You ›[/] [dim](Enter alone to speak, or type)[/] "
            )
        else:
            prompt = "[bold cyan]You ›[/] "
        raw = self.console.input(prompt)
        if not raw.strip() and self._voice is not None:
            return self._voice.listen()
        return raw

    def _turn(self, text: str) -> None:
        self._printed = False
        self._turn_started = time.perf_counter()
        try:
            result = self.agent.run(
                text,
                session_id=self.session_id,
                emit=self._render_event,
            )
        except AgentError as exc:
            self._ensure_newline()
            self.console.print(f"[red]Nexis error:[/] {exc}")
            return
        except Exception as exc:  # last-resort guard so the REPL survives
            self._ensure_newline()
            self.console.print(f"[red]Unexpected error:[/] {type(exc).__name__}: {exc}")
            self.audit.record("cli_error", error=f"{type(exc).__name__}: {exc}")
            return
        self._ensure_newline()
        if not self._printed and result.text:
            self.console.print(result.text, markup=False, highlight=False)
        elapsed = time.perf_counter() - self._turn_started
        self.console.print(
            f"[dim]— {elapsed:.1f}s · {result.steps} step(s) · "
            f"{len(result.tools_used)} tool call(s)[/]"
        )
        if self._voice is not None and result.text:
            self._voice.say(result.text)

    # ---------- rendering ----------

    def _ensure_newline(self) -> None:
        if self._line_open:
            self.console.print()
            self._line_open = False

    def _render_event(self, event: AgentEvent) -> None:
        if event.kind == "status":
            if not self._printed and not self._line_open:
                self.console.print(Text(f"· {event.text}", style="dim"))
            return
        if event.kind == "delta":
            if not self._line_open:
                self.console.print(Text("Nexis › ", style="bold cyan"), end="")
                self._line_open = True
            sys.stdout.write(event.text)
            sys.stdout.flush()
            self._printed = True
            return
        if event.kind == "tool_call":
            self._ensure_newline()
            summary = str(event.detail.get("summary", ""))
            risk = str(event.detail.get("risk", "safe"))
            line = Text("  ↳ ", style="dim")
            line.append(event.tool or "tool", style="bold")
            line.append(f"  {summary}", style="dim")
            if risk != "safe":
                line.append(f"  [{risk}]", style="yellow")
            self.console.print(line)
            return
        if event.kind == "tool_result":
            self._ensure_newline()
            detail = event.detail
            if detail.get("ok"):
                duration = detail.get("duration_ms")
                suffix = f" ({duration} ms)" if duration is not None else ""
                self.console.print(
                    Text(f"    ok · {event.tool}{suffix}", style="green")
                )
            elif detail.get("declined"):
                self.console.print(Text(f"    declined · {event.tool}", style="yellow"))
            else:
                error = str(detail.get("error") or "failed")
                self.console.print(
                    Text(f"    failed · {event.tool} · {error[:240]}", style="red")
                )
            return
        if event.kind == "notice":
            self._ensure_newline()
            self.console.print(Text(event.text, style="yellow"))

    # ---------- confirmations ----------

    def _confirm(self, request: ConfirmationRequest) -> bool:
        self._ensure_newline()
        body = Text()
        body.append(request.summary, style="bold")
        arguments = request.details.get("arguments") or {}
        if arguments:
            rendered = json.dumps(arguments, indent=2, ensure_ascii=False, default=str)
            if len(rendered) > 1200:
                rendered = rendered[:1200] + " …"
            body.append("\n\n")
            body.append(rendered, style="dim")
        self.console.print(
            Panel(body, title="[yellow]Nexis needs your approval[/]", border_style="yellow")
        )
        try:
            approved = Confirm.ask("Proceed with this action?", default=False, console=self.console)
        except (EOFError, KeyboardInterrupt):
            self.console.print()
            self.agent.interrupt()
            return False
        self.console.print(Text("Approved." if approved else "Declined.", style="dim"))
        return approved

    # ---------- commands ----------

    def _command(self, line: str) -> bool:
        parts = line.split()
        command = parts[0].lower()
        args = parts[1:]

        if command in {"/quit", "/exit", "/q"}:
            return True
        if command == "/help":
            table = Table(title="Commands", show_header=True, header_style="bold cyan")
            table.add_column("Command", style="bold")
            table.add_column("Description")
            for cmd, description in HELP_ROWS:
                table.add_row(cmd, description)
            self.console.print(table)
            return False
        if command == "/new":
            self.session_id = uuid.uuid4().hex[:8]
            self.memory.touch_session(self.session_id)
            self.console.print(f"[green]New session:[/] {self.session_id}")
            return False
        if command == "/sessions":
            rows = self.memory.sessions(limit=10)
            table = Table(title="Recent sessions")
            for column in ("session", "started", "last active"):
                table.add_column(column)
            for row in rows:
                table.add_row(row["session_id"], row["started_at"], row["last_active_at"])
            self.console.print(table if rows else "[dim]No sessions yet.[/]")
            return False
        if command == "/facts":
            facts = self.memory.facts()
            if not facts:
                self.console.print("[dim]Nothing saved yet. Ask me to remember something.[/]")
                return False
            table = Table(title="Saved preferences")
            table.add_column("key", style="bold")
            table.add_column("value")
            for key, value in facts.items():
                table.add_row(key, value)
            self.console.print(table)
            return False
        if command == "/forget":
            if not args:
                self.console.print("[yellow]Usage:[/] /forget <key>")
                return False
            removed = self.memory.forget_fact(" ".join(args))
            self.console.print(
                "[green]Forgotten.[/]" if removed else "[yellow]No such fact.[/]"
            )
            return False
        if command == "/model":
            if not args:
                self.console.print(f"[dim]Current model:[/] {self.provider.model}")
                health = self.provider.health()
                if health.reachable and health.models:
                    self.console.print(f"[dim]Available:[/] {', '.join(health.models)}")
                return False
            name = args[0]
            self.provider.model = name
            self.settings = dataclasses.replace(
                self.settings, llm=dataclasses.replace(self.settings.llm, model=name)
            )
            self.audit.record("model_changed", model=name)
            self.console.print(f"[green]Model set to[/] {name}")
            return False
        if command == "/workspace":
            if args and args[0].lower() == "open":
                try:
                    if sys.platform == "win32":
                        subprocess.Popen(["explorer", str(self.workspace.root)])
                    else:
                        subprocess.Popen(["xdg-open", str(self.workspace.root)])
                except OSError as exc:
                    self.console.print(f"[red]Could not open:[/] {exc}")
                return False
            self.console.print(str(self.workspace.root))
            return False
        if command == "/audit":
            count = 10
            if args:
                try:
                    count = max(1, min(int(args[0]), 200))
                except ValueError:
                    self.console.print("[yellow]Usage:[/] /audit [n]")
                    return False
            entries = self.audit.tail(count)
            if not entries:
                self.console.print("[dim]Audit log is empty.[/]")
                return False
            table = Table(title=f"Last {len(entries)} audit entries", show_lines=False)
            table.add_column("time", style="dim")
            table.add_column("event", style="bold")
            table.add_column("tool")
            table.add_column("detail", style="dim")
            for entry in entries:
                detail = entry.get("summary") or entry.get("error") or entry.get("model") or ""
                table.add_row(
                    str(entry.get("ts", ""))[11:19],
                    str(entry.get("event", "")),
                    str(entry.get("tool", "")),
                    str(detail)[:80],
                )
            self.console.print(table)
            self.console.print(f"[dim]Full log:[/] {self.audit.path}")
            return False
        if command == "/mic":
            if self._voice is None and not self._init_voice():
                return False
            self._voice.test_mic()
            return False
        if command == "/voice":
            mode = args[0].lower() if args else ("off" if self._voice else "on")
            if mode == "on":
                if self._voice is None:
                    self._init_voice()
                else:
                    self.console.print("[dim]Voice is already on.[/]")
            elif mode == "off":
                if self._voice is not None:
                    self._voice.stop_speaking()
                self._voice = None
                self.voice_enabled = False
                self.console.print("[dim]Voice mode off.[/]")
            else:
                self.console.print("[yellow]Usage:[/] /voice [on|off]")
            return False
        if command == "/check":
            check_environment(self.settings, self.console)
            return False
        if command == "/stop":
            self.agent.interrupt()
            self.console.print("[dim]Stop requested.[/]")
            return False

        self.console.print(f"[yellow]Unknown command:[/] {command} — try /help")
        return False


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console()
    settings = _apply_overrides(load_settings(args.config), args)
    if args.check:
        return check_environment(settings, console)
    app = NexisApp(settings, console, session_id=args.session)
    voice = bool(args.voice) or (settings.voice.enabled and not args.no_voice)
    return app.run(voice)


if __name__ == "__main__":
    raise SystemExit(main())
