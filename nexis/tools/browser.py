from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

from nexis.tools.registry import ToolRegistry
from nexis.workspace import Workspace

BROWSER_INSTALL_HINT = (
    "Browser control needs Playwright. Install it once with:\n"
    "  pip install playwright\n"
    "  python -m playwright install chromium"
)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("URL must not be empty")
    if "://" not in url and not url.startswith(("about:", "chrome:")):
        url = "https://" + url
    return url


class BrowserSession:
    """A single visible Chromium window shared by every browser tool."""

    def __init__(self, user_data_dir: Path, headless: bool | None = None):
        self.user_data_dir = Path(user_data_dir)
        self.headless = (
            headless
            if headless is not None
            else os.environ.get("NEXIS_HEADLESS", "").lower() in {"1", "true", "yes"}
        )
        self._pw = None
        self._context = None
        self._page = None

    def _ensure(self) -> Any:
        if self._page is not None and not self._page.is_closed():
            return self._page
        if self._pw is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise RuntimeError(BROWSER_INSTALL_HINT) from exc
            self._pw = sync_playwright().start()
        try:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self._context = self._pw.chromium.launch_persistent_context(
                str(self.user_data_dir),
                headless=self.headless,
                no_viewport=True,
                args=["--start-maximized"],
            )
        except Exception as exc:
            self._shutdown_playwright()
            raise RuntimeError(
                f"Could not start Chromium: {exc}\n{BROWSER_INSTALL_HINT}"
            ) from exc
        pages = self._context.pages
        self._page = pages[0] if pages else self._context.new_page()
        return self._page

    @property
    def page(self) -> Any:
        return self._ensure()

    def _shutdown_playwright(self) -> None:
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = None

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        self._page = None
        self._shutdown_playwright()

    def open(self, url: str, wait_ms: int = 800) -> dict[str, Any]:
        page = self.page
        page.goto(_normalize_url(url), wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait_ms)
        return self._describe(page)

    def _describe(self, page: Any) -> dict[str, Any]:
        try:
            title = page.title()
        except Exception:
            title = ""
        return {"url": page.url, "title": title}

    def read(self, max_chars: int = 6000) -> dict[str, Any]:
        page = self.page
        try:
            body = page.inner_text("body")
        except Exception:
            body = page.content()
        text = " ".join(body.split()) if len(body) < 400 else body.strip()
        truncated = len(text) > max_chars
        return {**self._describe(page), "text": text[:max_chars], "truncated": truncated}

    def click(self, selector: str | None = None, text: str | None = None) -> dict[str, Any]:
        page = self.page
        if not selector and not text:
            raise ValueError("Provide either a CSS selector or link/button text")
        from playwright.sync_api import TimeoutError as PWTimeout

        clicked = False
        if selector:
            page.locator(selector).first.click(timeout=10000)
            clicked = True
        else:
            assert text is not None
            attempts = [
                lambda: page.get_by_text(text, exact=False).first.click(timeout=6000),
                lambda: page.get_by_role("link", name=text, exact=False).first.click(timeout=5000),
                lambda: page.get_by_role("button", name=text, exact=False).first.click(timeout=5000),
            ]
            errors: list[str] = []
            for attempt in attempts:
                try:
                    attempt()
                    clicked = True
                    break
                except PWTimeout as exc:
                    errors.append(str(exc).splitlines()[0])
                except Exception as exc:
                    errors.append(str(exc).splitlines()[0])
            if not clicked:
                raise RuntimeError(f"Could not find anything to click for '{text}': {errors[:2]}")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PWTimeout:
            pass
        page.wait_for_timeout(600)
        return self._describe(page)

    def type_text(
        self, text: str, selector: str | None = None, submit: bool = False
    ) -> dict[str, Any]:
        page = self.page
        if selector:
            page.locator(selector).first.fill(text, timeout=10000)
        else:
            page.keyboard.type(text, delay=20)
        if submit:
            page.keyboard.press("Enter")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            page.wait_for_timeout(600)
        return self._describe(page)

    def press(self, key: str) -> dict[str, Any]:
        page = self.page
        page.keyboard.press(key)
        page.wait_for_timeout(300)
        return self._describe(page)

    def back(self) -> dict[str, Any]:
        page = self.page
        page.go_back(timeout=15000)
        page.wait_for_timeout(400)
        return self._describe(page)

    def screenshot(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(target), full_page=True)


class WorkspaceBrowser(BrowserSession):
    def __init__(self, workspace: Workspace, user_data_dir: Path, headless: bool | None = None):
        super().__init__(user_data_dir=user_data_dir, headless=headless)
        self.workspace = workspace


def _upload_image(page: Any, target: Path) -> bool:
    for selector in ("input[type=file]", "input[accept*='image']"):
        try:
            page.locator(selector).first.set_input_files(str(target), timeout=8000)
            return True
        except Exception:
            continue
    return False


