from __future__ import annotations

import base64
import html
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from nexis.tools.registry import ToolRegistry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_FETCH_CHARS = 8000


def _clean(text: str) -> str:
    text = " ".join(text.split())
    return re.sub(r"\s*Read more$", "", text).strip()


class _BingParser(HTMLParser):
    """Pull title/URL/snippet triples out of Bing's b_algo result blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._li_depth = 0
        self._algo_depth: int | None = None
        self._in_h2 = 0
        self._title_parts: list[str] | None = None
        self._title_text = ""
        self._snippet_parts: list[str] | None = None
        self._snippet_text = ""
        self._snippet_done = False
        self._href = ""

    def _reset(self) -> None:
        self._algo_depth = None
        self._in_h2 = 0
        self._title_parts = None
        self._title_text = ""
        self._snippet_parts = None
        self._snippet_text = ""
        self._snippet_done = False
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "li":
            self._li_depth += 1
            if self._algo_depth is None and "b_algo" in attributes.get("class", ""):
                self._reset()
                self._algo_depth = self._li_depth
            return
        if self._algo_depth is None:
            return
        if tag == "h2":
            self._in_h2 += 1
        elif tag == "a" and self._in_h2 and not self._href:
            self._href = attributes.get("href", "")
            self._title_parts = []
        elif tag == "p" and not self._snippet_done:
            classes = attributes.get("class", "")
            if "b_lineclamp" in classes or "b_algoSlug" in classes:
                self._snippet_parts = []
                self._snippet_done = True

    def handle_data(self, data: str) -> None:
        if self._algo_depth is None:
            return
        if self._title_parts is not None and self._in_h2:
            self._title_parts.append(data)
        elif self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            if self._algo_depth is not None and self._li_depth == self._algo_depth:
                self._finish()
            self._li_depth = max(0, self._li_depth - 1)
            return
        if self._algo_depth is None:
            return
        if tag == "h2":
            self._in_h2 = max(0, self._in_h2 - 1)
            if self._title_parts is not None:
                self._title_text = _clean("".join(self._title_parts))
                self._title_parts = None
        elif tag == "p" and self._snippet_parts is not None:
            self._snippet_text = _clean("".join(self._snippet_parts))
            self._snippet_parts = None

    def _finish(self) -> None:
        title = self._title_text or _clean("".join(self._title_parts or []))
        if title and self._href:
            self.results.append(
                {
                    "title": title,
                    "url": _decode_bing_url(self._href),
                    "snippet": self._snippet_text,
                }
            )
        self._reset()


def _decode_bing_url(href: str) -> str:
    parsed = urlparse(href)
    if parsed.netloc.endswith("bing.com") and parsed.path.startswith("/ck/"):
        target = parse_qs(parsed.query).get("u")
        if target and target[0].startswith("a1"):
            payload = target[0][2:]
            payload += "=" * (-len(payload) % 4)
            try:
                return base64.urlsafe_b64decode(payload).decode("utf-8", "replace")
            except (ValueError, UnicodeDecodeError):
                return href
    return href


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.snippets: list[str] = []
        self._capture: str | None = None
        self._end_tag: str | None = None
        self._buffer: list[str] = []
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "")
        if tag == "a" and ("result__a" in classes or "result-link" in classes):
            self._capture = "link"
            self._end_tag = tag
            self._buffer = []
            self._href = attributes.get("href", "")
        elif self._capture is None and (
            "result__snippet" in classes or "result-snippet" in classes
        ):
            self._capture = "snippet"
            self._end_tag = tag
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag == self._end_tag:
            text = " ".join("".join(self._buffer).split())
            if self._capture == "link" and text:
                self.links.append((text, _clean_ddg_url(self._href)))
            elif self._capture == "snippet" and text:
                self.snippets.append(text)
            self._capture = None
            self._end_tag = None
            self._buffer = []


def _clean_ddg_url(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip_depth:
            stripped = data.strip()
            if stripped:
                self.parts.append(stripped)


def _extract_text(markup: str) -> tuple[str, str]:
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed HTML should never crash a tool
        pass
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(parser.parts))
    text = re.sub(r"[ \t]{2,}", " ", text)
    return html.unescape(parser.title.strip()), text.strip()


def _search_bing(query: str, max_results: int, timeout: float) -> list[dict[str, str]]:
    response = httpx.get(
        "https://www.bing.com/search",
        params={"q": query},
        headers=HEADERS,
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
    )
    response.raise_for_status()
    parser = _BingParser()
    parser.feed(response.text)
    parser.close()
    return [result for result in parser.results if result["title"]][:max_results]


def _search_ddg(query: str, max_results: int, timeout: float) -> list[dict[str, str]]:
    response = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers=HEADERS,
        timeout=timeout,
        follow_redirects=True,
        trust_env=False,
    )
    response.raise_for_status()
    parser = _DuckDuckGoParser()
    parser.feed(response.text)
    parser.close()
    results: list[dict[str, str]] = []
    for index, (title, url) in enumerate(parser.links[:max_results]):
        snippet = parser.snippets[index] if index < len(parser.snippets) else ""
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


def search_web(query: str, max_results: int = 5, timeout: float = 20.0) -> list[dict[str, str]]:
    errors: list[str] = []
    for name, backend in (("bing", _search_bing), ("duckduckgo", _search_ddg)):
        try:
            results = backend(query, max_results, timeout)
        except httpx.HTTPError as exc:
            errors.append(f"{name}: {exc}")
            continue
        if results:
            return results
        errors.append(f"{name}: no parsed results (possibly blocked)")
    raise RuntimeError(
        "Web search returned no results. " + " | ".join(errors)
    )


def fetch_page(url: str, timeout: float = 20.0) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    response = httpx.get(
        url, headers=HEADERS, timeout=timeout, follow_redirects=True, trust_env=False
    )
    response.raise_for_status()
    title, text = _extract_text(response.text)
    truncated = len(text) > MAX_FETCH_CHARS
    return {
        "url": str(response.url),
        "title": title,
        "text": text[:MAX_FETCH_CHARS],
        "truncated": truncated,
    }


def register(registry: ToolRegistry) -> None:
    def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
        try:
            results = search_web(query, max_results=max(1, min(int(max_results), 10)))
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Web search failed: {exc}") from exc
        return {"query": query, "results": results}

    def fetch_url(url: str) -> dict[str, Any]:
        try:
            return fetch_page(url)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Could not fetch {url}: {exc}") from exc

    registry.add(
        "web_search",
        "Search the web and return titles, URLs and snippets. Uses Bing, falling back to "
        "DuckDuckGo. Use fetch_url to read a promising result in full.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "1-10, default 5"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        web_search,
    )
    registry.add(
        "fetch_url",
        "Download a web page and return its visible text (first 8000 characters) with the title.",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        fetch_url,
    )
