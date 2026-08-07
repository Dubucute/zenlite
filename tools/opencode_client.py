"""opencode_client.py — simple CLI to call OpenCode via the OpenAI Python SDK.

Uses the OpenAI SDK (v1+) with a client pointed at `OPENAI_BASE_URL`
(defaults to OpenCode Zen). Supports tool-calling via `--tools`.

Usage examples:
  export OPENAI_API_KEY=sk-...
  python tools/opencode_client.py --model mimo-v2.5-free --prompt "Say hello"

Tool-calling example (auto):
  python tools/opencode_client.py --model mimo-v2.5-free --prompt "Get weather" \
    --tools '[{"type":"function","function":{"name":"get_weather","description":"Get weather","parameters":{"type":"object","properties":{"location":{"type":"string"}},"required":["location"]}}}]' \
    --tool_choice auto
"""

import os
import sys
import json
import argparse
from openai import OpenAI

DEFAULT_BASE = os.getenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--api_key", help="API key (falls back to OPENAI_API_KEY env)")
    p.add_argument("--base", default=DEFAULT_BASE, help="OpenAI API base URL (defaults to OPENAI_BASE_URL)")
    p.add_argument("--model", default="mimo-v2.5-free")
    p.add_argument("--prompt", required=True)
    p.add_argument("--tools", help="JSON array of tool specs (OpenAI `tools` format)")
    p.add_argument("--tool_choice", help="tool_choice value (e.g. 'auto' or '{\"type\":\"function\",\"function\":{\"name\":...}}')")
    p.add_argument("--stream", action="store_true")
    return p.parse_args()


def _json_or_raw(value: str):
    try:
        return json.loads(value)
    except Exception:
        return value


def main():
    args = parse_args()
    api_key = args.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("Error: API key required via --api_key or OPENAI_API_KEY env var", file=sys.stderr)
        sys.exit(2)

    client = OpenAI(base_url=args.base, api_key=api_key)

    messages = [{"role": "user", "content": args.prompt}]

    params = {
        "model": args.model,
        "messages": messages,
    }
    if args.tools:
        params["tools"] = _json_or_raw(args.tools)
    if args.tool_choice:
        params["tool_choice"] = _json_or_raw(args.tool_choice)

    if args.stream:
        print(f"Streaming response from {args.base}")
        stream = client.chat.completions.create(stream=True, **params)
        for chunk in stream:
            print(json.dumps(chunk.model_dump(), ensure_ascii=False))
    else:
        resp = client.chat.completions.create(**params)
        print(json.dumps(resp.model_dump(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
