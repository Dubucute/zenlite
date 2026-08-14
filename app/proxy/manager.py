"""
Proxy Rotation Manager
Strategy: try DIRECT first, then rotate through IPVanish SOCKS5 proxies
ONLY when direct is rate-limited (429). No free proxies.
If ALL proxies fail, retry the entire pool once more.

Blacklist policy (important):
  - An HTTP status code (429/5xx/4xx) means the proxy CONNECTED and tunneled
    fine — the status is the UPSTREAM's verdict, never blacklist for it.
  - TRANSPORT faults (SOCKS handshake/auth failure, connect refused) prove
    the proxy itself is dead -> blacklisted (auth 24h, others 5 min).
  - Timeouts / read errors mean the upstream is slow (long contexts take
    50s+) — proxy is innocent, never blacklisted.
"""

import asyncio
import json
import random
import threading
import time
import logging
from typing import Optional

import httpx

from app.config import SOCKS5_PROXIES

logger = logging.getLogger("zenlite.proxy")


# ── socksio bytearray compat ────────────────────────────────────────────────
# On Windows, asyncio's Proactor transport delivers `bytearray` chunks to
# httpcore's SOCKS5 handshake, and socksio's `decode_address` — which is
# `functools.lru_cache`d — cannot hash a bytearray, so every connect-reply
# parse dies with "TypeError: unhashable type: 'bytearray'". uvicorn's dev
# reload / multi-worker children run a Selector loop (bytes) and are fine;
# single-worker no-reload runs (and the Dockerfile / Procfile `uvicorn` CMD)
# run a Proactor loop and would lose proxy fallback entirely. Normalize the
# bind address to `bytes` before handing it to the cached original.
def _install_socksio_compat() -> None:
    try:
        import socksio.socks5 as _socks5
        import socksio.utils as _utils
    except ImportError:  # httpx installed without [socks]
        return
    if getattr(_utils.decode_address, "_zenlite_socksio_compat", False):
        return
    _original = _utils.decode_address

    def _safe_decode(address_type, encoded_addr):
        return _original(address_type, bytes(encoded_addr))

    _safe_decode._zenlite_socksio_compat = True
    _utils.decode_address = _safe_decode
    # socks5.py binds the name at import time, so it must be patched too.
    _socks5.decode_address = _safe_decode


_install_socksio_compat()


# ── Log helpers ──────────────────────────────────────────────────────────────
# A bare "status=429" tells nobody why rotation is happening. Surface a short
# snippet of the upstream error body so quota errors like
# `FreeUsageLimitError: Rate limit exceeded...` are visible in the dashboard.
# Never raise — logging must not break a request.

def _response_snippet(response, limit: int = 160) -> str:
    """Short snippet of an already-read (non-stream) upstream error body."""
    try:
        return (response.text or "")[:limit].replace("\n", " ")
    except Exception:
        return ""


async def _stream_snippet(response, limit: int = 160) -> str:
    """Read a streamed upstream error body (aread) and return a short snippet."""
    try:
        raw = (await response.aread())[:limit]
        return raw.decode("utf-8", "replace").replace("\n", " ")
    except Exception:
        return ""


def _is_quota_exhausted(snippet: str) -> bool:
    """True when an upstream 429 body is a quota error (FreeUsageLimitError)
    rather than transient throttling — rotation against a drained quota is
    mostly futile, so we cap how many proxies we probe."""
    text = snippet.lower()
    return "freeusagelimiterror" in text or "rate limit exceeded" in text


def _extract_error_message(snippet: str) -> str:
    """Pull the human-readable message out of an upstream error body so
    client-facing errors don't embed raw nested JSON (which mangles SSE error
    chunks and client displays). Falls back to the raw snippet."""
    try:
        data = json.loads(snippet)
    except Exception:
        return snippet
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type")
        if msg:
            return str(msg)
    return snippet


class UpstreamVerdictError(RuntimeError):
    """An EXPECTED upstream error — quota exhausted, an HTTP status verdict, a
    slow upstream — that is relayed to the client. These are routine, so they
    are logged at WARNING without a traceback; only unexpected failures get
    ERROR-level logs."""


