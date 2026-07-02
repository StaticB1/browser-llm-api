# Browser LLM API

Drives a **chat web UI** through an automated Chrome browser ([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and exposes it as an **OpenAI-compatible API**. No official API key — it uses a logged-in web session. Two providers, chosen per-request by the OpenAI **`model`** field:

| `model` | site | profile | images |
|---------|------|---------|--------|
| `gemini-browser` | gemini.google.com | `gemini_profile/` | ✅ |
| `chatgpt-browser` | chatgpt.com | `chatgpt_profile/` | ✅ |

An unknown/absent `model` falls back to `DEFAULT_PROVIDER` (env, default `gemini-browser`).

- **`server.py`** — FastAPI server on port **8081** (`/v1/chat/completions` streaming + non-streaming, `/v1/images/generations`, `/v1/models`, `/images/<file>`).
- **`providers/`** — one adapter per site behind a common `Provider` interface (`gemini.py`, `chatgpt.py`); add a backend by adding a provider, not by touching `server.py`.
- **`login.py`** — interactive re-auth helper: `python login.py gemini|chatgpt` (see below).
- **`gemini_bot.py`** — standalone single-prompt Gemini prototype (hardcoded prompt → saves the answer).
- **`serve.sh`** / **`install-service.sh`** / **`browser-llm-api.service.template`** — run the server (venv + display auto-detect), and install it as a background `systemd --user` service generated for this clone.
- **`gen_asset.py`** — CLI to generate + post-process a website image asset (resize/crop/convert/favicon/transparency). Uses `/v1/images/generations`; needs Pillow.
- **`AGENT_IMAGE_GUIDE.md`** — instructions to hand an AI coding agent so it uses this API to generate site image assets.

## How it works

The server keeps **one persistent Chrome per provider** (profile in `gemini_profile/` / `chatgpt_profile/`, gitignored), started lazily on first use. On each request it opens the site, types the prompt, and reads the streamed answer out of the DOM — Gemini by walking the shadow DOM, ChatGPT from the plain-DOM `.markdown` of the last assistant turn. Completion is detected via a CDP network signal (the provider's streaming request finishing) with a DOM-stability fallback. Requests are serialized **per provider** by a lock, so Gemini and ChatGPT can run concurrently.

> **Note on ChatGPT:** the ChatGPT selectors are best-guess against the live UI (which changes often and sits behind Cloudflare/anti-bot) and may need tweaking. Automating chatgpt.com may also conflict with OpenAI's ToS — use accordingly.

## Requirements

- Google Chrome, Python 3.12
- Deps go in a **venv** (system Python is usually PEP-668 externally-managed): `python3.12 -m venv venv && ./venv/bin/pip install nodriver fastapi uvicorn pydantic`. Run with `./venv/bin/python server.py`.
- Chrome runs **non-headless** on purpose (the sites block true headless). `serve.sh` auto-detects a display: it uses a real `$DISPLAY` if present (needed for ChatGPT image gen), otherwise falls back to headless **Xvfb** (needs the `xvfb` package; Gemini works, ChatGPT images don't). Re-auth uses a real display (e.g. `DISPLAY=:1`).
- **ChatGPT image generation needs a real GPU display** (see below) — it does not render under headless Xvfb.

## Run once (foreground)

```bash
./serve.sh                   # serves http://localhost:8081/v1 (venv + display auto-detect)
# or the standalone Gemini prototype:
python3 gemini_bot.py
```

## Selecting a provider

Set the OpenAI `model` field per request:

```bash
curl http://localhost:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"chatgpt-browser","messages":[{"role":"user","content":"hello"}]}'
# swap "chatgpt-browser" for "gemini-browser" to hit Gemini instead
```

`GET /v1/models` lists both. Set `DEFAULT_PROVIDER=chatgpt-browser` to change the fallback.

## Run in the background (systemd --user)

`install-service.sh` sets up a venv, installs deps, and generates a `systemd --user` unit pointing at **this** clone (no hardcoded paths). It auto-starts, auto-restarts, and survives logout (linger).

```bash
./install-service.sh
```

**Display mode** — on a machine with a real display, toggle whether Chrome runs visibly (persists across restarts):

```bash
./mode.sh            # show current mode
./mode.sh visible    # run on the real display — enables ChatGPT image gen (a window shows)
./mode.sh headless   # invisible Xvfb (default) — Gemini images ok, ChatGPT images off
```

Manage it:

```bash
systemctl --user status browser-llm-api
systemctl --user restart browser-llm-api
journalctl --user -u browser-llm-api -f  # live logs (server.log stays empty; the journal is the log)
```

## Authentication (important)

**Each provider needs its own login**, stored in its own profile (`gemini_profile/` / `chatgpt_profile/`). When answers come back empty or you see a sign-in / "verify you're human" wall, that provider's session has expired.

**Re-auth must be done in the automation's own browser, on a real display.** The background service's Chrome is invisible (Xvfb), so you can't sign in there — the helper below opens a visible browser instead. Also, `nodriver` launches Chrome with `--password-store=basic`; a normal Chrome uses the system keyring, and cookies written by one cannot be decrypted by the other. So do **not** sign in with a plain `google-chrome` — use the helper:

```bash
systemctl --user stop browser-llm-api
DISPLAY=:1 ./venv/bin/python login.py gemini    # or: chatgpt — a Chrome window opens; sign in; it auto-detects and closes
systemctl --user start browser-llm-api
```

## Image generation

Both providers generate images from a natural prompt. Generated images are saved to disk **and** served over HTTP, so you get a file and a link.

**Storage path** — `GEMINI_IMAGE_DIR` (env; `serve.sh` defaults it to `~/Pictures/gemini`). The folder is created on startup and mounted at `/images/<file>`. `GEMINI_PUBLIC_URL` (default `http://localhost:8081`) is the base used to build the returned links — change it if you reach the server from another host. If the folder isn't writable, saving is skipped and it falls back to inline base64 / `data:` URLs.

- **In chat** — a prompt like "generate an image of …" returns the image inline in the assistant message as markdown pointing at the served file: `![...](http://localhost:8081/images/gemini_….png)`. Gemini replies image-only (its accompanying text is internal "thinking"); ChatGPT keeps its caption text and appends the image.
- **Images endpoint** — OpenAI-style `POST /v1/images/generations` (`model` selects the provider):

```bash
curl http://localhost:8081/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-browser","prompt":"a red bicycle on a beach at sunset"}'
# -> {"created": ..., "data": [{
#      "b64_json": "<base64>",
#      "url":  "http://localhost:8081/images/gemini_….png",
#      "path": "~/Pictures/gemini/gemini_….png"
#    }]}
```

`n` and `size` are accepted but ignored — the provider decides count and dimensions. Internally the server waits for the `<img>` to finish rendering, then reads it to base64 from inside the page, writes it to `GEMINI_IMAGE_DIR`, and returns base64 + URL + path. The endpoint returns **501** if the provider can't generate images, **502** if it returned none.

For **long asset runs**, the provider's browser is **auto-recycled** every few image gens (`BROWSER_RECYCLE_AFTER_IMAGES`, default 3) — its renderer bloats after ~4–5 heavy image generations and starts timing out, so a fresh browser is spun up automatically before that happens. (Clients should still treat a 502/timeout as "check `GEMINI_IMAGE_DIR` for the newest file" — the image is written to disk before the response returns.)

> **⚠️ ChatGPT image generation needs a GPU / real display.** GPT-image renders on a `<canvas>` that stalls under headless Xvfb, so the Xvfb systemd service can't produce ChatGPT images (text is fine). Run the server on a real display for ChatGPT images: `DISPLAY=:1 ./venv/bin/python server.py`. It's also slow on the free tier (30s–4 min). Gemini images work under Xvfb.

**Generating website assets** — the image endpoint returns one landscape image with no transparency, so for real assets (hero images, backgrounds, textures, avatars, favicons) use **`gen_asset.py`**, which generates then post-processes with Pillow (resize/crop/convert/favicon/transparency). It calls `/v1/images/generations` with no `model`, so it uses `DEFAULT_PROVIDER`. See **`AGENT_IMAGE_GUIDE.md`** for a ready-to-hand instruction set for an AI coding agent (its Gemini-specific notes — fixed output size, ✦ watermark — apply to the Gemini provider; ChatGPT returns a larger PNG with no watermark).

```bash
./venv/bin/python gen_asset.py --prompt "friendly cartoon fox mascot, flat vector, centered, solid white background" \
    --out public/avatar.png --square 256 --knockout-bg
```

## Caveats

- **Fragile by nature** — each provider depends on its site's live DOM/selectors (Gemini: `model-response`; ChatGPT: `[data-message-author-role="assistant"]`, `[data-testid="send-button"]`). A UI change can break extraction. ChatGPT additionally sits behind Cloudflare/anti-bot.
- **One request at a time per provider** (per-provider lock); Gemini and ChatGPT run concurrently, callers to the same provider queue.
- **`usage` token counts are approximate** (word-split, not a real tokenizer).
- A hard crash can leave a stale `<profile>/SingletonLock`; the unit clears both providers' locks on start (`ExecStartPre`).
