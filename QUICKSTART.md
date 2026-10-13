# Quickstart

An OpenAI-compatible API at **`http://localhost:8081`** backed by ChatGPT (the default, and the
only image provider on our install) and Gemini (text), through browser automation. Use it from
any project for text, code/HTML, and images.

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

**Web UI (easiest)** — open **`http://localhost:8081/`** in a browser: streaming chat,
image generation, and a gallery of everything generated so far, with live provider status.
Attach images with the 📎 button (or just paste / drag-drop them) to ask about a screenshot,
or to edit an existing image.

**Text / code / HTML** (via the bundled client):
```bash
./client.py "Explain CORS in two sentences"
./client.py --stream "tell me a short story"
./client.py --variant instant "one-line summary of TCP"   # faster than the default GPT-6 at High
# write an HTML page straight to a file (strips ``` fences):
./client.py --out index.html --strip-fences \
  "Output ONLY a complete standalone HTML5 landing page for a bakery, inline CSS, no explanation"
```

**Images** (raw):
```bash
curl -s localhost:8081/v1/images/generations -H 'Content-Type: application/json' \
  -d '{"prompt":"isometric 3d rocket icon","size":"1024x1024"}' | jq -r '.data[0].path'
```

**Send an image IN** (vision, or image-to-image editing):
```bash
./client.py --image screenshot.png "What's broken in this layout?"

# edit an existing image (multipart, same shape as OpenAI's images.edit)
curl -s localhost:8081/v1/images/edits -F image=@logo.png \
  -F 'prompt=same logo, flat vector, navy background' | jq -r '.data[0].path'
```

**Website image assets** (exact size, crop, favicon, transparency; `--ref` restyles an
existing asset instead of starting from scratch):
```bash
./venv/bin/python gen_asset.py --prompt "flat vector fox mascot" \
  --out avatar.png --square 256 --knockout-bg
./venv/bin/python gen_asset.py --prompt "misty forest at dawn, lots of sky" \
  --out hero.webp --width 1600 --height 900
# three alternatives side by side (hero-v1..v3.webp + hero-variants.png), originals kept:
./venv/bin/python gen_asset.py --prompt "misty forest at dawn, lots of sky" \
  --out hero.webp --width 1600 --height 900 --variants 3 --keep-original
```
ChatGPT is asked for the size and a transparent background in words, then the file is cropped
to the exact pixels. Exit code 3 means ChatGPT's image limit is reached and the message gives
the reset time: stop there and say so; don't retry before it.

**From a coding agent** (MCP — the agent gets real tools instead of a shell command):
```bash
claude mcp add browser-llm -- "$PWD/venv/bin/python" "$PWD/mcp_server.py"
```
Then ask it to look at a screenshot or make an asset: `ask` takes images and can write its answer
straight to a file, `generate_image` writes the asset (or `variants` of it) and returns the path,
the original's size and the model that drew it. Same config shape works in Claude Desktop, Cursor
and Zed — see the README.

**From any Python project** (standard OpenAI SDK — no code changes, just the base URL):
```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8081/v1", api_key="local")
print(c.chat.completions.create(model="chatgpt-browser",
      messages=[{"role":"user","content":"hi"}]).choices[0].message.content)
```
Any OpenAI-based tool works the same way — set base URL `http://localhost:8081/v1`,
key `local`, model `chatgpt-browser` (or `gemini-browser` for text).

## Good to know

- **`model`** picks the backend: `chatgpt-browser` (default, override with `DEFAULT_PROVIDER`) or
  `gemini-browser`. Images go to ChatGPT only on our install (Eben's order, 13 Oct 2026):
  `IMAGE_PROVIDERS=chatgpt-browser`, so a Gemini image request gets 403.
- **ChatGPT's model** is GPT-6 at High unless `CHATGPT_MODEL` / `CHATGPT_EFFORT` say otherwise;
  one request can pick with `model_variant` (`"instant"`, `"GPT-5.6 Sol"`, `"GPT-6/medium"`).
  Each answer reports `model_used`, read from the conversation's record on chatgpt.com.
- **Plus limits** (measured 13 Oct 2026): 120 images per 24 hours, 80 file uploads per 3 hours.
  Our install keeps 10 images back for Eben's own chats. A limit reached comes back as HTTP 429
  with the reset time (README, "ChatGPT Plus limits").
- **ChatGPT image gen needs an X display**, not Chrome's headless mode. The service's Xvfb
  display `:98` works (8 images on 13 Oct 2026, 48–90 s each).
- **One request at a time per provider** — calls queue, they don't run in parallel. ChatGPT and
  Gemini do run concurrently with each other. Two conversations inside one provider is not
  something browser automation can do: it was tried and the second one never completes. Latency is
  real: on 13 Oct 2026 a one-line answer at High took 17–23 s and an image 48–90 s on Plus; long
  answers take longer. Set generous client timeouts
  (the bundled client defaults to 440 s).
- **Empty replies?** the session expired → re-run `login.py <provider>` (see above).
- **Verify generated code/HTML.** The answer is scraped from the rendered page, so long code
  blocks can very occasionally drop or mangle a character. Fine for prose; eyeball generated code.
- Manage the service: `systemctl --user {status,restart,stop} browser-llm-api` ·
  logs: `journalctl --user -u browser-llm-api -f`
