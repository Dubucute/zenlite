"""Minimal test for gateway streaming requests."""

import asyncio
import httpx


async def test_stream():
    body = {
        "model": "oc/mimo-v2.5-free",
        "messages": [{"role": "user", "content": "Say hello in one word"}],
        "stream": True,
        "provider": "opencode_free",
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                "http://127.0.0.1:8100/v1/chat/completions",
                json=body,
            ) as resp:
                print("status", resp.status_code)
                async for line in resp.aiter_lines():
                    print(line)
                    if line.startswith("data:") and "[DONE]" in line:
                        break
    except Exception as e:
        print("ERROR:", e)


async def main():
    await test_stream()


if __name__ == "__main__":
    asyncio.run(main())
