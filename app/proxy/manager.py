"""
Proxy Rotation Manager
Strategy: try DIRECT first, then rotate through IPVanish SOCKS5 proxies
ONLY when direct is rate-limited (429). No free proxies.
If ALL proxies fail, retry the entire pool once more.
"""

import asyncio
import random
import threading
import time
import logging
from typing import Optional

import httpx

from app.config import SOCKS5_PROXIES

logger = logging.getLogger("zenlite.proxy")


class ProxyManager:
    """
    Manages proxy rotation with a direct-first fallback strategy.

    Flow per request:
      1. Attempt direct connection (no proxy)
      2. If DIRECT is rate-limited (429) → rotate through the IPVanish SOCKS5 pool
      3. If ALL proxies fail on first pass → retry the pool once more
      4. Any NON-rate-limit direct failure (5xx, 4xx, timeout, connect error)
         is returned/raised immediately — NO proxy rotation. Rotation exists
         only to escape IP rate-limits.
      5. Dead proxies get blacklisted: auth failures (401/403) for 24h,
         other failures for 5 min — rotation never burns time on known-dead
         proxies (e.g. expired IPVanish credentials returning 401).

    Proxy priority:
      1. IPVanish SOCKS5 (paid, reliable, with auth)
    """

    # Direct-path statuses that trigger proxy rotation: rate-limiting (429)
    # and Cloudflare IP-blocks (403 with an HTML page). Anything else (5xx,
    # JSON 4xx, timeouts) is a non-proxy failure and is returned/raised
    # as-is — never disguised as a proxy error.
    DIRECT_ROTATE_STATUSES = {429, 403}

    # Proxy-internal statuses that mean "try the next proxy"
    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}

    # Blacklist durations: 401/403 = dead credentials (24h); everything else = 5 min cooldown
    PROXY_BLACKLIST_AUTH_SECONDS = 24 * 3600
    PROXY_BLACKLIST_FAIL_SECONDS = 300

    def __init__(self):
        self._ipvanish_pool: list[str] = list(SOCKS5_PROXIES)
        self._current_index: int = 0
        self._lock = asyncio.Lock()
        # Proxy blacklist: proxy URL -> cooldown-until (epoch seconds)
        self._blacklist: dict[str, float] = {}
        # Shared client for the direct path: keep-alive + connection pooling
        # avoid paying a fresh TLS handshake on every request. Proxy fallback
        # attempts still use per-attempt clients (lazily closed).
        self._direct_client: Optional[httpx.AsyncClient] = None
        self._client_lock = threading.Lock()
        # Stats
        self.stats = {
            "total_requests": 0,
            "direct_successes": 0,
            "proxy_successes": 0,
            "rate_limited_retries": 0,
            "proxy_failures": 0,
            "retries": 0,
            "failures": 0,
            "blacklisted_proxies": 0,
        }

    @property
    def _proxy_pool(self) -> list[str]:
        """The IPVanish SOCKS5 pool (direct path needs no proxy)."""
        return list(self._ipvanish_pool)

    def _is_blacklisted(self, proxy_url: str) -> bool:
        """True if the proxy is in cooldown/blacklist (skip it)."""
        return self._blacklist.get(proxy_url, 0.0) > time.time()

    def _is_block_page(self, response) -> bool:
        """True if the response is an HTML block/challenge page (Cloudflare)."""
        ct = (response.headers.get("content-type") or "").lower()
        return "text/html" in ct

    def _blacklist_proxy(self, proxy_url: str, seconds: float):
        """Put a proxy into cooldown for `seconds`."""
        self._blacklist[proxy_url] = time.time() + seconds
        self.stats["blacklisted_proxies"] = len(self._blacklist)
        logger.info("Proxy %s blacklisted for %ds", self._proxy_label(proxy_url), int(seconds))

    def _next_proxy(self) -> str:
        """Get the next non-blacklisted proxy (round-robin across all)."""
        pool = self._proxy_pool
        if not pool:
            raise Exception("No proxies available")
        for _ in range(len(pool)):
            proxy = pool[self._current_index % len(pool)]
            self._current_index += 1
            if not self._is_blacklisted(proxy):
                return proxy
        raise Exception("All proxies are blacklisted (in cooldown)")

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

    def start(self):
        """Create the shared direct-path client eagerly. Call once at app
        startup so the client is bound to the server's event loop and the
        first request doesn't pay client-construction cost."""
        self._get_direct_client()

    def _get_direct_client(self) -> httpx.AsyncClient:
        """Return the shared direct-path client, creating it if needed."""
        if self._direct_client is not None and not self._direct_client.is_closed:
            return self._direct_client
        # Lock-guarded: only the constructor (sync) runs inside, so a plain
        # threading.Lock is safe and prevents two concurrent first requests
        # from creating orphaned clients.
        with self._client_lock:
            if self._direct_client is None or self._direct_client.is_closed:
                self._direct_client = httpx.AsyncClient(
                    timeout=httpx.Timeout(120.0),
                    follow_redirects=True,
                    limits=httpx.Limits(max_connections=128, max_keepalive_connections=32),
                )
        return self._direct_client

    async def aclose(self):
        """Close the shared direct client. Call on application shutdown."""
        if self._direct_client is not None and not self._direct_client.is_closed:
            await self._direct_client.aclose()
        self._direct_client = None

    def get_stats(self) -> dict:
        """Return current proxy rotation stats."""
        stats = {**self.stats}
        stats["pool_size"] = len(self._proxy_pool)
        stats["ipvanish_count"] = len(self._ipvanish_pool)
        stats["blacklisted_proxies"] = len(self._blacklist)
        return stats

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
                        self._blacklist.pop(proxy_url, None)  # healthy again
                        logger.info("Proxy %s succeeded (status=%d)", proxy_label, response.status_code)
                        return response
                    else:
                        last_error = Exception(f"Proxy {proxy_label} returned {response.status_code}")
                        self.stats["proxy_failures"] += 1
                        if response.status_code in (401, 403):
                            self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_AUTH_SECONDS)
                        else:
                            self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_FAIL_SECONDS)
                        logger.warning("Proxy %s failed: status=%d (blacklisted)", proxy_label, response.status_code)
            except Exception as e:
                # Any failure (timeouts, connect errors, and third-party quirks
                # like socksio's malformed-reply crash) counts as a proxy
                # failure so rotation continues to the next proxy.
                last_error = e
                self.stats["proxy_failures"] += 1
                self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_FAIL_SECONDS)
                logger.warning("Proxy %s failed: %s (blacklisted 5min)", proxy_label, type(e).__name__)

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
                    if response.status_code in (401, 403):
                        self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_AUTH_SECONDS)
                    else:
                        self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_FAIL_SECONDS)
                    logger.warning(
                        "Proxy stream %s error (status=%d), trying next...",
                        proxy_label, response.status_code,
                    )
                elif response.status_code < 400:
                    self.stats["proxy_successes"] += 1
                    self._blacklist.pop(proxy_url, None)  # healthy again
                    logger.info("Proxy stream %s succeeded (status=%d)", proxy_label, response.status_code)
                    yield response, proxy_url, client
                    return
                else:
                    last_error = Exception(f"Proxy stream {proxy_label} returned {response.status_code}")
                    self.stats["proxy_failures"] += 1
                    await response.aclose()
                    await client.aclose()
                    if response.status_code in (401, 403):
                        self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_AUTH_SECONDS)
                    else:
                        self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_FAIL_SECONDS)
                    logger.warning("Proxy stream %s failed: status=%d (blacklisted)", proxy_label, response.status_code)
            except Exception as e:
                # Any failure (timeouts, connect errors, and third-party quirks
                # like socksio's malformed-reply crash) counts as a proxy
                # failure so rotation continues to the next proxy.
                last_error = e
                self.stats["proxy_failures"] += 1
                self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_FAIL_SECONDS)
                logger.warning("Proxy stream %s failed: %s (blacklisted 5min)", proxy_label, type(e).__name__)

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
        direct_response: Optional[httpx.Response] = None
        direct_limited = False

        # ── Step 1: Direct connection (shared keep-alive client) ────────
        try:
            client = self._get_direct_client()
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                timeout=httpx.Timeout(timeout),
            )
            if response.status_code < 400:
                self.stats["direct_successes"] += 1
                return response
            if response.status_code in self.DIRECT_ROTATE_STATUSES and (
                response.status_code != 403 or self._is_block_page(response)
            ):
                direct_limited = True
                direct_response = response
                self.stats["rate_limited_retries"] += 1
                logger.warning(
                    "Direct rate-limited/blocked (status=%d), rotating through proxies...",
                    response.status_code,
                )
            else:
                # Non-rate-limit failure (5xx/4xx/JSON-403): return the upstream
                # error as-is. No proxy rotation — this is not a proxy problem.
                self.stats["failures"] += 1
                logger.warning("Direct failed: status=%d (not rate-limit/block; returning as-is, no proxy retry)", response.status_code)
                return response
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Timeouts/connect errors are not rate-limits: surface immediately.
            self.stats["failures"] += 1
            logger.warning("Direct failed: %s (not rate-limit; no proxy retry)", type(e).__name__)
            raise e

        # ── Step 2: Rotate through proxies (only reached on direct 429) ─
        try:
            return await self._try_proxies(method, url, headers, json_body, timeout, pool_size)
        except Exception as e:
            last_error = e

        # ── Step 3: RETRY the pool once more ────────────────────────────
        logger.warning("All %d proxies failed, retrying pool once more...", pool_size)
        self.stats["retries"] += 1
        try:
            return await self._try_proxies(method, url, headers, json_body, timeout, pool_size)
        except Exception as e:
            last_error = e

        # All exhausted — surface the ORIGINAL direct error (the 429), never
        # a misleading generic proxy error.
        self.stats["failures"] += 1
        if direct_response is not None:
            if direct_response.status_code == 429:
                logger.warning(
                    "Rotation exhausted (%d proxies, %d proxy failures); returning original direct status=%d",
                    pool_size, self.stats["proxy_failures"], direct_response.status_code,
                )
                return direct_response
            # Cloudflare block page — never pass raw HTML through; raise a clear error
            raise RuntimeError(
                f"Upstream blocked the request (HTTP 403 - Cloudflare/IP block) and all {pool_size} rotation proxies failed"
            )
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

        Yields (response, proxy_url, client) tuples. For the direct path
        `client` is None (shared keep-alive client — close the response but
        not the client); proxy attempts yield their per-attempt client.
        """
        self.stats["total_requests"] += 1
        last_error: Optional[Exception] = None
        pool_size = len(self._proxy_pool)
        direct_limited = False
        direct_limited_status: Optional[int] = None

        # ── Step 1: Direct connection (shared keep-alive client) ────────
        # Note: for the direct path `client` is yielded as None — the caller
        # must close the response but NOT the shared client. Proxy attempts
        # yield their per-attempt client for the caller to close.
        try:
            client = self._get_direct_client()
            # timeout is set on the request (build_request), not on send() —
            # send() does not accept a timeout kwarg in current httpx.
            request = client.build_request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                timeout=httpx.Timeout(timeout),
            )
            response = await client.send(request, stream=True)
            if response.status_code < 400:
                self.stats["direct_successes"] += 1
                yield response, None, None
                return
            if response.status_code in self.DIRECT_ROTATE_STATUSES and (
                response.status_code != 403 or self._is_block_page(response)
            ):
                direct_limited = True
                direct_limited_status = response.status_code
                self.stats["rate_limited_retries"] += 1
                await response.aclose()
                logger.warning("Direct stream rate-limited/blocked (status=%d), rotating through proxies...", response.status_code)
            else:
                # Non-rate-limit failure: surface the upstream error immediately
                # (no proxy rotation). Streaming can't return the response, so
                # raise with the status + body — the caller yields an error chunk.
                self.stats["failures"] += 1
                body = ""
                try:
                    body = (await response.aread())[:200].decode("utf-8", "replace")
                except Exception:
                    pass
                await response.aclose()
                logger.warning("Direct stream failed: status=%d (not rate-limit; no proxy retry)", response.status_code)
                raise RuntimeError(f"Upstream returned HTTP {response.status_code}: {body}")
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Timeouts/connect errors are not rate-limits: surface immediately.
            self.stats["failures"] += 1
            logger.warning("Direct stream failed: %s (not rate-limit; no proxy retry)", type(e).__name__)
            raise e

        # ── Step 2: Rotate through proxies (only reached on direct 429) ─
        try:
            async for item in self._try_proxies_stream(method, url, headers, json_body, timeout, pool_size):
                yield item
                return
        except Exception as e:
            last_error = e

        # ── Step 3: RETRY the pool once more ────────────────────────────
        logger.warning("All %d proxy streams failed, retrying pool once more...", pool_size)
        self.stats["retries"] += 1
        try:
            async for item in self._try_proxies_stream(method, url, headers, json_body, timeout, pool_size):
                yield item
                return
        except Exception as e:
            last_error = e

        # All exhausted — surface the ORIGINAL reason (the direct 429/block),
        # never a misleading generic proxy error.
        self.stats["failures"] += 1
        if direct_limited:
            raise RuntimeError(
                f"Upstream rate-limited/blocked (HTTP {direct_limited_status or 429}) and all {pool_size} rotation proxies failed"
            )
        raise last_error or Exception("All direct + proxy stream connections failed (after retry)")


# Singleton instance
proxy_manager = ProxyManager()
