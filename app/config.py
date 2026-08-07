"""
ZenLite Configuration
Provider definitions, proxy settings, and app configuration.
"""

import os
import httpx
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("zenlite.config")

# ── Server Settings ──────────────────────────────────────────────────────────
HOST = os.getenv("ZENLITE_HOST", "127.0.0.1")
PORT = int(os.getenv("ZENLITE_PORT", "8100"))

# ── OpenCode Base URL ────────────────────────────────────────────────────────
OPENCODE_BASE_URL = "https://opencode.ai/zen/v1"
# Allow overriding the OpenAI base URL. Defaults to OPENCODE_BASE_URL so
# clients that expect an OpenAI-compatible endpoint can be pointed at
# OpenCode's custom implementation by setting `OPENAI_BASE_URL`.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", OPENCODE_BASE_URL)


# ── Provider Definitions ─────────────────────────────────────────────────────
@dataclass
class ProviderConfig:
    """Defines a provider that ZenLite can route to."""
    id: str                              # Internal key
    name: str                            # Display name
    base_url: str                        # Upstream API base URL
    no_auth: bool                        # Whether API key is required
    models: list[str]                    # Available model IDs
    description: str = ""

    @property
    def auth_type(self) -> str:
        return "none" if self.no_auth else "api_key"


# Raw upstream model IDs (what the API actually expects)
RAW_MODELS = [
    "big-pickle",
    "deepseek-v4-flash-free",
    "mimo-v2.5-free",
    "hy3-free",
    "nemotron-3-ultra-free",
    "north-mini-code-free",
]

# Model prefixes for each auth type
FREE_MODEL_PREFIX = "oc"        # oc/big-pickle
ZEN_MODEL_PREFIX = "opencode"   # opencode/big-pickle


def _prefixed_models(prefix: str, raw: list[str]) -> list[str]:
    """Generate prefixed model names: ['oc/big-pickle', 'oc/deepseek-v4-flash-free', ...]"""
    return [f"{prefix}/{m}" for m in raw]


def strip_model_prefix(model: str) -> str:
    """
    Strip provider prefix from a model name and return the raw upstream model.
    'oc/big-pickle' → 'big-pickle'
    'opencode/mimo-v2.5-free' → 'mimo-v2.5-free'
    'big-pickle' (no prefix) → 'big-pickle' (pass-through)
    """
    for prefix in (FREE_MODEL_PREFIX, ZEN_MODEL_PREFIX):
        if model.startswith(f"{prefix}/"):
            return model[len(prefix) + 1 :]
    return model


# ── Model Aliases ────────────────────────────────────────────────────────────
# Map common client model names (GitHub Copilot defaults, generic names) to
# free OpenCode models so clients work with zero configuration.
MODEL_ALIASES = {
    "gpt-4o": "oc/deepseek-v4-flash-free",
    "gpt-4o-mini": "oc/deepseek-v4-flash-free",
    "gpt-4.1": "oc/deepseek-v4-flash-free",
    "gpt-4.1-mini": "oc/deepseek-v4-flash-free",
    "gpt-4.1-nano": "oc/deepseek-v4-flash-free",
    "gpt-5": "oc/deepseek-v4-flash-free",
    "gpt-5-mini": "oc/deepseek-v4-flash-free",
    "gpt-5-nano": "oc/mimo-v2.5-free",
    "claude-3-5-sonnet": "oc/mimo-v2.5-free",
    "claude-3-7-sonnet": "oc/deepseek-v4-flash-free",
    "claude-sonnet-4": "oc/deepseek-v4-flash-free",
    "gemini-2.5-pro": "oc/deepseek-v4-flash-free",
    "gemini-2.5-flash": "oc/mimo-v2.5-free",
    "deepseek-chat": "oc/deepseek-v4-flash-free",
    "deepseek-reasoner": "oc/deepseek-v4-flash-free",
}


PROVIDERS: dict[str, ProviderConfig] = {
    "opencode_free": ProviderConfig(
        id="opencode_free",
        name="OpenCode Free",
        base_url=f"{OPENCODE_BASE_URL}/chat/completions",
        no_auth=True,
        description="Free, no authentication required. Models prefixed with oc/.",
        models=_prefixed_models(FREE_MODEL_PREFIX, RAW_MODELS),
    ),
    "opencode_zen": ProviderConfig(
        id="opencode_zen",
        name="OpenCode Zen",
        base_url=f"{OPENCODE_BASE_URL}/chat/completions",
        no_auth=False,
        description="Requires an OpenCode API key. Models prefixed with opencode/.",
        models=_prefixed_models(ZEN_MODEL_PREFIX, RAW_MODELS),
    ),
    "openai": ProviderConfig(
        id="openai",
        name="OpenAI",
        base_url=f"{OPENAI_BASE_URL}/chat/completions",
        no_auth=False,
        description="OpenAI-compatible endpoint (defaults to OpenCode Zen). Handles all traffic with full tool-calling support.",
        models=RAW_MODELS + list(MODEL_ALIASES),
    ),
}


# ── Proxy Settings ───────────────────────────────────────────────────────────
# Strategy: try direct first, then fall back through these SOCKS5 proxies.
# IPVanish SOCKS5 servers
PROXY_USER = "4wKRruhNI"
PROXY_PASS = "CIfYtdMDe0"
PROXY_PORT = 1080

SOCKS5_PROXIES: list[str] = [
    f"socks5://{PROXY_USER}:{PROXY_PASS}@au.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@ca.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@it.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@nl.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@pl.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@sg.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@es.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@gb.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@iad.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@atl.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@chi.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@cvg.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@dal.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@lax.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@mia.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@nyc.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@phx.socks.ipvanish.com:{PROXY_PORT}",
    f"socks5://{PROXY_USER}:{PROXY_PASS}@sjc.socks.ipvanish.com:{PROXY_PORT}",
]


# ── Free Proxy List (Proxifly) ──────────────────────────────────────────────
# Auto-refreshing list of free proxies (SOCKS5 + HTTP + SOCKS4)
FREE_PROXY_URL = "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt"
FREE_PROXY_REFRESH_INTERVAL = 300  # seconds (5 minutes)


async def fetch_free_proxies() -> list[str]:
    """
    Download and parse the free proxy list from Proxifly.
    Returns a list of proxy URLs (socks5:// and http:// only, no socks4).
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(FREE_PROXY_URL)
            resp.raise_for_status()
            lines = resp.text.strip().splitlines()
            # Filter: only socks5 and http proxies (skip socks4 — httpx needs socksio for it)
            free = [
                line.strip()
                for line in lines
                if line.strip() and (line.startswith("socks5://") or line.startswith("http://"))
            ]
            logger.info("Fetched %d free proxies from Proxifly", len(free))
            return free
    except Exception as e:
        logger.warning("Failed to fetch free proxy list: %s", e)
        return []


# ── Dashboard Settings ───────────────────────────────────────────────────────
DASHBOARD_TITLE = "ZenLite — AI Gateway"
DASHBOARD_VERSION = "1.0.0"


# Use free proxies from Proxifly when True. Set to False to force using only
# the paid IPVanish SOCKS5 pool (recommended for reliability).
USE_FREE_PROXIES = False

