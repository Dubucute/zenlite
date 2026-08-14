import httpx
import json

url = "http://127.0.0.1:8100/v1/chat/completions"
payload = {
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "Say hello"}],
    "stream": False
}

try:
    r = httpx.post(url, json=payload, timeout=30)
    print("Status:", r.status_code)
    print("Headers:", r.headers.get("content-type"))
    print("Response:", r.text[:500])
except Exception as e:
    print("Error:", e)
