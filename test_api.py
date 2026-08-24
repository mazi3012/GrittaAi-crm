"""Quick smoke-test: text chat + vision model via OpenRouter."""
import os, sys, io, base64
from dotenv import load_dotenv
load_dotenv()

import requests
from PIL import Image, ImageDraw

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
MODEL        = os.getenv("MODEL",        "stealth/ox-alpha")
VISION_MODEL = os.getenv("VISION_MODEL", "stealth/ox-alpha")
URL = "https://openrouter.ai/api/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "HTTP-Referer":  "http://localhost",
    "X-Title":       "GrettaAI",
    "Content-Type":  "application/json",
}

failed = False

# ------------------------------------------------------------------
# TEST 1 – plain text chat
# ------------------------------------------------------------------
print("=== TEST 1: Text Chat ===")
r = requests.post(URL, json={
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a helpful CRM assistant."},
        {"role": "user",   "content": "Say hello in one short sentence."}
    ]
}, headers=headers, timeout=60)

print(f"HTTP {r.status_code}")
if r.status_code == 200:
    d = r.json()
    if isinstance(d.get("error"), dict):
        print(f"FAIL (upstream error): {d['error']}")
        failed = True
    else:
        reply = d["choices"][0]["message"].get("content") or "(empty response)"
        print(f"PASS  => {reply[:150]}")
else:
    print(f"FAIL  => {r.text[:300]}")
    failed = True

# ------------------------------------------------------------------
# TEST 2 – vision model with base64 image
# ------------------------------------------------------------------
print()
print("=== TEST 2: Vision Model ===")
img = Image.new("RGB", (300, 80), color="white")
ImageDraw.Draw(img).text((10, 28), "Invoice Total: 500 USD", fill="black")
buf = io.BytesIO()
img.save(buf, format="JPEG", quality=85)
b64 = base64.b64encode(buf.getvalue()).decode()

r2 = requests.post(URL, json={
    "model": VISION_MODEL,
    "messages": [{"role": "user", "content": [
        {"type": "text",      "text": "What text do you see in this image?"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
    ]}]
}, headers=headers, timeout=90)

print(f"HTTP {r2.status_code}")
if r2.status_code == 200:
    d2 = r2.json()
    if isinstance(d2.get("error"), dict):
        print(f"FAIL (upstream error): {d2['error']}")
        failed = True
    else:
        reply2 = d2["choices"][0]["message"].get("content") or "(empty response)"
        print(f"PASS  => {reply2[:200]}")
else:
    print(f"FAIL  => {r2.text[:300]}")
    failed = True

# ------------------------------------------------------------------
print()
if failed:
    print("One or more tests FAILED.")
    sys.exit(1)
else:
    print("All tests PASSED.")
