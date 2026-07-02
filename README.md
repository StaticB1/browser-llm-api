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
- **`gemini-api.service`** — systemd `--user` unit to run the server in the background.
- **`gen_asset.py`** — CLI to generate + post-process a website image asset (resize/crop/convert/favicon/transparency). Uses `/v1/images/generations`; needs Pillow.
- **`AGENT_IMAGE_GUIDE.md`** — instructions to hand an AI coding agent so it uses this API to generate site image assets.

## How it works

The server keeps **one persistent Chrome per provider** (profile in `gemini_profile/` / `chatgpt_profile/`, gitignored), started lazily on first use. On each request it opens the site, types the prompt, and reads the streamed answer out of the DOM — Gemini by walking the shadow DOM, ChatGPT from the plain-DOM `.markdown` of the last assistant turn. Completion is detected via a CDP network signal (the provider's streaming request finishing) with a DOM-stability fallback. Requests are serialized **per provider** by a lock, so Gemini and ChatGPT can run concurrently.

> **Note on ChatGPT:** the ChatGPT selectors are best-guess against the live UI (which changes often and sits behind Cloudflare/anti-bot) and may need tweaking. Automating chatgpt.com may also conflict with OpenAI's ToS — use accordingly.

## Requirements

- Google Chrome, Python 3.12
- Deps go in a **venv** (system Python is usually PEP-668 externally-managed): `python3.12 -m venv venv && ./venv/bin/pip install nodriver fastapi uvicorn pydantic`. Run with `./venv/bin/python server.py`.
- Chrome runs **non-headless** on purpose (the sites block true headless). The background service renders to a virtual **Xvfb** display (via `xvfb-run`), so no real monitor is needed. Re-auth uses a real display (e.g. `DISPLAY=:1`). Requires the `xvfb` package.
- **ChatGPT image generation needs a real GPU display** (see below) — it does not render under headless Xvfb.

## Run once (foreground)

```bash
python3 server.py            # serves http://localhost:8081/v1
# or the standalone prototype:
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

The unit runs the server under `xvfb-run`, so Chrome renders to an invisible virtual display — no window appears and no login session is required (with linger enabled it survives logout).

```bash
mkdir -p ~/.config/systemd/user
cp gemini-api.service ~/.config/systemd/user/
# edit ExecStart / DISPLAY / WorkingDirectory in the unit if your paths differ
systemctl --user daemon-reload
systemctl --user enable --now gemini-api.service
loginctl enable-linger "$USER"      # keep it running after logout
```

Manage it:

```bash
systemctl --user status gemini-api
systemctl --user restart gemini-api
journalctl --user -u gemini-api -f  # live logs (the server.log file stays empty; the journal is the log)
```

## Authentication (important)

**Each provider needs its own login**, stored in its own profile (`gemini_profile/` / `chatgpt_profile/`). When answers come back empty or you see a sign-in / "verify you're human" wall, that provider's session has expired.

**Re-auth must be done in the automation's own browser, on a real display.** The background service's Chrome is invisible (Xvfb), so you can't sign in there — the helper below opens a visible browser instead. Also, `nodriver` launches Chrome with `--password-store=basic`; a normal Chrome uses the system keyring, and cookies written by one cannot be decrypted by the other. So do **not** sign in with a plain `google-chrome` — use the helper:

```bash
systemctl --user stop gemini-api
DISPLAY=:1 python3 login.py gemini    # or: chatgpt — a Chrome window opens; sign in; it auto-detects and closes
systemctl --user start gemini-api
```

## Image generation

Both providers generate images from a natural prompt. Generated images are saved to disk **and** served over HTTP, so you get a file and a link.

**Storage path** — set `GEMINI_IMAGE_DIR` in the service unit (`Environment=GEMINI_IMAGE_DIR=…`); default `/home/b/Pictures/gemini`. The folder is created on startup and mounted at `/images/<file>`. `GEMINI_PUBLIC_URL` (default `http://localhost:8081`) is the base used to build the returned links — change it if you reach the server from another host. If the folder isn't writable, saving is skipped and it falls back to inline base64 / `data:` URLs.

- **In chat** — a prompt like "generate an image of …" returns the image inline in the assistant message as markdown pointing at the served file: `![...](http://localhost:8081/images/gemini_….png)`. Gemini replies image-only (its accompanying text is internal "thinking"); ChatGPT keeps its caption text and appends the image.
- **Images endpoint** — OpenAI-style `POST /v1/images/generations` (`model` selects the provider):

```bash
curl http://localhost:8081/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-browser","prompt":"a red bicycle on a beach at sunset"}'
# -> {"created": ..., "data": [{
#      "b64_json": "<base64>",
#      "url":  "http://localhost:8081/images/gemini_….png",
#      "path": "/home/b/Pictures/gemini/gemini_….png"
#    }]}
```

`n` and `size` are accepted but ignored — the provider decides count and dimensions. Internally the server waits for the `<img>` to finish rendering, then reads it to base64 from inside the page, writes it to `GEMINI_IMAGE_DIR`, and returns base64 + URL + path. The endpoint returns **501** if the provider can't generate images, **502** if it returned none.

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
