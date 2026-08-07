"""
OpenCode Zen Provider
Requires API key (Bearer token).
"""

from typing import Optional

from app.providers.base import BaseProvider
from app.config import PROVIDERS


class OpenCodeZenProvider(BaseProvider):
    """
    OpenCode Zen — requires a user-supplied API key.
    The key is sent as a Bearer token in the Authorization header.
    """

    def __init__(self):
        super().__init__(PROVIDERS["opencode_zen"])

    def build_headers(self, api_key: Optional[str] = None) -> dict:
        """Build headers with Bearer token."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "ZenLite/1.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

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
        """Build payload. Same structure as Free provider."""
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
        for key in ("frequency_penalty", "presence_penalty", "stop", "n"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]
        return payload


# Singleton
opencode_zen_provider = OpenCodeZenProvider()
