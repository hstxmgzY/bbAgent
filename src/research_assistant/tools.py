"""Search and web-read tools with failure handling."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from research_assistant.models import SearchResult, ToolResult


class SearchTool:
    def __init__(self, timeout: float = 12.0):
        self.timeout = timeout

    async def search(self, query: str, limit: int = 5) -> ToolResult:
        try:
            if os.getenv("BRAVE_API_KEY"):
                results = await self._brave(query, limit)
            else:
                results = await self._duckduckgo_html(query, limit)
            return ToolResult(ok=True, data=_dedupe_results(results)[:limit])
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _brave(self, query: str, limit: int) -> list[SearchResult]:
        headers = {"X-Subscription-Token": os.environ["BRAVE_API_KEY"]}
        params = {"q": query, "count": limit}
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
        items = response.json().get("web", {}).get("results", [])
        return [
            SearchResult(
                title=item.get("title", "Untitled"),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
            )
            for item in items
            if item.get("url")
        ]

    async def _duckduckgo_html(self, query: str, limit: int) -> list[SearchResult]:
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {"User-Agent": "stage2-research-assistant/0.1"}
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        results: list[SearchResult] = []
        for item in soup.select(".result"):
            link = item.select_one(".result__a")
            snippet = item.select_one(".result__snippet")
            if not link or not link.get("href"):
                continue
            results.append(
                SearchResult(
                    title=link.get_text(" ", strip=True),
                    url=_normalize_search_url(str(link["href"])),
                    snippet=snippet.get_text(" ", strip=True) if snippet else "",
                )
            )
            if len(results) >= limit:
                break
        return results


class WebReadTool:
    ALLOWED_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml+xml")

    def __init__(
        self,
        timeout: float = 15.0,
        max_response_bytes: int = 2_000_000,
        max_redirects: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.transport = transport

    async def read(self, url: str) -> ToolResult:
        try:
            page_url, page_text = await self._fetch(url)
            soup = BeautifulSoup(page_text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            title = soup.title.get_text(" ", strip=True) if soup.title else page_url
            blocks = [
                block.get_text(" ", strip=True)
                for block in soup.find_all(["h1", "h2", "h3", "p", "li"])
            ]
            text = "\n".join(block for block in blocks if len(block) > 40)
            if not text:
                return ToolResult(ok=False, error="empty readable content")
            return ToolResult(
                ok=True, data={"title": title, "url": page_url, "text": text[:30000]}
            )
        except Exception as exc:
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _fetch(self, url: str) -> tuple[str, str]:
        headers = {"User-Agent": "stage2-research-assistant/0.1"}
        current_url = url
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            for redirect_count in range(self.max_redirects + 1):
                await _validate_public_url(current_url)
                async with client.stream(
                    "GET", current_url, headers=headers
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError(
                                "redirect response did not include a location"
                            )
                        if redirect_count >= self.max_redirects:
                            raise ValueError("too many redirects")
                        current_url = urljoin(str(response.url), location)
                        continue

                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(
                        content_type.startswith(allowed)
                        for allowed in self.ALLOWED_CONTENT_TYPES
                    ):
                        raise ValueError(
                            f"unsupported content type: {content_type or 'unknown'}"
                        )

                    declared_size = response.headers.get("content-length")
                    if declared_size and int(declared_size) > self.max_response_bytes:
                        raise ValueError("response body exceeds size limit")

                    content = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        content.extend(chunk)
                        if len(content) > self.max_response_bytes:
                            raise ValueError("response body exceeds size limit")
                    encoding = response.encoding or "utf-8"
                    return str(response.url), content.decode(encoding, errors="replace")
        raise ValueError("unable to fetch URL")


async def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http and https URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("URL must contain a valid host without credentials")

    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        records = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, port, type=socket.SOCK_STREAM
        )
        addresses = list({ipaddress.ip_address(record[4][0]) for record in records})

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("URL resolves to a non-public network address")


def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    deduped: list[SearchResult] = []
    for result in results:
        if result.url in seen:
            continue
        seen.add(result.url)
        deduped.append(result)
    return deduped


def _normalize_search_url(href: str) -> str:
    absolute = urljoin("https://duckduckgo.com", href)
    parsed = urlsplit(absolute)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return absolute