def _yandex_reverse_image(page: Any, target: Path) -> dict[str, Any]:
    page.goto("https://yandex.com/images/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    if not _upload_image(page, target):
        raise RuntimeError("Yandex did not show an image upload control")
    page.wait_for_timeout(7000)
    url = page.url
    if "rpt=imageview" not in url:
        raise RuntimeError(f"Yandex did not open a results page (url: {url[:120]})")
    text = " ".join(page.inner_text("body").split())
    lowered = text.lower()
    if "captcha" in lowered and ("not a robot" in lowered or "confirmation" in lowered):
        raise RuntimeError("Yandex asked for a captcha")
    return {"engine": "yandex", "url": url, "summary": text[:4000]}


def _google_lens_reverse_image(page: Any, target: Path) -> dict[str, Any]:
    page.goto("https://lens.google.com/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)
    if not _upload_image(page, target):
        raise RuntimeError("Google Lens did not show an image upload control")
    page.wait_for_timeout(6000)
    text = " ".join(page.inner_text("body").split())
    if "/sorry/" in page.url or "unusual traffic" in text.lower():
        raise RuntimeError("Google blocked the request (unusual traffic / captcha)")
    return {"engine": "google_lens", "url": page.url, "summary": text[:4000]}


def register(registry: ToolRegistry, browser: WorkspaceBrowser) -> None:
    def browser_open(url: str) -> dict[str, Any]:
        return browser.open(url)

    def browser_read(max_chars: int = 6000) -> dict[str, Any]:
        return browser.read(max_chars=max(500, min(int(max_chars), 20000)))

    def browser_click(selector: str | None = None, text: str | None = None) -> dict[str, Any]:
        return browser.click(selector=selector, text=text)

    def browser_type(
        text: str, selector: str | None = None, submit: bool = False
    ) -> dict[str, Any]:
        return browser.type_text(text, selector=selector, submit=submit)

    def browser_press(key: str) -> dict[str, Any]:
        return browser.press(key)

    def browser_back() -> dict[str, Any]:
        return browser.back()

    def browser_screenshot(filename: str | None = None) -> dict[str, Any]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = filename or f"screenshots/screen-{stamp}.png"
        if not name.lower().endswith(".png"):
            name += ".png"
        target = browser.workspace.resolve(name)
        browser.screenshot(target)
        return {"saved": browser.workspace.relative(target)}

    def reverse_image_search(image_path: str, engine: str = "auto") -> dict[str, Any]:
        target = browser.workspace.resolve(image_path)
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"No such image in workspace: {image_path}")
        if target.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError(f"Unsupported image type: {target.suffix}")
        engines = (
            ["yandex", "google_lens"]
            if engine == "auto"
            else [engine]
        )
        errors: list[str] = []
        for name in engines:
            try:
                if name == "yandex":
                    result = _yandex_reverse_image(browser.page, target)
                elif name == "google_lens":
                    result = _google_lens_reverse_image(browser.page, target)
                else:
                    raise ValueError(f"Unknown reverse image engine: {name}")
                result["image"] = browser.workspace.relative(target)
                return result
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        raise RuntimeError(
            "Reverse image search failed. " + " | ".join(errors)
        )

    registry.add(
        "browser_open",
        "Open a URL in Nexis's visible Chromium window and return the page title and URL. "
        "Use this for interactive browsing, not for a quick quick search.",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        browser_open,
    )
    registry.add(
        "browser_read",
        "Read the visible text of the current browser page (front 6000 characters by default).",
        {
            "type": "object",
            "properties": {"max_chars": {"type": "integer"}},
            "additionalProperties": False,
        },
        browser_read,
    )
    registry.add(
        "browser_click",
        "Click an element on the current page, either by CSS selector or by visible link/button text.",
        {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector (preferred if known)"},
                "text": {"type": "string", "description": "Visible text of a link or button"},
            },
            "additionalProperties": False,
        },
        browser_click,
    )
    registry.add(
        "browser_type",
        "Type text into the page: into a field matched by CSS selector, or into whatever is focused. "
        "Set submit=true to press Enter afterwards. Confirm with the user before submitting forms "
        "that send data, place orders, or post content.",
        {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "selector": {"type": "string"},
                "submit": {"type": "boolean"},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
        browser_type,
    )
    registry.add(
        "browser_press",
        "Press a keyboard key on the current page, e.g. Enter, Escape, PageDown, Meta+L.",
        {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
        browser_press,
    )
    registry.add(
        "browser_back",
        "Go back one page in the Nexis browser history.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        browser_back,
    )
    registry.add(
        "browser_screenshot",
        "Take a full-page screenshot and save it inside the Nexis Workspace.",
        {
            "type": "object",
            "properties": {"filename": {"type": "string"}},
            "additionalProperties": False,
        },
        browser_screenshot,
    )
    registry.add(
        "reverse_image_search",
        "Run a reverse image search on an image inside the Nexis Workspace (Yandex first, "
        "Google Lens as fallback) and return the results page summary.",
        {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Relative path inside workspace",
                },
                "engine": {
                    "type": "string",
                    "enum": ["auto", "yandex", "google_lens"],
                    "description": "Default auto",
                },
            },
            "required": ["image_path"],
            "additionalProperties": False,
        },
        reverse_image_search,
    )
