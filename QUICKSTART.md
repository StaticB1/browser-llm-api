# Quickstart

An OpenAI-compatible API at **`http://localhost:8081`** backed by ChatGPT / Gemini
(browser automation). Use it from any project for text, code/HTML, and images.

## 1. Start it

**Always-on (recommended)** — install once; auto-starts with your desktop session:
```bash
./install-service.sh
```
**Or just run it in a terminal:**
```bash
./serve.sh
```
Check it's up: `curl -s localhost:8081/v1/models`

> One-time per account (only if replies come back empty / a login wall shows):
> `DISPLAY=:1 ./venv/bin/python login.py chatgpt`   (and/or `login.py gemini`)

## 2. Use it

**Text / code / HTML** (via the bundled client):
```bash
./client.py "Explain CORS in two sentences"
./client.py --stream "tell me a short story"
# write an HTML page straight to a file (strips ``` fences):
./client.py --out index.html --strip-fences \
  "Output ONLY a complete standalone HTML5 landing page for a bakery, inline CSS, no explanation"
```

**Images** (raw):
```bash
curl -s localhost:8081/v1/images/generations -H 'Content-Type: application/json' \
  -d '{"prompt":"isometric 3d rocket icon, vibrant"}' | jq -r '.data[0].path'
```

**Website image assets** (auto resize / crop / favicon / transparency):
```bash
./venv/bin/python gen_asset.py --prompt "flat vector fox mascot, solid white background" \
  --out avatar.png --square 256 --knockout-bg
./venv/bin/python gen_asset.py --prompt "misty forest at dawn, cinematic, lots of sky" \
  --out hero.webp --width 1600 --height 900
```

**From any Python project** (standard OpenAI SDK — no code changes, just the base URL):
```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8081/v1", api_key="local")
print(c.chat.completions.create(model="chatgpt-browser",
      messages=[{"role":"user","content":"hi"}]).choices[0].message.content)
```
Any OpenAI-based tool works the same way — set base URL `http://localhost:8081/v1`,
key `local`, model `chatgpt-browser` or `gemini-browser`.

## Good to know

- **`model`** picks the backend: `gemini-browser` or `chatgpt-browser`. Default is `gemini-browser` (override with `DEFAULT_PROVIDER`).
- **ChatGPT image gen needs a real display.** The service runs headless by default; `./mode.sh visible` switches it to the real display (enables ChatGPT images), `./mode.sh headless` switches back. Gemini and all text work headless.
- **One request at a time per provider** — calls queue, they don't run in parallel. Latency is real:
  text ~10–40s, images ~30s–4 min. Set generous client timeouts (the bundled client defaults to 440s).
- **Empty replies?** the session expired → re-run `login.py <provider>` (see above).
- **Verify generated code/HTML.** The answer is scraped from the rendered page, so long code
  blocks can very occasionally drop or mangle a character. Fine for prose; eyeball generated code.
- Manage the service: `systemctl --user {status,restart,stop} browser-llm-api` ·
  logs: `journalctl --user -u browser-llm-api -f`
