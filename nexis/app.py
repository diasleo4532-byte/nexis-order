from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from nexis.config import APP_DIR, load_settings
from nexis.web.server import create_app
from nexis.web.service import NexisService

URL_FILE = APP_DIR / "app-url.json"
LOG_FILE = APP_DIR / "app.log"
WINDOW_PROFILE = APP_DIR / "app-window"

# pythonw has no console streams, and uvicorn calls sys.stdout.isatty() at startup.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _setup_logging() -> logging.Logger:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("nexis.app")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        if sys.stderr is not None:
            stream = logging.StreamHandler()
            stream.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(stream)
    return logger


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _existing_url() -> str | None:
    try:
        data = json.loads(URL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = str(data.get("url") or "")
    if not url.startswith("http://127.0.0.1:"):
        return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url + "api/state", timeout=0.6) as response:
            if response.status == 200:
                return url
    except OSError:
        return None
    return None


def _browser_candidates() -> list[Path]:
    roots = [
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMFILES"),
        os.environ.get("LOCALAPPDATA"),
    ]
    rels = [
        ("Microsoft", "Edge", "Application", "msedge.exe"),
        ("Google", "Chrome", "Application", "chrome.exe"),
        ("Chromium", "Application", "chrome.exe"),
        ("BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
    ]
    candidates: list[Path] = []
    for root in roots:
        if not root:
            continue
        for rel in rels:
            candidates.append(Path(root).joinpath(*rel))
    for name in ("msedge.exe", "chrome.exe", "brave.exe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    return candidates


def _open_window(url: str, logger: logging.Logger) -> subprocess.Popen | None:
    WINDOW_PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        f"--app={url}",
        "--window-size=1240,860",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={WINDOW_PROFILE}",
        "--disable-features=Translate",
    ]
    for exe in _browser_candidates():
        if not exe.is_file():
            continue
        try:
            proc = subprocess.Popen(
                [str(exe), *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            logger.info("Opened app window via %s", exe)
            return proc
        except OSError:
            continue
    logger.warning("Edge/Chromium not found; opening the default browser instead.")
    webbrowser.open(url)
    return None


def main() -> int:
    logger = _setup_logging()
    try:
        settings = load_settings()
    except SystemExit as exc:
        logger.error("%s", exc)
        return 2

    existing = _existing_url()
    if existing:
        logger.info("Nexis is already running at %s", existing)
        if not os.environ.get("NEXIS_NO_WINDOW"):
            _open_window(existing, logger)
        print(f"Nexis is already running at {existing}")
        return 0

    service = NexisService(settings)
    try:
        import uvicorn
    except ImportError:
        logger.error("The app dependencies are missing. Run: .venv\\Scripts\\python -m pip install -e .[app]")
        service.close()
        return 2

    port = _free_port()
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            ws="none",
        )
    )
    thread = threading.Thread(target=server.run, name="nexis-server", daemon=True)
    thread.start()

    deadline = time.monotonic() + 20
    while not server.started:
        if not thread.is_alive():
            logger.error("The Nexis server stopped during startup. See %s", LOG_FILE)
            service.close()
            return 1
        if time.monotonic() > deadline:
            logger.error("The Nexis server did not start in time. See %s", LOG_FILE)
            server.should_exit = True
            service.close()
            return 1
        time.sleep(0.05)

    URL_FILE.write_text(json.dumps({"url": url}), encoding="utf-8")
    logger.info("Nexis listening at %s", url)
    print(f"Nexis is running at {url}")
    print("Close the Nexis window to quit (or press Ctrl+C here).")

    proc = None if os.environ.get("NEXIS_NO_WINDOW") else _open_window(url, logger)
    try:
        if proc is not None:
            started = time.monotonic()
            proc.wait()
            if time.monotonic() - started < 3:
                logger.info("Browser handed off to a background process; server stays up.")
                while True:
                    time.sleep(1)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl+C).")
    finally:
        server.should_exit = True
        thread.join(timeout=6)
        try:
            URL_FILE.unlink()
        except OSError:
            pass
        service.close()
        logger.info("Nexis stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
