"""Shared async HTTP client with bounded retries on transient failures."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout_seconds, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


class UpstreamError(RuntimeError):
    def __init__(self, service: str, status: int | None, detail: str) -> None:
        self.service = service
        self.status = status
        super().__init__(f"{service} failed (status={status}): {detail[:500]}")


async def post_json(
    url: str,
    *,
    service: str,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    max_retries: int | None = None,
) -> dict[str, Any]:
    """POST JSON, retrying transient errors with jittered exponential backoff."""
    attempts = max_retries if max_retries is not None else settings.http_max_retries
    client = get_client()
    last: Exception | None = None

    for attempt in range(attempts):
        try:
            resp = await client.post(url, headers=headers, json=json_body)
            if resp.status_code in RETRY_STATUS and attempt < attempts - 1:
                last = UpstreamError(service, resp.status_code, resp.text)
                await _sleep_backoff(attempt, resp)
                continue
            if resp.status_code >= 400:
                raise UpstreamError(service, resp.status_code, resp.text)
            return resp.json()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = exc
            if attempt < attempts - 1:
                await _sleep_backoff(attempt, None)
                continue
            raise UpstreamError(service, None, str(exc)) from exc

    raise UpstreamError(service, None, str(last))


async def _sleep_backoff(attempt: int, resp: httpx.Response | None) -> None:
    # Honour Retry-After when the upstream tells us how long to wait.
    if resp is not None:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                await asyncio.sleep(min(float(retry_after), 30.0))
                return
            except ValueError:
                pass
    delay = min(2**attempt, 8) + random.uniform(0, 0.5)
    await asyncio.sleep(delay)
