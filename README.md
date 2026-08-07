# ZenLite

ZenLite is a lightweight OpenAI-compatible gateway for OpenCode Free and OpenCode Zen providers with proxy rotation and a simple dashboard.

## Features
- OpenAI-compatible `/v1/chat/completions`
- Full OpenAI-protocol passthrough — `tools` / `tool_choice` / `tool_calls`, `functions`, `response_format`, `stream_options`, ... are forwarded verbatim
- Works with tool-calling clients like **GitHub Copilot agent mode**
- Free (`oc/`) and Zen (`opencode/`) model prefixes
- Direct-first proxy fallback
- Streaming support
- Simple dashboard UI

## Run locally
```bash
pip install -r requirements.txt
python run.py
```

Then open http://127.0.0.1:8100/

## GitHub Copilot (free models)

Point Copilot at ZenLite as a custom OpenAI-compatible endpoint. Use any `oc/`
model — the API key can be any dummy string (ZenLite drops it for free-tier
models), and common model names like `gpt-4o` are auto-mapped to a free model.

VS Code `settings.json`:

```json
{
  "github.copilot.chat.openai.compatible.models": {
    "oc/deepseek-v4-flash-free": {
      "name": "OpenCode Free (DeepSeek V4 Flash)",
      "url": "http://127.0.0.1:8100/v1/chat/completions",
      "apiKey": "free",
      "streaming": true
    }
  }
}
```

Tool calling works out of the box, so Copilot agent mode can run terminal
commands and edit files.

## Production / Deployment

Start with multiple workers (auto-reload is disabled when `workers > 1`):

```bash
ZENLITE_HOST=0.0.0.0 ZENLITE_WORKERS=4 python run.py
```

- `ZENLITE_WORKERS` — set to roughly 2× your CPU cores. Each worker is an
  independent process, so concurrent clients are served in parallel.
- `ZENLITE_HOST=0.0.0.0` — bind all interfaces so other machines (or a
  reverse proxy) can reach the gateway.
- `ZENLITE_RELOAD=1` — force dev auto-reload (single worker only).
- Run behind a reverse proxy (nginx/Caddy) for TLS.

Environment variables: `ZENLITE_HOST`, `ZENLITE_PORT`, `ZENLITE_WORKERS`,
`ZENLITE_RELOAD`.
