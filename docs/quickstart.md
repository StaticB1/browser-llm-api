# Quickstart

An OpenAI-compatible API at `http://localhost:8081`, backed by your signed-in ChatGPT (the default, and the image provider) and Gemini sessions through browser automation. Use it from any project for text, code, HTML and images.

## 1. Start it

```bash
scripts/install-service.sh      # always on: a systemd --user service plus a watchdog
./serve.sh                      # or just run it in a terminal
./venv/bin/browser-llm health   # is it up?
```

Once per account, or whenever replies come back empty behind a sign-in wall:

```bash
DISPLAY=:1 ./venv/bin/browser-llm login chatgpt      # and/or: gemini
```

The commands below assume `./venv/bin` is on your `PATH`, or that you run them as `./venv/bin/browser-llm`.

## 2. Use it

The dashboard: open `http://localhost:8081/` for streaming chat with history, image creation with design presets, and a gallery. Attach images with the plus button, by pasting, or by dropping them in.

Text, code and HTML:

```bash
browser-llm ask "Explain CORS in two sentences"
browser-llm ask --stream "tell me a short story"
browser-llm ask --variant instant "one-line summary of TCP"     # faster than GPT-6 at High
browser-llm ask --out index.html --strip-fences \
  "Output ONLY a complete standalone HTML5 landing page for a bakery, inline CSS, no explanation"
browser-llm ask --image screenshot.png "What's broken in this layout?"
```

Image assets, cut to the exact size:

```bash
browser-llm image --prompt "flat vector fox mascot" --out avatar.png --square 256 --knockout-bg
browser-llm image --prompt "misty forest at dawn, lots of sky" --out hero.webp --width 1600 --height 900
browser-llm image --kind icon --prompt "a calendar with a check mark" --out icon.png --square 256
browser-llm image --prompt "misty forest at dawn" --out hero.webp --width 1600 --height 900 --variants 3
```

Exit code 3 means ChatGPT's image limit is reached; the message gives the reset time. Stop there.

A design, one image per section in one design system, then tokens, then a review of your build:

```bash
browser-llm design set --kind section --brief "what it is, for whom, the action" \
  --section "Hero: headline '...', button '...'" --section "Pricing: ..." --out designs/home
browser-llm design tokens designs/home/0*.png --out designs/home
browser-llm design review --design designs/home/01-hero.png --build shot.png --width 1440
```

The raw API:

```bash
curl -s localhost:8081/v1/images/generations -H 'Content-Type: application/json' \
  -d '{"prompt":"isometric 3d rocket icon","size":"1024x1024"}' | jq -r '.data[0].path'
curl -s localhost:8081/v1/images/edits -F image=@logo.png \
  -F 'prompt=the same logo as a flat vector on navy' | jq -r '.data[0].path'
```

From a coding agent, over MCP:

```bash
claude mcp add browser-llm -- "$PWD/mcp.sh"
```

From any Python project, with the standard OpenAI SDK:

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8081/v1", api_key="local")
print(c.chat.completions.create(model="chatgpt-browser",
      messages=[{"role": "user", "content": "hi"}]).choices[0].message.content)
```

Or the bundled client, which needs only the standard library: `from browser_llm.client import ask`.

## Good to know

- `model` picks the backend: `chatgpt-browser` (default) or `gemini-browser`. `IMAGE_PROVIDERS` decides which may make images.
- ChatGPT's model is GPT-6 at High unless `CHATGPT_MODEL` and `CHATGPT_EFFORT` say otherwise. One request can choose with `model_variant`, and each answer reports `model_used`.
- Limits: on ChatGPT Plus the page's counter allowed 120 images per 24 hours, measured in October 2026. A reached limit comes back as HTTP 429 with the reset time.
- One request at a time per provider: calls queue; ChatGPT and Gemini run alongside each other. A one-line answer at High took 17 to 23 seconds and an image about a minute, so set generous client timeouts (the bundled client waits 440 seconds).
- Empty replies usually mean the session expired: sign in again. [Running it](running.md#troubleshooting) has the rest.
- Check generated code: the answer is read from the rendered page, so a very long code block can occasionally lose a character.
