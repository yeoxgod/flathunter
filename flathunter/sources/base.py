from __future__ import annotations

import asyncio
import random

import httpx

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5",
}


class FetchError(Exception):
    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked  # True при 403/429


class Fetcher:
    """Вежливый HTTP-клиент: паузы между запросами, повторы при сбоях сети.

    При 403/429 сразу останавливается (без обходов): это сигнал сайта сбавить темп.
    """

    BLOCK_PAUSE = 3600  # после 403/429 к этому сайту не обращаемся час

    def __init__(self, min_delay: float = 1.5, max_delay: float = 4.0):
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.blocked_until: dict[str, float] = {}
        self._client = httpx.AsyncClient(
            headers=HEADERS, timeout=httpx.Timeout(25.0), follow_redirects=True
        )

    async def get(self, url: str) -> str:
        import time
        from urllib.parse import urlparse

        host = urlparse(url).netloc.lower()
        if self.blocked_until.get(host, 0) > time.time():
            raise FetchError(f"{host} недавно ответил 403, пауза: {url}", blocked=True)
        await asyncio.sleep(random.uniform(self.min_delay, self.max_delay))
        last: object = None
        for attempt in range(3):
            try:
                r = await self._client.get(url)
            except httpx.HTTPError as e:
                last = e
                await asyncio.sleep(2 ** attempt * 2)
                continue
            if r.status_code in (403, 429):
                self.blocked_until[host] = time.time() + self.BLOCK_PAUSE
                raise FetchError(
                    f"HTTP {r.status_code} (блокировка или лимит запросов): {url}", blocked=True
                )
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                await asyncio.sleep(2 ** attempt * 2)
                continue
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}: {url}")
            return r.text
        raise FetchError(f"сеть или сервер недоступны ({last}): {url}")

    async def aclose(self) -> None:
        await self._client.aclose()
