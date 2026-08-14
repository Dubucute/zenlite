"""
OpenAI-Compatible Chat Router
Handles /v1/chat/completions with both streaming and non-streaming.

ZenLite is a transparent OpenAI-compatible gateway: the full client request
body (tools, tool_choice, functions, stream_options, ...) is forwarded
verbatim to the upstream (OpenCode Zen by default), so tool-calling clients
like GitHub Copilot agent mode work end-to-end.
"""

import json
import logging
import time
import uuid
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import PROVIDERS, RAW_MODELS, MODEL_ALIASES, strip_model_prefix
from app.providers.base import UpstreamError

logger = logging.getLogger("zenlite.router")

router = APIRouter(prefix="/v1", tags=["OpenAI-Compatible"])

# ZenLite-specific request fields that are never forwarded upstream
ZENLITE_ONLY_FIELDS = {"provider"}


# ── Provider Selection ───────────────────────────────────────────────────────

def get_provider(provider_name: Optional[str] = None, api_key: Optional[str] = None):
    """Every request is handled by the single no-auth OpenCode provider.

    The `provider` field is accepted for backwards compatibility but is
    ignored — ZenLite is a keyless gateway to OpenCode Free:
      - "opencode_free" (default) / None / anything — no API key required
    """
    from app.providers.openai import openai_provider

    return openai_provider, None


# ── API Key Extraction ──────────────────────────────────────────────────────

def extract_api_key(request: Request) -> Optional[str]:
    """Extract API key from the Authorization header (Bearer token)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


# ── Models Endpoint ──────────────────────────────────────────────────────────

@router.get("/models")
async def list_models():
    """List all available models across all providers."""
    models = []
    for provider_config in PROVIDERS.values():
        for model_id in provider_config.models:
            models.append({
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": provider_config.name,
                "permission": [],
                "root": model_id,
                "parent": None,
            })
    return {"object": "list", "data": models}


# ── Chat Completions Endpoint ───────────────────────────────────────────────

@router.post("/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible /v1/chat/completions endpoint.

    Accepts both streaming and non-streaming requests. The request body is
    passed through to the upstream almost verbatim (only ZenLite-only fields
    such as `provider` are removed, and the model prefix is stripped).
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body: expected an object")

    model = body.get("model", "deepseek-v4-flash-free")
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    provider_name = body.get("provider")

    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    # Upstream thinking-mode models (e.g. big-pickle) REQUIRE assistant
    # tool_calls messages to echo `reasoning_content` back. When the last
    # message is a tool result, a missing/empty field 400s with
    # "The `reasoning_content` in the thinking mode must be passed back".
    # Clients like Minis drop it, so inject an empty string — the upstream
    # accepts any string value (null is rejected).
    for m in messages:
        if (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and m.get("tool_calls")
            and not isinstance(m.get("reasoning_content"), str)
        ):
            m["reasoning_content"] = ""

    api_key = extract_api_key(request)
    provider, effective_key = get_provider(provider_name, api_key)

    # Resolve aliases (e.g. Copilot's default "gpt-4o") to a free model
    model = MODEL_ALIASES.get(model, model)

    # Strip the provider prefix so the upstream receives the raw model id
    upstream_model = strip_model_prefix(model)

    # Free-tier models never need (and reject) an API key — drop any key so
    # clients like Copilot can send any dummy string and still work.
    if model.startswith("oc/") or upstream_model in RAW_MODELS:
        effective_key = None

    # Everything the client sent, minus ZenLite-only fields, goes upstream
    # verbatim — tools, tool_choice, stream_options, ... all pass through.
    passthrough = {k: v for k, v in body.items() if k not in ZENLITE_ONLY_FIELDS}

    logger.info("Chat request: model=%s → upstream=%s stream=%s", model, upstream_model, stream)

    # ── Non-Streaming ────────────────────────────────────────────────────
    if not stream:
        try:
            return await provider.chat_completion(
                model=upstream_model,
                messages=messages,
                api_key=effective_key,
                extra_body=passthrough,
            )
        except UpstreamError as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)
        except Exception as e:
            logger.exception("Provider error")
            raise HTTPException(status_code=502, detail=f"Provider error: {str(e)}")

    # ── Streaming ────────────────────────────────────────────────────────
    async def stream_generator() -> AsyncGenerator[str, None]:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        try:
            async for chunk in provider.chat_completion_stream(
                model=upstream_model,
                messages=messages,
                api_key=effective_key,
                extra_body=passthrough,
            ):
                # Ensure proper OpenAI streaming format
                chunk.setdefault("id", completion_id)
                chunk.setdefault("object", "chat.completion.chunk")
                chunk.setdefault("created", created)
                chunk.setdefault("model", model)

                yield f"data: {json.dumps(chunk)}\n\n"

            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.exception("Stream error")
            error_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "error": {"message": str(e), "type": "provider_error"},
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
