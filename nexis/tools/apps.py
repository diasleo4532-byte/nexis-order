from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from typing import Any

from nexis.tools.registry import ToolRegistry
from nexis.workspace import Workspace

APP_ALIASES = {
    "browser": "__default_browser__",
    "default browser": "__default_browser__",
    "web browser": "__default_browser__",
    "chrome": "chrome",
    "edge": "msedge",
    "firefox": "firefox",
    "spotify": "spotify:",
    "notepad": "notepad",
    "calculator": "calc",
    "calc": "calc",
    "files": "explorer",
    "file manager": "explorer",
    "explorer": "explorer",
    "terminal": "wt",
    "powershell": "powershell",
    "cmd": "cmd",
    "command prompt": "cmd",
    "settings": "ms-settings:",
    "store": "ms-windows-store:",
    "vscode": "code",
    "vs code": "code",
    "code": "code",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "outlook": "outlook",
}

MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "prev": 0xB1,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
}


def _launch(target: str) -> None:
    if target == "__default_browser__":
        if not webbrowser.open("https://www.google.com"):
            raise RuntimeError("Could not open the default browser")
        return

    if sys.platform == "win32":
        if target.endswith(":"):
            os.startfile(target)  # type: ignore[attr-defined]
            return
        last_error: OSError | None = None
        for name in (target, f"{target}.exe"):
            try:
                os.startfile(name)  # type: ignore[attr-defined]
                return
            except OSError as exc:
                last_error = exc
        executable = shutil.which(target)
        if executable:
            subprocess.Popen([executable], close_fds=True)
            return
        raise RuntimeError(f"Could not launch '{target}'. Is it installed? ({last_error})")

    executable = shutil.which(target)
    if not executable:
        raise RuntimeError(f"Could not find '{target}' on this system")
    subprocess.Popen([executable], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _send_media_key(action: str, times: int) -> dict[str, Any]:
    key = action.strip().lower().replace("-", "_").replace(" ", "_")
    if key not in MEDIA_KEYS:
        raise ValueError(
            f"Unknown media action '{action}'. Use one of: {', '.join(sorted(MEDIA_KEYS))}"
        )
    if sys.platform != "win32":
        raise RuntimeError("Global media keys are only supported on Windows")
    vk = MEDIA_KEYS[key]
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    repeats = max(1, min(int(times), 20))
    for _ in range(repeats):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 0x0002, 0)
        time.sleep(0.05)
    return {"action": key, "times": repeats}


def register(registry: ToolRegistry, workspace: Workspace) -> None:
    def open_app(name: str) -> dict[str, Any]:
        _launch(name)
        return {"launched": name}

    def open_file(path: str) -> dict[str, Any]:
        target = workspace.resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"No such file in workspace: {path}")
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]
        else:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(target)])
        return {"opened": workspace.relative(target)}

    def open_url(url: str) -> dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        if not webbrowser.open(url):
            raise RuntimeError("Could not open the default browser")
        return {"opened": url}

    def media_control(action: str, times: int = 1) -> dict[str, Any]:
        return _send_media_key(action, times)

    registry.add(
        "open_app",
        "Open a desktop application by name (e.g. chrome, spotify, notepad, calculator, "
        "files, terminal, settings, vscode, word, excel) or an executable name.",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Application name"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        open_app,
    )
    registry.add(
        "open_file",
        "Open a file from the Nexis Workspace with its default application.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path in workspace"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        open_file,
    )
    registry.add(
        "open_url",
        "Open a URL in the user's default web browser. For interactive browsing use browser_open.",
        {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to open"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        open_url,
    )
    registry.add(
        "media_control",
        "Control whatever media is currently playing through Windows media keys "
        "(YouTube in a browser, Spotify, media players).",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play_pause", "next", "previous", "volume_up", "volume_down", "mute"],
                },
                "times": {"type": "integer", "description": "Repeat count, e.g. 5 for volume"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        media_control,
    )
