# Browser LLM API

**Turn the AI you already use in your browser into a local, OpenAI-compatible API — no API keys, no per-token bills.**

Browser LLM API drives a real, logged-in **ChatGPT** and **Gemini** session through an automated Chrome ([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and re-exposes it as the same HTTP API your tools already speak. Point any OpenAI SDK, script, or app at `http://localhost:8081/v1` and get **streaming chat *and* image generation** — powered by your existing subscription (or free tier), running entirely on your own machine.

![MIT License](https://img.shields.io/badge/license-MIT-green) ![Python 3.12](https://img.shields.io/badge/python-3.12-blue) ![Providers: ChatGPT · Gemini](https://img.shields.io/badge/providers-ChatGPT%20%C2%B7%20Gemini-8b5cf6)

```python
# It's the OpenAI SDK you already know — just change the base URL.
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="not-needed")

client.chat.completions.create(
    model="chatgpt-browser",                       # or "gemini-browser"
    messages=[{"role": "user", "content": "Write a haiku about local-first AI."}],
)
```

## Why you might want this

- 🔑 **No API keys, no metered billing.** It rides your normal logged-in web session, so you use the plan you already pay for — or the free tier — instead of a separate paid API.
- 🔌 **Drop-in OpenAI compatibility.** `/v1/chat/completions` (streaming + non-streaming) and `/v1/images/generations`, with the same request/response shapes. Existing OpenAI clients, LangChain, scripts, and dev tools "just work."
- 🎨 **Chat *and* images.** Both providers generate images from a prompt; results come back as a link **and** a saved file.
- 🧩 **Two providers, one field.** Switch between ChatGPT and Gemini per request via the `model` field. Run both at once — they're independent.
- 🏠 **Local & private to your LAN.** Everything runs on your box; nothing goes to a third-party API broker.
- 🖥️ **Use it four ways** (below): REST API, a web dashboard, an embeddable chat widget, or a **native Linux desktop app**.

> **The honest catch:** this automates logged-in sessions on sites that have **no official API for this** — so it's inherently fragile (a site UI change can break it), may conflict with each provider's Terms of Service, and is meant for **personal / experimental** use on your own account. See the [Disclaimer](#disclaimer).

## What's in the box

| Surface | What it is | Where |
|---------|-----------|-------|
| 🔌 **OpenAI-compatible API** | `/v1/chat/completions` + `/v1/images/generations` + `/v1/models` | `http://localhost:8081/v1` |
| 🖥️ **Web dashboard** | Streaming chat, image generation, a gallery, and a live status tab — single file, no build step | `http://localhost:8081/` |
| 💬 **Embeddable widget** | One `<script>` tag drops a floating chat bubble onto any page on your network | `/widget.js` (demo at `/demo`) |
| 🐧 **Native desktop app** | GTK tray widget that generates images for your active **VS Code project**, + a full Chat / Images / Gallery / Status window (Linux) | [`desktop/`](desktop/README.md) |

| `model` | Backend | Images |
|---------|---------|--------|
| `chatgpt-browser` | chatgpt.com | ✅ |
| `gemini-browser` | gemini.google.com | ✅ |

An unknown/absent `model` falls back to `DEFAULT_PROVIDER` (env, default `gemini-browser`).

## Quick start

```bash
# 1. Install into a virtualenv (system Python is usually externally-managed).
python3.12 -m venv venv
./venv/bin/pip install -e ".[assets]"     # editable install; [assets] adds Pillow for gen_asset.py

# 2. Start the server (auto-detects a display; falls back to headless Xvfb).
./serve.sh                                 # → http://localhost:8081

# 3. Sign in once per provider (a real Chrome window opens — log in, it closes itself).
DISPLAY=:1 ./venv/bin/python login.py chatgpt      # and/or: gemini

# 4. Use it.
curl http://localhost:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"chatgpt-browser","messages":[{"role":"user","content":"hello!"}]}'
```

Then open **`http://localhost:8081/`** for the web dashboard, or run the [native desktop app](#native-desktop-app). You need **Google Chrome** and **Python 3.12** installed; on a headless box also install the system `xvfb` package.

> First answer empty, or you hit a sign-in / "verify you're human" wall? That provider's session just needs a fresh login — see [Authentication](#authentication).

## How it works

The server keeps **one persistent Chrome per provider** (profile in `gemini_profile/` / `chatgpt_profile/`, gitignored), started lazily on first use. On each request it opens the site, types the prompt, and reads the streamed answer back out of the DOM — Gemini by walking the shadow DOM, ChatGPT from the plain-DOM `.markdown` of the last assistant turn. Completion is detected via a CDP network signal (the provider's streaming request finishing) with a DOM-stability fallback. Requests are serialized **per provider** by a lock, so Gemini and ChatGPT can run concurrently while callers to the *same* provider queue.

> **Note on ChatGPT:** the ChatGPT selectors are best-guess against the live UI (which changes often and sits behind Cloudflare/anti-bot) and may need tweaking. Automating chatgpt.com may also conflict with OpenAI's ToS — use accordingly.

## Web UI

Open **`http://localhost:8081/`** in a browser. Four tabs:

- **Chat** — pick a provider, chat with streaming replies and multi-turn context; optional system prompt; generated images render inline. Enter sends, Shift+Enter for a newline.
- **Image** — one-line prompt → image, with an elapsed-time indicator (free-tier image gen takes 30s–4min).
- **Gallery** — every image saved to `GEMINI_IMAGE_DIR`, newest first, filterable by provider.
- **Status** — live per-provider telemetry (requests, errors, avg + last latency, images-until-recycle countdown), server info (version, uptime, display, image dir), and a copy-paste **embed snippet** with a "Preview widget on this page" button.

The header shows each provider's live state (off / idle / busy — click the pills to jump to Status), and the footer shows the version, server uptime, and where images are being saved. The UI talks to the same JSON API documented below (`/api/status` and `/api/gallery` back the status bar and gallery).

## Embeddable widget

Drop a floating chat bubble onto **any** page on your network with one line — it talks to this server's `/v1/chat/completions`:

```html
<script src="http://localhost:8081/widget.js"></script>
```

The widget is self-contained and **Shadow-DOM isolated** (host-page CSS can't leak in or out). It auto-discovers the API base from its own script URL, so the host page can be on any origin/port — the server already sends open CORS headers. See a live demo at **`http://localhost:8081/demo`**.

Configure with `data-*` attributes on the script tag:

| attribute | default | meaning |
|-----------|---------|---------|
| `data-provider` | server default | `gemini-browser` / `chatgpt-browser` |
| `data-title` | `Ask AI` | header text |
| `data-accent` | `#6ea8fe` | accent color |
| `data-position` | `br` | `br` (bottom-right) / `bl` (bottom-left) |
| `data-greeting` | friendly hi | first assistant line |
| `data-system` | — | a system prompt sent with every turn |
| `data-open` | — | `1` to start expanded |

Runtime handle: `window.BrowserLLMWidget.{open, close, reset, config}` — e.g. `BrowserLLMWidget.config({accent:'#9b8cfb', provider:'chatgpt-browser'})`. Press `Esc` to close.

> The widget inherits the server's **no-auth, LAN-only** trust model — embedding it just means the unauthenticated endpoint is reachable from more pages. Fine for trusted LAN use; don't expose it beyond your network.

## Native desktop app

Prefer a **real Linux app** over a browser tab? [`desktop/`](desktop/README.md) has a native GTK3 client:

- a **tray indicator** whose popup is built for **quick image assets scoped to your VS Code project** — it auto-detects the focused VS Code window and saves generated images straight into that project (remembering a save folder per project). Plus a Chat tab.
- a **full app window** with Chat / Images / Gallery / Status tabs.

Chats are **shared** between the popup and the window (enlarging keeps your conversation) and **persisted** across restarts. It's a thin front-end over the same HTTP API (no browser automation lives in it), and runs on the **system `python3`** — **no venv, no pip, standard library only**.

```bash
# system GTK3 + AppIndicator + xdotool (present/1-cmd on Ubuntu GNOME):
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin xdotool

./desktop/run.sh                 # start it — lands in the tray
./desktop/install-desktop.sh     # optional: add a "Browser LLM" launcher to the app grid
```

Point it at a non-default server with `BROWSER_LLM_API=http://host:8081`. Full details in [`desktop/README.md`](desktop/README.md).

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

## Authentication

**Each provider needs its own login**, stored in its own profile (`gemini_profile/` / `chatgpt_profile/`). When answers come back empty or you see a sign-in / "verify you're human" wall, that provider's session has expired.

**Re-auth must be done in the automation's own browser, on a real display.** The background service's Chrome is invisible (Xvfb), so you can't sign in there — the helper below opens a visible browser instead. Also, `nodriver` launches Chrome with `--password-store=basic`; a normal Chrome uses the system keyring, and cookies written by one cannot be decrypted by the other. So do **not** sign in with a plain `google-chrome` — use the helper:

```bash
systemctl --user stop browser-llm-api
DISPLAY=:1 ./venv/bin/python login.py gemini    # or: chatgpt — a Chrome window opens; sign in; it auto-detects and closes
systemctl --user start browser-llm-api
```

## Image generation

Both providers generate images from a natural prompt. Generated images are saved to disk **and** served over HTTP, so you get a file and a link.

**Storage path** — `GEMINI_IMAGE_DIR` (env; `serve.sh` defaults it to `~/Pictures/browser-llm`). The folder is created on startup and mounted at `/images/<file>`. `GEMINI_PUBLIC_URL` (default `http://localhost:8081`) is the base used to build the returned links — change it if you reach the server from another host. If the folder isn't writable, saving is skipped and it falls back to inline base64 / `data:` URLs.

- **In chat** — a prompt like "generate an image of …" returns the image inline in the assistant message as markdown pointing at the served file: `![...](http://localhost:8081/images/gemini_….png)`. Gemini replies image-only (its accompanying text is internal "thinking"); ChatGPT keeps its caption text and appends the image.
- **Images endpoint** — OpenAI-style `POST /v1/images/generations` (`model` selects the provider):

```bash
curl http://localhost:8081/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-browser","prompt":"a red bicycle on a beach at sunset"}'
# -> {"created": ..., "data": [{
#      "b64_json": "<base64>",
#      "url":  "http://localhost:8081/images/gemini_….png",
#      "path": "~/Pictures/browser-llm/gemini/gemini_….png"
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

## Configuration

| env var | default | meaning |
|---------|---------|---------|
| `DEFAULT_PROVIDER` | `gemini-browser` | provider used when `model` is unknown/absent |
| `GEMINI_IMAGE_DIR` (`IMAGE_DIR`) | `~/Pictures/browser-llm` | base dir for saved images (per-provider subfolders) |
| `GEMINI_PUBLIC_URL` | `http://localhost:8081` | base URL used to build returned image links |
| `BROWSER_RECYCLE_AFTER_IMAGES` | `3` | recycle a provider's browser after this many image gens |
| `BROWSER_LLM_API` | `http://localhost:8081` | server URL the desktop app / `client.py` connect to |

## Project layout

- **`server.py`** — FastAPI server on port **8081** (`/v1/chat/completions`, `/v1/images/generations`, `/v1/models`, `/images/<file>`, plus `/api/status`, `/api/gallery`, `/version`, `/widget.js`, `/demo`). `main()` is the `browser-llm` console entry point.
- **`providers/`** — one adapter per site behind a common `Provider` interface (`gemini.py`, `chatgpt.py`); add a backend by adding a provider, not by touching `server.py`. `base.py` holds the generic completion loop + the unit-tested done-decision logic.
- **`webui/`** — `index.html` (mini web dashboard), `widget.js` (embeddable bubble), `widget-demo.html` (`/demo`). Single files, no build step.
- **`desktop/`** — native Linux desktop app + tray widget (GTK3). See [`desktop/README.md`](desktop/README.md).
- **`login.py`** — interactive re-auth helper: `python login.py gemini|chatgpt`.
- **`client.py`** — tiny stdlib CLI/import client for the API (`./client.py "prompt"`, or `from client import ask`).
- **`gen_asset.py`** — CLI to generate + post-process a website image asset (needs Pillow).
- **`serve.sh`** / **`install-service.sh`** / **`mode.sh`** / **`browser-llm-api.service.template`** — run the server and manage it as a background `systemd --user` service (generated for this clone).
- **`gemini_bot.py`** — standalone single-prompt Gemini prototype.
- **`AGENT_IMAGE_GUIDE.md`** — instructions to hand an AI coding agent so it uses this API to generate site image assets.

## Caveats

- **Fragile by nature** — each provider depends on its site's live DOM/selectors (Gemini: `model-response`; ChatGPT: `[data-message-author-role="assistant"]`, `[data-testid="send-button"]`). A UI change can break extraction. ChatGPT additionally sits behind Cloudflare/anti-bot.
- **One request at a time per provider** (per-provider lock); Gemini and ChatGPT run concurrently, callers to the same provider queue.
- **`usage` token counts are approximate** (word-split, not a real tokenizer).
- A hard crash can leave a stale `<profile>/SingletonLock`; the unit clears both providers' locks on start (`ExecStartPre`).

## Tests

The completion decision (when is a streamed answer / image done?) is pure logic in `providers/base.py` and has unit tests that need no browser:

```bash
./venv/bin/python -m unittest discover -s tests -v
```

## Contributors

- **[staticB1](https://github.com/StaticB1)**
- **Ebenezer "Ebstar" Tarubinga**

Contributions welcome — open an issue or pull request.

## License

[MIT](LICENSE). Version is defined in `_version.py` (surfaced at `/version`, `/api/status`, and the UI footer).

## Disclaimer

This tool automates logged-in sessions on third-party sites (gemini.google.com, chatgpt.com) that have **no official API for this use**. It may violate those services' Terms of Service, it is **inherently fragile** (a site UI change can break extraction at any time), and it uses your own account/session. Use it for personal/experimental purposes, at your own risk, and review each provider's ToS. The authors provide no warranty (see [LICENSE](LICENSE)).
