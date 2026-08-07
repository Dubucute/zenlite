# ZenLite

ZenLite is a lightweight OpenAI-compatible gateway for OpenCode Free and OpenCode Zen providers with proxy rotation and a simple dashboard.

## Features
- OpenAI-compatible `/v1/chat/completions`
- Free provider with `oc/` prefix support
- Zen provider with `opencode/` prefix support
- Direct-first proxy fallback
- Streaming support
- Simple dashboard UI

## Run locally
```bash
pip install -r requirements.txt
python run.py
```

Then open http://127.0.0.1:8100/