class UpstreamStatusError(UpstreamVerdictError):
    """Raised when the upstream returns a definitive HTTP error through a
    working proxy. The proxy tunneled fine — this is the upstream's verdict,
    so rotation must stop and the status/body surface to the caller instead
    of being swallowed and retried through the rest of the pool."""

    def __init__(self, status_code: int, detail: str = ""):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Upstream returned HTTP {status_code}: {detail[:200]}")


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
      4b. A 4xx returned THROUGH a working proxy is the upstream's verdict —
          surfaced immediately with its real status, no pool retry.
      4c. A 429 whose body is a quota error (FreeUsageLimitError) probes only a
          small sample of proxies, then surfaces the 429 — no full-pool sweep.
      4d. Known-good proxies (recent successes, most recent first) are tried
          before the round-robin pool — per-IP upstream quotas mean the last
          winner is the most likely to win again; when it 429s, move on.
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

    # When the upstream's 429 body reports quota exhaustion
    # (FreeUsageLimitError), sweeping the whole pool twice (up to 36 upstream
    # calls per client request) just amplifies the 429 storm. Per-IP quota
    # buckets can still have room, so probe a small sample of proxies, then
    # surface the upstream 429 as-is.
    QUOTA_ROTATION_CAP = 6

    # Backoff for 429 rate-limit bursts: small jittered sleep before trying
    # the next proxy. This gives the upstream a chance to recover and avoids
    # hammering it with a tight loop of 429s.
    RATE_LIMIT_BACKOFF_BASE = 0.5
    RATE_LIMIT_BACKOFF_MAX = 2.0

    # Blacklist durations: 401/403 = dead credentials (24h); everything else = 5 min cooldown
    PROXY_BLACKLIST_AUTH_SECONDS = 24 * 3600
    PROXY_BLACKLIST_FAIL_SECONDS = 300

    # Known-good proxy memory: a proxy that recently succeeded is tried FIRST
    # on the next rotation. Per-IP upstream quotas mean winners keep winning,
    # but a winner that 429s now falls through to the next candidate.
    GOOD_PROXY_MAX = 5
    GOOD_PROXY_TTL = 300.0

    # IMPORTANT: only TRANSPORT-level failures prove a proxy is dead.
    # An HTTP status code means the proxy connected and tunneled fine —
    # the status belongs to the UPSTREAM, never blacklist for it.
    # ReadTimeout/ReadError/WriteError mean the upstream is slow (long
    # contexts take 50s+) or reset the stream — the proxy is innocent,
    # never blacklist those either. Note: httpx's ReadError/WriteError/
    # CloseError all subclass NetworkError, so NetworkError must NOT be
    # listed here — only connect-level and proxy-level faults qualify.
    TRANSPORT_FAULT_TYPES = (
        httpx.ConnectError,    # connect refused / no route / DNS — proxy unreachable
        httpx.ConnectTimeout,  # proxy not accepting connections
        httpx.ProxyError,      # SOCKS handshake / auth failure — dead credentials
    )

    def __init__(self):
        self._ipvanish_pool: list[str] = list(SOCKS5_PROXIES)
        self._current_index: int = 0
        # Proxy blacklist: proxy URL -> cooldown-until (epoch seconds)
        self._blacklist: dict[str, float] = {}
        # Known-good proxies: (proxy_url, last-success epoch), most recent
        # first, capped. Tried before the round-robin pool during rotation.
        self._good: list[tuple[str, float]] = []
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
            "good_proxy_hits": 0,
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

    def _mark_good(self, proxy_url: str) -> None:
        """Record a proxy that just succeeded upstream. The most recent winner
        is tried first on the next rotation; entries expire after the TTL."""
        now = time.time()
        self._good = [
            (p, t) for p, t in self._good
            if p != proxy_url and now - t < self.GOOD_PROXY_TTL
        ]
        self._good.insert(0, (proxy_url, now))
        del self._good[self.GOOD_PROXY_MAX:]

    def _good_queue(self) -> list[str]:
        """Snapshot of known-good proxies (most recent first) to try before the
        round-robin pool. Built once per rotation call so concurrent requests
        each get their own queue — a shared cursor would let interleaved
        rotations re-try the same proxy (duplicate 429s) or skip candidates.
        """
        now = time.time()
        self._good = [(p, t) for p, t in self._good if now - t < self.GOOD_PROXY_TTL]
        return [p for p, _ in self._good if not self._is_blacklisted(p)]

    def _next_proxy(self, good_queue: Optional[list] = None) -> str:
        """Get the next non-blacklisted proxy.

        `good_queue` (a per-call snapshot from `_good_queue`) is drained first
        — known-good proxies are the most likely to win again under per-IP
        upstream quotas, and a winner that just got rate-limited falls through
        to the next candidate. Once the queue is empty, fall back to
        round-robin across the whole pool.
        """
        if good_queue:
            self.stats["good_proxy_hits"] += 1
            return good_queue.pop(0)
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
        good_queue = self._good_queue()  # per-call snapshot (race-free)

        for _ in range(max_proxies):
            try:
                proxy_url = self._next_proxy(good_queue)
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
                        # Upstream busy (429/5xx). Proxy worked fine — just
                        # move to the next proxy, do NOT blacklist.
                        last_error = Exception(f"Proxy {proxy_label} returned {response.status_code}")
                        self.stats["rate_limited_retries"] += 1
                        logger.warning(
                            "Proxy %s upstream status=%d: %s, trying next...",
                            proxy_label, response.status_code,
                            _response_snippet(response),
                        )
                        # Small jittered backoff to avoid hammering upstream
                        await asyncio.sleep(random.uniform(self.RATE_LIMIT_BACKOFF_BASE, self.RATE_LIMIT_BACKOFF_MAX))
                    elif response.status_code < 400:
                        self.stats["proxy_successes"] += 1
                        self._blacklist.pop(proxy_url, None)  # healthy again
                        self._mark_good(proxy_url)
                        logger.info("Proxy %s succeeded (status=%d)", proxy_label, response.status_code)
                        return response
                    else:
                        # 4xx from upstream through a WORKING proxy — the
                        # proxy is fine; surface the upstream verdict. Return
                        # the response as-is (the caller's validate_response
                        # turns it into an UpstreamError with the real
                        # status). Do NOT keep rotating: every proxy hits the
                        # same upstream, so the verdict won't change.
                        self.stats["proxy_failures"] += 1
                        logger.warning(
                            "Proxy %s upstream status=%d (proxy OK, surfacing upstream verdict)",
                            proxy_label, response.status_code,
                        )
                        return response
            except Exception as e:
                # Only TRANSPORT faults prove the proxy itself is dead
                # (SOCKS handshake/auth failure, connect refused, no route).
                # Timeouts / read errors mean the UPSTREAM is slow — the proxy
                # is innocent and must NOT be blacklisted.
                if isinstance(e, self.TRANSPORT_FAULT_TYPES):
                    self.stats["proxy_failures"] += 1
                    # SOCKS auth failure = dead credentials -> 24h; other
                    # transport faults -> 5 min cooldown.
                    is_auth = isinstance(e, httpx.ProxyError) and any(
                        s in str(e).lower() for s in ("auth", "socks5", "handshake")
                    )
                    if is_auth:
                        self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_AUTH_SECONDS)
                    else:
                        self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_FAIL_SECONDS)
                    logger.warning(
                        "Proxy %s transport failure: %s (blacklisted %ds)",
                        proxy_label, type(e).__name__,
                        self.PROXY_BLACKLIST_AUTH_SECONDS if is_auth else self.PROXY_BLACKLIST_FAIL_SECONDS,
                    )
                else:
                    # Upstream slow / read timeout / protocol quirk: proxy is
                    # fine, just try the next one — NO blacklist.
                    logger.warning(
                        "Proxy %s upstream slow/failed: %s (proxy NOT blacklisted)",
                        proxy_label, type(e).__name__,
                    )
                last_error = e
                # continue to next proxy
                continue

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
        good_queue = self._good_queue()  # per-call snapshot (race-free)

        for _ in range(max_proxies):
            try:
                proxy_url = self._next_proxy(good_queue)
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
                    # Upstream busy — proxy worked, try next, NO blacklist.
                    last_error = Exception(f"Proxy stream {proxy_label} returned {response.status_code}")
                    self.stats["rate_limited_retries"] += 1
                    snippet = await _stream_snippet(response)
                    await response.aclose()
                    await client.aclose()
                    logger.warning(
                        "Proxy stream %s upstream status=%d: %s, trying next...",
                        proxy_label, response.status_code, snippet,
                    )
                    # Small jittered backoff to avoid hammering upstream
                    await asyncio.sleep(random.uniform(self.RATE_LIMIT_BACKOFF_BASE, self.RATE_LIMIT_BACKOFF_MAX))
                elif response.status_code < 400:
                    self.stats["proxy_successes"] += 1
                    self._blacklist.pop(proxy_url, None)  # healthy again
                    self._mark_good(proxy_url)
                    logger.info("Proxy stream %s succeeded (status=%d)", proxy_label, response.status_code)
                    yield response, proxy_url, client
                    return
                else:
                    # 4xx through a working proxy = upstream verdict, not ours.
                    # Stop rotation and surface it immediately — the generic
                    # handler below must NOT swallow it as a transport/upstream
                    # fault and burn through the rest of the pool.
                    self.stats["proxy_failures"] += 1
                    body = ""
                    try:
                        body = (await response.aread())[:500].decode("utf-8", "replace")
                    except Exception:
                        pass
                    await response.aclose()
                    await client.aclose()
                    logger.warning(
                        "Proxy stream %s upstream status=%d (proxy OK, surfacing upstream verdict)",
                        proxy_label, response.status_code,
                    )
                    raise UpstreamStatusError(
                        response.status_code, _extract_error_message(body)
                    )
            except UpstreamStatusError:
                # Upstream's definitive verdict through a working proxy —
                # never retried, never blacklisted.
                raise
            except Exception as e:
                # Transport faults = proxy is dead (blacklist). Timeouts/read
                # errors = upstream slow (proxy innocent, keep going).
                if isinstance(e, self.TRANSPORT_FAULT_TYPES):
                    self.stats["proxy_failures"] += 1
                    is_auth = isinstance(e, httpx.ProxyError) and any(
                        s in str(e).lower() for s in ("auth", "socks5", "handshake")
                    )
                    if is_auth:
                        self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_AUTH_SECONDS)
                    else:
                        self._blacklist_proxy(proxy_url, self.PROXY_BLACKLIST_FAIL_SECONDS)
                    logger.warning(
                        "Proxy stream %s transport failure: %s (blacklisted %ds)",
                        proxy_label, type(e).__name__,
                        self.PROXY_BLACKLIST_AUTH_SECONDS if is_auth else self.PROXY_BLACKLIST_FAIL_SECONDS,
                    )
                else:
                    logger.warning(
                        "Proxy stream %s upstream slow/failed: %s (proxy NOT blacklisted)",
                        proxy_label, type(e).__name__,
                    )
                last_error = e
                # continue to next proxy
                continue

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
        quota_exhausted = False
        direct_snippet = ""

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
                direct_response = response
                self.stats["rate_limited_retries"] += 1
                direct_snippet = _response_snippet(response)
                quota_exhausted = _is_quota_exhausted(direct_snippet)
                logger.warning(
                    "Direct rate-limited/blocked (status=%d): %s, rotating through proxies...%s",
                    response.status_code, direct_snippet,
                    " (quota exhausted — capped rotation)" if quota_exhausted else "",
                )
                # Small backoff before proxy rotation to avoid immediate burst
                await asyncio.sleep(random.uniform(self.RATE_LIMIT_BACKOFF_BASE, self.RATE_LIMIT_BACKOFF_MAX))
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
        max_proxies = min(pool_size, self.QUOTA_ROTATION_CAP) if quota_exhausted else pool_size
        try:
            return await self._try_proxies(method, url, headers, json_body, timeout, max_proxies)
        except UpstreamStatusError:
            # Upstream verdict through a working proxy — surface it as-is,
            # no pool retry (retrying can't change the upstream's answer).
            raise
        except Exception as e:
            last_error = e

        if quota_exhausted:
            # Free-tier quota drained: a short probe is enough — burning the
            # whole pool twice would turn one client request into dozens of
            # upstream 429s. Surface the original direct verdict.
            self.stats["failures"] += 1
            logger.warning(
                "Quota exhausted: %d proxies probed without success; returning direct status=%d: %s",
                max_proxies, direct_response.status_code, direct_snippet,
            )
            return direct_response

        # ── Step 3: RETRY the pool once more (transient throttling only) ─
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
                    "Rotation exhausted (%d proxies, %d proxy failures); returning original direct status=%d: %s",
                    pool_size, self.stats["proxy_failures"], direct_response.status_code,
                    _response_snippet(direct_response),
                )
                return direct_response
            # Cloudflare block page — never pass raw HTML through; raise a clear
            # error (expected verdict, relayed without a traceback).
            raise UpstreamVerdictError(
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
        quota_exhausted = False
        snippet = ""

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
                snippet = await _stream_snippet(response)
                quota_exhausted = _is_quota_exhausted(snippet)
                await response.aclose()
                logger.warning(
                    "Direct stream rate-limited/blocked (status=%d): %s, rotating through proxies...%s",
                    response.status_code, snippet,
                    " (quota exhausted — capped rotation)" if quota_exhausted else "",
                )
                # Small backoff before proxy rotation
                await asyncio.sleep(random.uniform(self.RATE_LIMIT_BACKOFF_BASE, self.RATE_LIMIT_BACKOFF_MAX))
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
                raise UpstreamVerdictError(
                    f"Upstream returned HTTP {response.status_code}: "
                    f"{_extract_error_message(body)}"
                )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ConnectTimeout) as e:
            # Timeouts/connect errors are not rate-limits: surface immediately.
            self.stats["failures"] += 1
            logger.warning("Direct stream failed: %s (not rate-limit; no proxy retry)", type(e).__name__)
            raise e

        # ── Step 2: Rotate through proxies (only reached on direct 429) ─
        max_proxies = min(pool_size, self.QUOTA_ROTATION_CAP) if quota_exhausted else pool_size
        try:
            async for item in self._try_proxies_stream(method, url, headers, json_body, timeout, max_proxies):
                yield item
                return
        except UpstreamStatusError:
            # Upstream verdict through a working proxy — surface it as-is,
            # no pool retry (retrying can't change the upstream's answer).
            raise
        except Exception as e:
            last_error = e

        if quota_exhausted:
            self.stats["failures"] += 1
            raise UpstreamVerdictError(
                f"Upstream quota exhausted (HTTP {direct_limited_status or 429}): "
                f"{_extract_error_message(snippet)}"
            )

        # ── Step 3: RETRY the pool once more (transient throttling only) ─
        logger.warning("All %d proxy streams failed, retrying pool once more...", pool_size)
        self.stats["retries"] += 1
        try:
            async for item in self._try_proxies_stream(method, url, headers, json_body, timeout, pool_size):
                yield item
                return
        except UpstreamStatusError:
            # Upstream verdict through a working proxy — never retried further.
            raise
        except Exception as e:
            last_error = e

        # All exhausted — surface the ORIGINAL reason (the direct 429/block),
        # never a misleading generic proxy error.
        self.stats["failures"] += 1
        if direct_limited:
            raise UpstreamVerdictError(
                f"Upstream rate-limited/blocked (HTTP {direct_limited_status or 429}) and all {pool_size} rotation proxies failed"
            )
        raise last_error or Exception("All direct + proxy stream connections failed (after retry)")


# Singleton instance
proxy_manager = ProxyManager()
