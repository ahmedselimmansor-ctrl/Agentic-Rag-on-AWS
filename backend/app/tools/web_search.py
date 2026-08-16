"""Web search tool — Tavily or Serper, selected by config."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from app.config import settings
from app.services.http import UpstreamError, get_client, post_json

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class WebResult:
    title: str
    url: str
    snippet: str
    content: str = ""
    score: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class WebSearchUnavailable(RuntimeError):
    pass


async def search(query: str, max_results: int | None = None) -> list[WebResult]:
    n = max_results or settings.web_search_max_results
    provider = settings.web_search_provider

    if provider == "none":
        raise WebSearchUnavailable("Web search is disabled for this deployment.")
    if provider == "tavily":
        return await _tavily(query, n)
    if provider == "serper":
        return await _serper(query, n)
    raise WebSearchUnavailable(f"Unknown web search provider: {provider}")


async def _tavily(query: str, n: int) -> list[WebResult]:
    if not settings.tavily_api_key:
        raise WebSearchUnavailable("TAVILY_API_KEY is not set.")
    data = await post_json(
        "https://api.tavily.com/search",
        service="tavily",
        headers={"Content-Type": "application/json"},
        json_body={
            "api_key": settings.tavily_api_key,
            "query": query,
            "max_results": n,
            "search_depth": "advanced",
            "include_answer": False,
            "include_raw_content": False,
        },
    )
    return [
        WebResult(
            title=r.get("title", ""),
            url=r.get("url", ""),
            snippet=(r.get("content") or "")[:1200],
            score=r.get("score"),
        )
        for r in (data.get("results") or [])
    ][:n]


async def _serper(query: str, n: int) -> list[WebResult]:
    if not settings.serper_api_key:
        raise WebSearchUnavailable("SERPER_API_KEY is not set.")
    data = await post_json(
        "https://google.serper.dev/search",
        service="serper",
        headers={"X-API-KEY": settings.serper_api_key, "Content-Type": "application/json"},
        json_body={"q": query, "num": n},
    )
    results = [
        WebResult(
            title=r.get("title", ""),
            url=r.get("link", ""),
            snippet=(r.get("snippet") or "")[:1200],
        )
        for r in (data.get("organic") or [])
    ]
    # The answer box, when present, is usually the best single result.
    if box := data.get("answerBox"):
        results.insert(
            0,
            WebResult(
                title=box.get("title", "Answer"),
                url=box.get("link", ""),
                snippet=(box.get("answer") or box.get("snippet") or "")[:1200],
            ),
        )
    return results[:n]


async def fetch_page_text(url: str, max_chars: int = 6000) -> str:
    """Pull readable text from a result the model wants to read in full."""
    try:
        resp = await get_client().get(
            url, follow_redirects=True, headers={"User-Agent": "AgenticRAG/1.0"}
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError("fetch_page", None, str(exc)) from exc

    html = resp.text
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
    except ImportError:
        import re

        text = re.sub(r"<[^>]+>", " ", html)

    return " ".join(text.split())[:max_chars]
