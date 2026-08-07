"""
Base Provider Class
All providers inherit from this to ensure a consistent interface.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, AsyncGenerator

import httpx

from app.config import ProviderConfig
from app.proxy.manager import proxy_manager

logger = logging.getLogger("zenlite.providers")


class BaseProvider(ABC):
    """
    Abstract base for upstream AI providers.

    Subclasses must implement:
      - build_headers(api_key) → dict of HTTP headers
      - build_payload(request_body) → dict for upstream JSON body
      - validate_response(response) → raise on upstream errors
    """

    def __init__(self, config: ProviderConfig):
        self.config = config

    # ── Public Interface ──────────────────────────────────────────────────

    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        stream: bool = False,
        api_key: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        **kwargs,
    ) -> dict:
        """Send a non-streaming chat completion request."""
        headers = self.build_headers(api_key)
        payload = self.build_payload(
            model=model,
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            **kwargs,
        )

        response = await proxy_manager.execute(
            method="POST",
            url=self.config.base_url,
            headers=headers,
            json_body=payload,
        )
        self.validate_response(response)
        return response.json()

    async def chat_completion_stream(
        self,
        model: str,
        messages: list[dict],
        api_key: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        **kwargs,
    ) -> AsyncGenerator[dict, None]:
        """Send a streaming chat completion request. Yields SSE JSON chunks."""
        headers = self.build_headers(api_key)
        headers["Accept"] = "text/event-stream"
        payload = self.build_payload(
            model=model,
            messages=messages,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            **kwargs,
        )

        async for response, proxy_url, client in proxy_manager.execute_stream(
            method="POST",
            url=self.config.base_url,
            headers=headers,
            json_body=payload,
        ):
            try:
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            return
                        try:
                            import json
                            chunk = json.loads(data_str)
                            yield chunk
                        except Exception:
                            logger.debug("Could not parse SSE chunk: %s", data_str)
                    # Handle plain JSON lines too (some providers don't use SSE prefix)
                    elif line.startswith("{"):
                        try:
                            import json
                            chunk = json.loads(line)
                            yield chunk
                        except Exception:
                            logger.debug("Could not parse JSON line: %s", line)
            finally:
                await response.aclose()
                await client.aclose()

    # ── Abstract Methods ──────────────────────────────────────────────────

    @abstractmethod
    def build_headers(self, api_key: Optional[str] = None) -> dict:
        """Build HTTP headers for the upstream request."""
        ...

    @abstractmethod
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
        """Build the JSON payload for the upstream request."""
        ...

    def validate_response(self, response: httpx.Response) -> None:
        """Validate upstream response. Raises on errors."""
        if response.status_code >= 400:
            error_body = response.text[:500]
            raise UpstreamError(
                status_code=response.status_code,
                detail=error_body,
                provider=self.config.id,
            )


class UpstreamError(Exception):
    """Raised when the upstream provider returns an error."""

    def __init__(self, status_code: int, detail: str, provider: str):
        self.status_code = status_code
        self.detail = detail
        self.provider = provider
        super().__init__(
            f"Upstream error from {provider}: {status_code} — {detail[:200]}"
        )
