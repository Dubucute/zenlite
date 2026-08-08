"""
OpenAI-Compatible Passthrough Provider

ZenLite's single upstream provider. It fully handles the OpenAI protocol
against `OPENAI_BASE_URL` (defaults to the OpenCode Zen endpoint): every
field the client sends — `tools`, `tool_choice`, `functions`,
`function_call`, `response_format`, `stream_options`, `seed`, ... — is
forwarded verbatim, and upstream `tool_calls` (including streaming deltas)
are returned to the client unchanged. This is what lets tool-calling
clients like GitHub Copilot agent mode work end-to-end.

The OpenAI Python SDK is deliberately not used here: OpenCode speaks the
OpenAI protocol natively, so a transparent HTTP passthrough keeps ZenLite's
proxy rotation and retry behavior (direct-first, then the SOCKS5 pool),
which the SDK cannot provide.
"""

import json
from typing import Any, AsyncGenerator, Optional

from app.config import PROVIDERS
from app.providers.base import BaseProvider, merge_extra_fields
from app.proxy.manager import proxy_manager


class OpenAIProvider(BaseProvider):
    """Fully OpenAI-compatible passthrough for an OpenAI-compatible endpoint."""

    def __init__(self):
        super().__init__(PROVIDERS["openai"])

    def build_headers(self, api_key: Optional[str] = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            # Browser-like UA: upstream sits behind Cloudflare and bot-detects
            # non-browser clients, which triggers transient 403 block pages.
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/event-stream",
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
        extra_body: Optional[dict] = None,
        **kwargs: Any,
    ) -> dict:
        payload: dict = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        for key, value in (
            ("temperature", temperature),
            ("max_tokens", max_tokens),
            ("top_p", top_p),
            ("frequency_penalty", kwargs.get("frequency_penalty")),
            ("presence_penalty", kwargs.get("presence_penalty")),
            ("stop", kwargs.get("stop")),
            ("n", kwargs.get("n")),
        ):
            if value is not None:
                payload[key] = value
        # Forward everything else the client sent, verbatim
        merge_extra_fields(payload, extra_body)
        return payload

    async def chat_completion(
        self,
        model: str,
        messages: list[dict],
        stream: bool = False,
        api_key: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        extra_body: Optional[dict] = None,
        **kwargs: Any,
    ) -> dict:
        """Non-streaming call. Returns the upstream JSON as-is, including
        `tool_calls` when the model decides to call a tool."""
        payload = self.build_payload(
            model=model,
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            extra_body=extra_body,
            **kwargs,
        )
        response = await proxy_manager.execute(
            method="POST",
            url=self.config.base_url,
            headers=self.build_headers(api_key),
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
        extra_body: Optional[dict] = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict, None]:
        """Streaming call. Yields SSE chunks unchanged, so `delta.tool_calls`
        deltas reach the client (e.g. Copilot agent mode) untouched."""
        payload = self.build_payload(
            model=model,
            messages=messages,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=top_p,
            extra_body=extra_body,
            **kwargs,
        )
        async for response, proxy_url, client in proxy_manager.execute_stream(
            method="POST",
            url=self.config.base_url,
            headers=self.build_headers(api_key),
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
                            yield json.loads(data_str)
                        except Exception:
                            continue
                    elif line.startswith("{"):
                        try:
                            yield json.loads(line)
                        except Exception:
                            continue
            finally:
                await response.aclose()
                # Direct-path streams use the shared proxy_manager client;
                # only per-attempt proxy clients need closing here.
                if client is not None:
                    await client.aclose()


# Singleton
openai_provider = OpenAIProvider()
