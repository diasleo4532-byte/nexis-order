from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from typing import Any
from urllib.parse import quote, quote_plus

from nexis.tools.browser import WorkspaceBrowser
from nexis.tools.registry import ToolRegistry
from nexis.workspace import Workspace


def _youtube_fallback(query: str, reason: str) -> dict[str, Any]:
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    webbrowser.open(search_url)
    return {
        "playing": None,
        "fallback": "opened_search_in_default_browser",
        "url": search_url,
        "note": f"Opened the YouTube search in the default browser; the user needs to pick a "
        f"result. Automatic playback was unavailable: {reason}",
    }


def play_youtube(browser: WorkspaceBrowser, query: str) -> dict[str, Any]:
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    try:
        page = browser.page
        page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        for label in ("Accept all", "Reject all", "I agree"):
            try:
                page.get_by_role("button", name=label).first.click(timeout=1200)
                break
            except Exception:
                continue
        page.wait_for_selector("a#video-title", timeout=15000)
        first = page.locator("a#video-title").first
        title = first.get_attribute("title") or first.inner_text()
        first.click(timeout=10000)
        page.wait_for_timeout(2500)
        return {"playing": title, "url": page.url, "autoplay": True}
    except RuntimeError as exc:
        return _youtube_fallback(query, str(exc).splitlines()[0])
    except Exception as exc:
        return _youtube_fallback(query, f"{type(exc).__name__}: {str(exc).splitlines()[0]}")


def play_spotify(query: str | None) -> dict[str, Any]:
    if sys.platform != "win32":
        raise RuntimeError("Spotify URI launching is only wired up on Windows")
    target = f"spotify:search:{quote(query)}" if query else "spotify:"
    try:
        os.startfile(target)  # type: ignore[attr-defined]
    except OSError as exc:
        raise RuntimeError(
            "Spotify does not seem to be installed or registered. Install the Spotify "
            "desktop app, or ask me to play it on YouTube instead."
        ) from exc
    note = (
        "Spotify is showing search results. Use media_control(play_pause) to start "
        "playback, and media_control(next/previous) to change tracks."
        if query
        else "Opened Spotify."
    )
    return {"opened": target, "note": note}


def play_local_media(workspace: Workspace, path: str) -> dict[str, Any]:
    target = workspace.resolve(path)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"No such media file in workspace: {path}")
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(target)])
    return {"playing": workspace.relative(target)}


def register(registry: ToolRegistry, browser: WorkspaceBrowser, workspace: Workspace) -> None:
    def youtube(query: str) -> dict[str, Any]:
        return play_youtube(browser, query)

    def spotify(query: str | None = None) -> dict[str, Any]:
        return play_spotify(query)

    def local(path: str) -> dict[str, Any]:
        return play_local_media(workspace, path)

    registry.add(
        "play_youtube",
        "Search YouTube and start playing the first matching video right away. Use this for "
        "'play <song/artist/video>' requests unless the user explicitly asks for Spotify.",
        {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Song, artist or video"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        youtube,
    )
    registry.add(
        "play_spotify",
        "Open Spotify search for a song, artist or playlist (desktop app required). Playback "
        "itself is controlled with media_control.",
        {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "additionalProperties": False,
        },
        spotify,
    )
    registry.add(
        "play_local_media",
        "Play an audio or video file stored in the Nexis Workspace with the default player.",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Relative path in workspace"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        local,
    )
