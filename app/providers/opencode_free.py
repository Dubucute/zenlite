"""
OpenCode Free Provider
No authentication required — keyless / no-auth.
All requests are proxied through the proxy rotation manager.
"""

import time
import uuid
from typing import Optional

from app.providers.base import BaseProvider
from app.config import PROVIDERS


class OpenCodeFreeProvider(BaseProvider):
    """
    OpenCode Free — no API key needed.

    The "no-auth" connection model mirrors OmniRoute's synthetic connection:
      connectionId = "noauth", apiKey = null
    All requests go through proxy rotation for reliability.
    """

    def __init__(self):
        super().__init__(PROVIDERS["opencode_free"])

    def build_headers(self, api_key: Optional[str] = None) -> dict:
        """
        No auth header needed. OpenCode Free accepts requests without any key.
        If someone accidentally passes a key, we ignore it.
        """
        return {
            "Content-Type": "application/json",
            "User-Agent": "ZenLite/1.0",
        }

    def build_payload(
        self,
        model: str,
        messages: list[dict],
        stream: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        **kwargs,
    ) -> dict:
        """Build payload. Pass through valid OpenAI-compatible fields."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if top_p is not None:
            payload["top_p"] = top_p
        # Pass through extra fields the caller may want
        for key in ("frequency_penalty", "presence_penalty", "stop", "n"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]
        return payload


# Singleton
opencode_free_provider = OpenCodeFreeProvider()
