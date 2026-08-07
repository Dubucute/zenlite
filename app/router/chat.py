"""
OpenAI-Compatible Chat Router
Handles /v1/chat/completions with both streaming and non-streaming.
"""

import json
import time
import uuid
import logging
from typing import Optional, AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import PROVIDERS, strip_model_prefix
from app.providers.opencode_free import opencode_free_provider
from app.providers.opencode_zen import opencode_zen_provider
from app.providers.base import UpstreamError

logger = logging.getLogger("zenlite.router")

router = APIRouter(prefix="/v1", tags=["OpenAI-Compatible"])


# ── Request / Response Models ────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    stop: Optional[list[str]] = None
    n: Optional[int] = None
    # ZenLite-specific: which provider to use
    provider: Optional[str] = None  # "opencode_free" or "opencode_zen"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = "stop"


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[ChatChoice]
    usage: UsageInfo = Field(default_factory=UsageInfo)


# ── Provider Selection Logic ─────────────────────────────────────────────────

def get_provider(provider_name: Optional[str] = None, api_key: Optional[str] = None):
    """
    Determine which provider to use.

    Priority:
      1. Explicit provider field in request body
      2. If api_key is provided → OpenCode Zen
      3. Default → OpenCode Free (no auth)
    """
    if provider_name:
        if provider_name == "opencode_free":
            return opencode_free_provider, None
        elif provider_name == "opencode_zen":
            if not api_key:
                raise HTTPException(
                    status_code=400,
                    detail="opencode_zen provider requires an API key",
                )
            return opencode_zen_provider, api_key
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider: {provider_name}. Use 'opencode_free' or 'opencode_zen'.",
            )

    # Auto-detect from API key
    if api_key:
        return opencode_zen_provider, api_key

    # Default: free provider
    return opencode_free_provider, None


# ── API Key Extraction ──────────────────────────────────────────────────────

def extract_api_key(request: Request) -> Optional[str]:
    """
    Extract API key from Authorization header (Bearer token).
    Returns None if not present (which is fine for free provider).
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


# ── Models Endpoint ──────────────────────────────────────────────────────────

@router.get("/models")
async def list_models(request: Request):
    """List all available models across all providers."""
    api_key = extract_api_key(request)
    models = []
    for provider_id, provider_config in PROVIDERS.items():
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
    return {
        "object": "list",
        "data": models,
    }


# ── Chat Completions Endpoint ───────────────────────────────────────────────

@router.post("/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible /v1/chat/completions endpoint.

    Accepts both streaming and non-streaming requests.
    Routes to the appropriate provider based on API key / provider field.
    """
    # Parse request body
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # Extract fields
    model = body.get("model", "deepseek-v4-flash-free")
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    temperature = body.get("temperature")
    max_tokens = body.get("max_tokens")
    top_p = body.get("top_p")
    frequency_penalty = body.get("frequency_penalty")
    presence_penalty = body.get("presence_penalty")
    stop = body.get("stop")
    n = body.get("n")
    provider_name = body.get("provider")

    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    # Get API key from header
    api_key = extract_api_key(request)

    # Select provider
    provider, effective_key = get_provider(provider_name, api_key)

    # Strip model prefix for upstream (oc/big-pickle → big-pickle)
    upstream_model = strip_model_prefix(model)

    logger.info(
        "Chat request: model=%s → upstream=%s provider=%s stream=%s",
        model,
        upstream_model,
        provider.config.id,
        stream,
    )

    # ── Non-Streaming ────────────────────────────────────────────────────
    if not stream:
        try:
            result = await provider.chat_completion(
                model=upstream_model,
                messages=messages,
                stream=False,
                api_key=effective_key,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                stop=stop,
                n=n,
            )
            # Return as-is if it's already a proper response, else wrap it
            if isinstance(result, dict) and "choices" in result:
                return result
            # Wrap raw response
            return ChatCompletionResponse(
                model=model,
                choices=[ChatChoice(
                    message=ChatMessage(role="assistant", content=str(result))
                )],
            ).model_dump()
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
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                frequency_penalty=frequency_penalty,
                presence_penalty=presence_penalty,
                stop=stop,
                n=n,
            ):
                # Ensure proper OpenAI streaming format
                if "id" not in chunk:
                    chunk["id"] = completion_id
                if "object" not in chunk:
                    chunk["object"] = "chat.completion.chunk"
                if "created" not in chunk:
                    chunk["created"] = created
                if "model" not in chunk:
                    chunk["model"] = model

                yield f"data: {json.dumps(chunk)}\n\n"

            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.exception("Stream error")
            error_chunk = {
                "error": {
                    "message": str(e),
                    "type": "provider_error",
                }
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
