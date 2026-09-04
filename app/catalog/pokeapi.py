"""Cached PokeAPI client, scope 5.5.

The catalog is seeded once and re-seeded only by explicit Admin action, so this
module is never on a request path. It caches every response to disk anyway: a
full seed is ~2,100 requests, and a cache turns a retry after a network blip
into seconds rather than another full crawl.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app import config

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^a-z0-9._-]+")


class PokeApiError(RuntimeError):
    pass


def _cache_path(url: str, cache_dir: Path) -> Path:
    tail = url.split("/api/v2/", 1)[-1].strip("/")
    return cache_dir / "pokeapi" / (_UNSAFE.sub("_", tail.lower()) + ".json")


class PokeApiClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        cache_dir: Path | None = None,
        concurrency: int | None = None,
        use_cache: bool = True,
    ) -> None:
        self.base_url = (base_url or config.POKEAPI_BASE).rstrip("/")
        self.cache_dir = cache_dir or config.CACHE_DIR
        self.use_cache = use_cache
        self._sem = asyncio.Semaphore(concurrency or config.SEED_CONCURRENCY)
        self._client: httpx.AsyncClient | None = None
        self.request_count = 0
        self.cache_hits = 0

    async def __aenter__(self) -> "PokeApiClient":
        self._client = httpx.AsyncClient(
            # Required: PokeAPI returns 403 to requests with no User-Agent.
            headers={"User-Agent": config.USER_AGENT, "Accept": "application/json"},
            timeout=config.SEED_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _resolve(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"

    async def get(self, path_or_url: str, *, retries: int = 3) -> dict[str, Any]:
        url = self._resolve(path_or_url)
        cache_file = _cache_path(url, self.cache_dir)

        if self.use_cache and cache_file.is_file():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                self.cache_hits += 1
                return data
            except (OSError, json.JSONDecodeError):
                log.warning("discarding unreadable cache entry %s", cache_file)

        if self._client is None:
            raise PokeApiError("client used outside its async context")

        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                async with self._sem:
                    response = await self._client.get(url)
                self.request_count += 1
                if response.status_code == 404:
                    raise PokeApiError(f"404 for {url}")
                response.raise_for_status()
                data = response.json()
                break
            except PokeApiError:
                raise
            except Exception as exc:  # network flake, 5xx, throttle
                last_error = exc
                if attempt == retries - 1:
                    raise PokeApiError(f"failed to fetch {url}: {exc}") from exc
                await asyncio.sleep(1.5 * (attempt + 1))
        else:  # pragma: no cover - loop always breaks or raises
            raise PokeApiError(f"failed to fetch {url}: {last_error}")

        if self.use_cache:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    async def get_many(self, paths: list[str]) -> list[dict[str, Any]]:
        return await asyncio.gather(*(self.get(p) for p in paths))

    async def list_species(self, limit: int | None = None) -> list[dict[str, Any]]:
        page = await self.get(f"pokemon-species?limit={limit or 100000}&offset=0")
        return page["results"]
