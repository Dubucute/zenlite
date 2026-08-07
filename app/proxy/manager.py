"""
Proxy Rotation Manager
Strategy: try DIRECT first, then rotate through SOCKS5 proxies on failure.
Free proxies auto-refresh from Proxifly every 5 minutes.
If ALL proxies fail, retry the entire pool once more.
"""

import asyncio
import random
import time
import logging
from typing import Optional

import httpx

from app.config import SOCKS5_PROXIES, FREE_PROXY_REFRESH_INTERVAL, fetch_free_proxies

logger = logging.getLogger("zenlite.proxy")


class ProxyManager:
    """
    Manages proxy rotation with a direct-first fallback strategy.

    Flow per request:
      1. Attempt direct connection (no proxy)
      2. On rate-limit (429), server error (5xx), timeout, connection error,
         or any non-2xx → rotate to next proxy
      3. Continue rotating through IPVanish SOCKS5, then free SOCKS5/HTTP proxies
      4. If ALL proxies fail on first pass → retry the entire pool once more
      5. Return first successful response or raise last error

    Proxy priority:
      1. IPVanish SOCKS5 (paid, reliable, with auth)
      2. Free SOCKS5 from Proxifly (auto-refreshing, no auth)
      3. Free HTTP from Proxifly (auto-refreshing, no auth)
    """

    # HTTP status codes that trigger proxy rotation
    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

    def __init__(self):
        self._ipvanish_pool: list[str] = list(SOCKS5_PROXIES)
        self._free_pool: list[str] = []  # populated by refresh
        self._current_index: int = 0
        self._lock = asyncio.Lock()
        self._refresh_task: Optional[asyncio.Task] = None
        # Stats
        self.stats = {
            "total_requests": 0,
            "direct_successes": 0,
            "proxy_successes": 0,
            "rate_limited_retries": 0,
            "proxy_failures": 0,
            "retries": 0,
            "failures": 0,
            "free_proxies_count": 0,
        }

    @property
    def _proxy_pool(self) -> list[str]:
        """Combined pool: IPVanish first, then free proxies."""
        return self._ipvanish_pool + self._free_pool

    def _next_proxy(self) -> str:
        """Get the next proxy from the pool (round-robin across all)."""
        pool = self._proxy_pool
        if not pool:
            raise Exception("No proxies available")
        proxy = pool[self._current_index % len(pool)]
        self._current_index += 1
        return proxy

    def _build_proxy_map(self, proxy_url: Optional[str]):
        """Return an httpx-compatible proxy value for the given proxy URL."""
        if proxy_url is None:
            return None
        return proxy_url

    def _proxy_label(self, proxy_url: str) -> str:
        """Extract a short label from a proxy URL."""
        if "@" in proxy_url:
            return proxy_url.split("@")[1].split(":")[0]
        return proxy_url.split("://")[1].split(":")[0]

    def get_stats(self) -> dict:
        """Return current proxy rotation stats."""
        stats = {**self.stats}
        stats["pool_size"] = len(self._proxy_pool)
        stats["ipvanish_count"] = len(self._ipvanish_pool)
        stats["free_proxies_count"] = len(self._free_pool)
        return stats

    # ── Free Proxy Refresh ───────────────────────────────────────────────
    async def refresh_proxies(self):
        """Download free proxies and merge into the pool."""
        free = await fetch_free_proxies()
        if free:
            self._free_pool = free
            self.stats["free_proxies_count"] = len(free)
            logger.info(
                "Proxy pool updated: %d IPVanish + %d free = %d total",
                len(self._ipvanish_pool),
                len(free),
                len(self._ipvanish_pool) + len(free),
            )
        else:
            logger.warning("Free proxy refresh returned empty, keeping existing pool")

    async def _refresh_loop(self):
        """Background loop that refreshes free proxies periodically."""
        while True:
            await asyncio.sleep(FREE_PROXY_REFRESH_INTERVAL)
            try:
                await self.refresh_proxies()
            except Exception as e:
                logger.error("Error during proxy refresh: %s", e)

    def start_refresh_task(self):
        """Start the background proxy refresh task."""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop())
            logger.info("Started free proxy refresh task (every %ds)", FREE_PROXY_REFRESH_INTERVAL)

    def stop_refresh_task(self):
        """Stop the background proxy refresh task."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            logger.info("Stopped free proxy refresh task")

    # ── Single attempt through all proxies ───────────────────────────────
    async def _try_proxies(
        self,
        method: str,
        url: str,
        headers: Optional[dict],
        json_body: Optional[dict],
        timeout: float,
        max_proxies: int,
    ) -> httpx.Response:
        """Try up to max_proxies proxies round-robin. Returns first success or raises."""
        last_error: Optional[Exception] = None

        for _ in range(max_proxies):
            try:
                proxy_url = self._next_proxy()
            except Exception:
                break
            proxy_label = self._proxy_label(proxy_url)

            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=True,
                    proxy=self._build_proxy_map(proxy_url),
                ) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        headers=headers,
                        json=json_body,
                    )
                    if response.status_code in self.RETRYABLE_STATUSES:
                        last_error = Exception(f"Proxy {proxy_label} returned {response.status_code}")
                        self.stats["rate_limited_retries"] += 1
                        logger.warning(
                            "Proxy %s error (status=%d), trying next...",
                            proxy_label, response.status_code,
                        )
                    elif response.status_code < 400:
                        self.stats["proxy_successes"] += 1
                        logger.info("Proxy %s succeeded (status=%d)", proxy_label, response.status_code)
                        return response
                    else:
                        last_error = Exception(f"Proxy {proxy_label} returned {response.status_code}")
                        self.stats["proxy_failures"] += 1
                        logger.warning("Proxy %s failed: status=%d", proxy_label, response.status_code)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_error = e
                self.stats["proxy_failures"] += 1
                logger.warning("Proxy %s failed: %s", proxy_label, type(e).__name__)

        raise last_error or Exception("All proxies failed")

    async def _try_proxies_stream(
        self,
        method: str,
        url: str,
        headers: Optional[dict],
        json_body: Optional[dict],
        timeout: float,
        max_proxies: int,
    ):
        """Try up to max_proxies proxies for streaming. Yields (response, proxy_url, client) or raises."""
        last_error: Optional[Exception] = None

        for _ in range(max_proxies):
            try:
                proxy_url = self._next_proxy()
            except Exception:
                break
            proxy_label = self._proxy_label(proxy_url)

            try:
                client = httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=True,
                    proxy=self._build_proxy_map(proxy_url),
                )
                request = client.build_request(
                    method=method, url=url, headers=headers, json=json_body
                )
                response = await client.send(request, stream=True)
                if response.status_code in self.RETRYABLE_STATUSES:
                    last_error = Exception(f"Proxy stream {proxy_label} returned {response.status_code}")
                    self.stats["rate_limited_retries"] += 1
                    await response.aclose()
                    await client.aclose()
                    logger.warning(
                        "Proxy stream %s error (status=%d), trying next...",
                        proxy_label, response.status_code,
                    )
                elif response.status_code < 400:
                    self.stats["proxy_successes"] += 1
                    logger.info("Proxy stream %s succeeded (status=%d)", proxy_label, response.status_code)
                    yield response, proxy_url, client
                    return
                else:
                    last_error = Exception(f"Proxy stream {proxy_label} returned {response.status_code}")
                    self.stats["proxy_failures"] += 1
                    await response.aclose()
                    await client.aclose()
                    logger.warning("Proxy stream %s failed: status=%d", proxy_label, response.status_code)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_error = e
                self.stats["proxy_failures"] += 1
                logger.warning("Proxy stream %s failed: %s", proxy_label, type(e).__name__)

        raise last_error or Exception("All proxy streams failed")

    # ── Main execute methods ─────────────────────────────────────────────
    async def execute(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        json_body: Optional[dict] = None,
        timeout: float = 120.0,
    ) -> httpx.Response:
        """
        Execute an HTTP request with proxy rotation fallback + retry.

        1. Try direct
        2. If that fails, rotate through all proxies
        3. If ALL proxies fail, retry the entire pool once more
        4. Return first successful response or raise last error
        """
        self.stats["total_requests"] += 1
        last_error: Optional[Exception] = None
        pool_size = len(self._proxy_pool)

        # ── Step 1: Direct connection ────────────────────────────────────
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
            ) as client:
                response = await client.request(
                    method=method, url=url, headers=headers, json=json_body,
                )
                if response.status_code in self.RETRYABLE_STATUSES:
                    last_error = Exception(f"Direct request returned {response.status_code}")
                    logger.warning("Direct rate-limited/error (status=%d), rotating...", response.status_code)
                    self.stats["rate_limited_retries"] += 1
                elif response.status_code < 400:
                    self.stats["direct_successes"] += 1
                    return response
                else:
                    last_error = Exception(f"Direct request returned {response.status_code}")
                    logger.warning("Direct failed: status=%d", response.status_code)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_error = e
            logger.warning("Direct failed: %s", type(e).__name__)

        # ── Step 2: Try all proxies ─────────────────────────────────────
        try:
            return await self._try_proxies(method, url, headers, json_body, timeout, pool_size)
        except Exception as e:
            last_error = e

        # ── Step 3: RETRY — try all proxies again ───────────────────────
        logger.warning("All %d proxies failed, retrying entire pool...", pool_size)
        self.stats["retries"] += 1
        try:
            return await self._try_proxies(method, url, headers, json_body, timeout, pool_size)
        except Exception as e:
            last_error = e

        # All exhausted
        self.stats["failures"] += 1
        raise last_error or Exception("All direct + proxy connections failed (after retry)")

    async def execute_stream(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        json_body: Optional[dict] = None,
        timeout: float = 120.0,
    ):
        """
        Execute a streaming HTTP request with proxy rotation fallback + retry.

        Yields (response, proxy_url, client) tuples.
        """
        self.stats["total_requests"] += 1
        last_error: Optional[Exception] = None
        pool_size = len(self._proxy_pool)

        # ── Step 1: Direct connection ────────────────────────────────────
        try:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
            )
            response = await client.send(
                client.build_request(method=method, url=url, headers=headers, json=json_body),
                stream=True,
            )
            if response.status_code in self.RETRYABLE_STATUSES:
                last_error = Exception(f"Direct stream returned {response.status_code}")
                self.stats["rate_limited_retries"] += 1
                await response.aclose()
                await client.aclose()
                logger.warning("Direct stream error (status=%d), rotating...", response.status_code)
            elif response.status_code < 400:
                self.stats["direct_successes"] += 1
                yield response, None, client
                return
            else:
                last_error = Exception(f"Direct stream returned {response.status_code}")
                await response.aclose()
                await client.aclose()
                logger.warning("Direct stream failed: status=%d", response.status_code)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
            last_error = e
            logger.warning("Direct stream failed: %s", type(e).__name__)

        # ── Step 2: Try all proxies ─────────────────────────────────────
        try:
            async for item in self._try_proxies_stream(method, url, headers, json_body, timeout, pool_size):
                yield item
                return
        except Exception as e:
            last_error = e

        # ── Step 3: RETRY — try all proxies again ───────────────────────
        logger.warning("All %d proxy streams failed, retrying entire pool...", pool_size)
        self.stats["retries"] += 1
        try:
            async for item in self._try_proxies_stream(method, url, headers, json_body, timeout, pool_size):
                yield item
                return
        except Exception as e:
            last_error = e

        # All exhausted
        self.stats["failures"] += 1
        raise last_error or Exception("All direct + proxy stream connections failed (after retry)")


# Singleton instance
proxy_manager = ProxyManager()
