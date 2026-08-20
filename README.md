# Browser LLM API

**Turn the AI you already use in your browser into a local, OpenAI-compatible API — no API keys, no per-token bills.**

Browser LLM API drives a real, logged-in **ChatGPT** and **Gemini** session through an automated Chrome ([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and re-exposes it as the same HTTP API your tools already speak. Point any OpenAI SDK, script, or app at `http://localhost:8081/v1` and get **streaming chat, vision (send images *in*), and image generation + editing** — powered by your existing subscription (or free tier), running entirely on your own machine.

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
- 🔌 **Drop-in OpenAI compatibility.** `/v1/chat/completions` (streaming + non-streaming), `/v1/images/generations` and `/v1/images/edits`, with the same request/response shapes. Existing OpenAI clients, LangChain, scripts, and dev tools "just work."
- 🎨 **Chat *and* images — both directions.** Generate images from a prompt (returned as a link **and** a saved file), and **send images in**: vision questions and image-to-image editing, by uploading into the provider's own composer.
- 🧩 **Two providers, one field.** Switch between ChatGPT and Gemini per request via the `model` field. Run both at once — they're independent.
- 🏠 **Local & private to your LAN.** Everything runs on your box; nothing goes to a third-party API broker.
- 🤖 **Native tools for coding agents.** A built-in MCP server hands `ask`, `generate_image`, `list_models` and `health` to Claude Code, Claude Desktop, Cursor or Zed, so an agent sends a screenshot and gets back a written file instead of shelling out to a CLI.
- 🖥️ **Use it five ways** (below): REST API, MCP, a web dashboard, an embeddable chat widget, or a **native Linux desktop app**.

> **The honest catch:** this automates logged-in sessions on sites that have **no official API for this** — so it's inherently fragile (a site UI change can break it), may conflict with each provider's Terms of Service, and is meant for **personal / experimental** use on your own account. See the [Disclaimer](#disclaimer).

## What's in the box

| Surface | What it is | Where |
|---------|-----------|-------|
| 🔌 **OpenAI-compatible API** | `/v1/chat/completions` (text + vision) + `/v1/images/generations` + `/v1/images/edits` + `/v1/models` | `http://localhost:8081/v1` |
| 🤖 **MCP server** | `ask` / `generate_image` / `list_models` / `health` over stdio JSON-RPC, for Claude Code, Claude Desktop, Cursor, Zed | `mcp_server.py` |
| 🖥️ **Web dashboard** | Chat with saved history you can search, rename and delete, image attachments, image generation and editing, a gallery, live telemetry. Single file, no build step | `http://localhost:8081/` |
| 💬 **Embeddable widget** | One `<script>` tag drops a floating chat bubble onto any page on your network, with image attach and a remembered thread | `/widget.js` (demo at `/demo`) |
| 🐧 **Native desktop app** | GTK tray widget that generates/edits images for your active **VS Code project**, + a full Chat / Images / Gallery / Status window (Linux) | [`desktop/`](desktop/README.md) |

| `model` | Backend | Images out | Images in |
|---------|---------|------------|-----------|
| `chatgpt-browser` | chatgpt.com | ✅ | ✅ |
| `gemini-browser` | gemini.google.com | ✅ | ✅ |

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

Then open **`http://localhost:8081/`** for the dashboard, or run the [native desktop app](#native-desktop-app). You need **Google Chrome** and **Python 3.12** installed; on a headless box also install the system `xvfb` package.

> First answer empty, or you hit a sign-in / "verify you're human" wall? That provider's session just needs a fresh login — see [Authentication](#authentication).

## How it works

The server keeps **one persistent Chrome per provider** (profile in `gemini_profile/` / `chatgpt_profile/`, gitignored), started lazily on first use. On each request it opens the site, uploads any attached images through the page's own file picker (via CDP, so the site sees an ordinary file selection), types the prompt, and reads the streamed answer back out of the DOM — Gemini by walking the shadow DOM, ChatGPT from the plain-DOM `.markdown` of the last assistant turn. Completion is detected via a CDP network signal (the provider's streaming request finishing) with a DOM-stability fallback. Requests are serialized **per provider** by a lock, so Gemini and ChatGPT can run concurrently while callers to the *same* provider queue.

> **Note on ChatGPT:** the ChatGPT selectors are best-guess against the live UI (which changes often and sits behind Cloudflare/anti-bot) and may need tweaking. Automating chatgpt.com may also conflict with OpenAI's ToS — use accordingly.

## Web dashboard

Open **`http://localhost:8081/`**. It is shaped like the chat app you already use: a history
sidebar on the left, one centred reading column, plain assistant prose, grey user bubbles, a
rounded composer.

- **Chat** — streaming replies with multi-turn context, Enter to send, Shift+Enter for a newline.
  The send button becomes a **stop** button while an answer streams. Attach images with the plus
  button, or paste, or drag-drop them onto the composer; thumbnails stay in the conversation so
  follow-up questions still see them. Code comes back as a card with a language label and a Copy
  button. Hover a turn to copy it, or retry the last answer.
- **Chat history** — every conversation is kept in this browser (`localStorage`), grouped by day,
  searchable by title and body. Rename or delete one from its row, delete all from the top-bar
  menu, export one as JSON. New chat is `Ctrl+Shift+O`.
- **Import the account's own history** — "Import history" in the top-bar menu pulls the titles of
  the conversations already in the signed-in account, so threads that predate this dashboard, or
  that you had in another browser, are listed here too. A thread's messages load the first time you
  open it, one at a time, because reading a whole account up front is what makes the site start
  answering 429. Only ChatGPT exposes a conversation list this server can read.
- **Delete that means delete** — deleting a chat also deletes the images it generated from disk and
  the real conversation from the provider's account, and says which of the three it did. Select
  several at once from the menu's "Select chats", or several images from the gallery's "Select".
- **Create image** — one-line prompt to image, with an elapsed timer, since free-tier generation
  takes 30s to 4min. Attach reference images to switch to **image-to-image** (the request goes to
  `/v1/images/edits`).
- **Gallery** — every image saved under `GEMINI_IMAGE_DIR`, newest first, filterable by provider.
  Select, then tick tiles or take the whole page with "Select all shown", and delete. The gallery is
  the server's own image folder, not a view of the chats: an imported conversation's pictures live
  on the provider's site, so deleting that chat cannot remove anything here.
- **Server** — per-provider telemetry (requests, errors, average and last latency, images until
  recycle), server info (version, uptime, display, image directory), and a copy-paste **embed
  snippet** with a Preview button.

The model picker sits in the top bar and shows each provider's live state; the pill beside it says
whether the current provider is ready or busy. The theme switch in the sidebar footer has three
states, light, auto and dark, and auto follows the system setting live. `/api/status` and
`/api/gallery` back the status readouts and the gallery; `/api/history`,
`/api/conversation/{id}`, `/api/conversations/delete` and `/api/gallery/delete` back the import and
the deletes. Those four reach into a signed-in account, so they answer **403 to a cross-origin
caller** — open the dashboard on the server's own address and they work; a page on another site
cannot use them.

## Embeddable widget

Drop a floating chat bubble onto **any** page on your network with one line — it talks to this server's `/v1/chat/completions`:

```html
<script src="http://localhost:8081/widget.js"></script>
```

The widget is self-contained and **Shadow-DOM isolated** (host-page CSS can't leak in or out). It carries the same look as the dashboard: grey user bubbles, plain assistant prose, code cards with Copy, a stop button mid-answer, and full-screen sizing on a phone. The thread survives a page reload, and the pencil in its header starts a fresh one. It can attach images too, through the plus button, paste, or drag-drop onto the composer. It auto-discovers the API base from its own script URL, so the host page can be on any origin/port — the server already sends open CORS headers. See a live demo at **`http://localhost:8081/demo`**.

Configure with `data-*` attributes on the script tag:

| attribute | default | meaning |
|-----------|---------|---------|
| `data-provider` | server default | `gemini-browser` / `chatgpt-browser` |
| `data-title` | `Ask AI` | header text |
| `data-accent` | theme colour | pins one colour for the bubble and send button in both themes |
| `data-position` | `br` | `br` (bottom-right) / `bl` (bottom-left) |
| `data-greeting` | friendly hi | first assistant line |
| `data-system` | — | a system prompt sent with every turn |
| `data-open` | — | `1` to start expanded |
| `data-persist` | `1` | `0` forgets the thread on reload |
| `data-theme` | `auto` | `light` / `dark` / `auto`, which follows the host page |
| `data-attach` | `1` | `0` hides the 📎 image-attach button |

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

- **In chat** — a prompt like "generate an image of …" returns the image inline in the assistant message as markdown pointing at the served file: `![...](http://localhost:8081/images/gemini/gemini_….png)`. Gemini replies image-only (its accompanying text is internal "thinking"); ChatGPT keeps its caption text and appends the image.
- **Images endpoint** — OpenAI-style `POST /v1/images/generations` (`model` selects the provider):

```bash
curl http://localhost:8081/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini-browser","prompt":"a red bicycle on a beach at sunset"}'
# -> {"created": ..., "data": [{
#      "b64_json": "<base64>",
#      "url":  "http://localhost:8081/images/gemini/gemini_….png",
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

## Image input (vision + image-to-image)

You can send images **in**, not just get them out: attachments are uploaded into the provider's own
chat composer (the same file picker a human clicks), then the prompt is submitted with them. That
covers "what's in this screenshot?", "find the bug in this diagram", and image-to-image editing.

**In chat** — standard OpenAI vision content parts. A `data:` URL, an `http(s)` URL, or a local file
path all work (paths only from localhost — see below):

```bash
curl http://localhost:8081/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "chatgpt-browser",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "What is written in this image?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGg…"}}
  ]}]}'
```

The OpenAI SDK works unchanged, and there's a shorthand (`images`) when you don't want content parts:

```python
client.chat.completions.create(model="chatgpt-browser", messages=[
    {"role": "user", "content": [
        {"type": "text", "text": "Which CSS rule is wrong here?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
    ]}])

# shorthand (not OpenAI): {"messages": [...], "images": ["/path/or/data-url", ...]}
```

**Image-to-image / edits** — `POST /v1/images/edits` takes OpenAI's multipart upload *or* the same
JSON body as `/v1/images/generations` plus `image`/`images`. The reference is uploaded with the
prompt, and the result is saved and returned exactly like a generation:

```bash
# multipart (what the OpenAI SDK's images.edit sends)
curl http://localhost:8081/v1/images/edits \
  -F model=chatgpt-browser -F image=@logo.png \
  -F 'prompt=same logo, flat vector style, deep navy background, white text'

# or JSON — and /v1/images/generations accepts `image` too, which does the same thing
curl http://localhost:8081/v1/images/generations -H 'Content-Type: application/json' \
  -d '{"model":"chatgpt-browser","prompt":"restyle as a night scene","image":"/abs/path/hero.png"}'
```

Every surface can do it: the **web UI** (📎 in the chat composer and on the Image tab — click, paste
or drag-drop), the **widget** (📎; disable with `data-attach="0"`), the **desktop app** (📎 in Chat,
and reference images in the tray's project image panel), `client.py --image FILE` (repeatable), and
`gen_asset.py --ref FILE` for asset restyling.

**Accepted attachment forms:** `data:` URL, bare base64, `http(s)` URL (downloaded server-side),
`file://` URL, or a local path. **Local paths are accepted only from loopback clients** — a LAN
client with the API key would otherwise be able to upload any file off the server box; it must send
bytes instead (`ALLOW_REMOTE_FILE_PATHS=1` opts out). Limits: `MAX_ATTACHMENTS` (default 6) and
`MAX_ATTACHMENT_MB` (default 20). Temp files are deleted after the request; your own files are never
touched. When a model is proxied via `REMOTE_PROVIDERS`, local paths are inlined as `data:` URLs
before forwarding, so remote providers work the same way (both ends need ≥ 0.2.0).

> Vision requests are subject to the same fragility as the rest: ChatGPT's upload path is verified,
> Gemini's is built on the generic file-chooser mechanism with best-guess menu labels (its "Upload &
> tools" button is verified) — if a Gemini upload stops landing, that's the first thing to check.

## Ephemeral requests

The sessions are signed in as a person, so every call a tool makes lands in that
person's real chat history. Send `"ephemeral": true` with a chat completion and the
server deletes the conversation as soon as the answer has been read:

```bash
curl -s localhost:8081/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "chatgpt-browser",
  "ephemeral": true,
  "messages": [{"role": "user", "content": "Judge this copy: ..."}]
}'
```

It is the same soft delete the sidebar's own "Delete" performs, on the conversation
that request created and no other. Deletion failing never fails the request: the
answer is already in hand, and the server logs the reason. ChatGPT implements it;
providers that keep no reachable history ignore the flag. Image generation is not
covered: an asset you keep usually comes with a thread you want to go back to.

The MCP `ask` tool sets it by default, since an agent's question is tooling rather
than conversation; pass `keep_chat: true` when the person asked to see the thread.

## Configuration

| env var | default | meaning |
|---------|---------|---------|
| `DEFAULT_PROVIDER` | `gemini-browser` | provider used when `model` is unknown/absent |
| `GEMINI_IMAGE_DIR` (`IMAGE_DIR`) | `~/Pictures/browser-llm` | base dir for saved images (per-provider subfolders) |
| `GEMINI_PUBLIC_URL` | `http://localhost:8081` | base URL used to build returned image links |
| `BROWSER_RECYCLE_AFTER_IMAGES` | `3` | recycle a provider's browser after this many image gens |
| `BROWSER_LLM_API` | `http://localhost:8081` | server URL the desktop app / `client.py` connect to |
| `BROWSER_LLM_HOST` | `127.0.0.1` | interface the server binds. It drives your logged-in accounts, so it stays off the network until you set `0.0.0.0` (do that with `BROWSER_LLM_API_KEY`) |
| `BROWSER_LLM_PORT` | `8081` | port the server binds |
| `BROWSER_LLM_API_KEY` | *(unset)* | require this key (`Authorization: Bearer …` or `X-Api-Key`) from **non-localhost** clients on `/v1/*` and `/api/*`; localhost stays open |
| `REMOTE_PROVIDERS` | *(unset)* | `model=url[,…]` — proxy those models to another browser-llm-api instance instead of a local browser |
| `REMOTE_API_KEY` | *(unset)* | Bearer key sent on proxied requests (the upstream's `BROWSER_LLM_API_KEY`) |
| `MAX_ATTACHMENTS` | `6` | most input images one request may attach |
| `MAX_ATTACHMENT_MB` | `20` | per-attachment size ceiling |
| `ALLOW_REMOTE_FILE_PATHS` | *(unset)* | let non-localhost (and cross-origin) clients attach **server-side file paths** (off by default) |
| `BROWSER_LLM_EPHEMERAL` | `0` | delete the conversation after every chat completion that doesn't say otherwise (see [Ephemeral requests](#ephemeral-requests)) |

### Who can call it

CORS is deliberately open, because the embeddable widget has to work from any page on your network.
That has a consequence worth knowing: **while the server is running, any website open in your browser
can POST to it** — the request comes from `127.0.0.1` like any local client — and use your logged-in
session. So:

- Treat the port as local trust. Keep the default `127.0.0.1` bind unless you mean to share it, and
  set `BROWSER_LLM_API_KEY` when you do.
- **File-path attachments require a local *and* same-origin caller.** A path makes the browser upload
  that file, so a cross-origin page naming `~/.ssh/config` would otherwise be a file-read primitive —
  it gets a 400 instead. Requests with no `Origin` at all (curl, `client.py`, the desktop app) are
  local clients and keep working. `ALLOW_REMOTE_FILE_PATHS=1` opts out of the whole check.
- Stop the service when you are not using it if you would rather not have the endpoint live at all.

## Sharing a provider with another machine

One machine has a logged-in ChatGPT (or Gemini) session; a friend runs this same project without
one. Point the friend's install at yours — their server proxies that model to your box, and
everything on their side (web UI, widget, desktop app, gallery of their own Gemini) keeps working:

```bash
# On the machine WITH the login (the upstream):
export BROWSER_LLM_HOST=0.0.0.0                      # listen on the LAN
export BROWSER_LLM_API_KEY=$(openssl rand -hex 24)   # gate non-localhost clients
export GEMINI_PUBLIC_URL=http://<your-lan-ip>:8081   # so returned image links resolve remotely
./serve.sh

# On the friend's machine:
export REMOTE_PROVIDERS="chatgpt-browser=http://<your-lan-ip>:8081"
export REMOTE_API_KEY=<the key from above>
./serve.sh
```

Requests for `chatgpt-browser` on the friend's box are forwarded verbatim (streaming included),
**including image attachments** — a local file path is read and inlined as a `data:` URL before
forwarding, since a path means nothing on the upstream's filesystem (both ends need ≥ 0.2.0 for
image input). All other models still run in their own local browser. Different networks? Put both machines on a
[Tailscale](https://tailscale.com) tailnet and use the tailnet IP instead of the LAN IP — nothing
else changes.

## MCP server

Any MCP client can drive the logged-in sessions as native tools, instead of shelling
out to `client.py` and passing files around:

```bash
claude mcp add browser-llm -- "$PWD/venv/bin/python" "$PWD/mcp_server.py"
```

Or, in a client that reads a JSON config (Claude Desktop, Cursor, Zed):

```json
{
  "mcpServers": {
    "browser-llm": {
      "command": "/path/to/browser-llm-api/venv/bin/python",
      "args": ["/path/to/browser-llm-api/mcp_server.py"]
    }
  }
}
```

| tool | does |
|------|------|
| `ask` | prompt (+ `images`, `system`, `model`) → the reply. `out` writes it to a file and returns the path, which keeps a large HTML answer out of the agent's context; `strip_fences` drops ``` lines. The conversation is deleted afterwards unless `keep_chat` is set. |
| `generate_image` | prompt (+ `refs` for image-to-image) → a written asset, with the same resize/crop/favicon/knockout shaping as `gen_asset.py`. |
| `list_models` | the provider ids this server can drive. |
| `health` | reachable? which providers are logged in? a request in flight? |

Stdio JSON-RPC, no dependencies beyond the server's own. Tool calls run on worker
threads, so a four-minute image generation never blocks a ping or a second call —
requests still serialise per provider upstream, which is where the browser lock is.
Point it at another host with `BROWSER_LLM_API`, and set `BROWSER_LLM_API_KEY` if
that host has one.

## Project layout

- **`server.py`** — FastAPI server on port **8081** (`/v1/chat/completions`, `/v1/images/generations`, `/v1/images/edits`, `/v1/models`, `/images/<provider>/<file>`, plus `/api/status`, `/api/gallery`, `/api/history`, `/api/conversation/<id>`, `/api/conversations/delete`, `/api/gallery/delete`, `/version`, `/widget.js`, `/demo`). Also owns the attachment layer: turning whatever a client sent (data URL, base64, http URL, multipart bytes, local path) into files for the browser, and the loopback-only path policy. `main()` is the `browser-llm` console entry point.
- **`providers/`** — one adapter per site behind a common `Provider` interface (`gemini.py`, `chatgpt.py`); add a backend by adding a provider, not by touching `server.py`. `base.py` holds the generic completion loop, the unit-tested done-decision logic, and the generic file-upload machinery (existing file input, or CDP file-chooser interception for sites that create one on demand).
- **`webui/`** — `index.html` (mini web dashboard), `widget.js` (embeddable bubble), `widget-demo.html` (`/demo`). Single files, no build step.
- **`desktop/`** — native Linux desktop app + tray widget (GTK3). See [`desktop/README.md`](desktop/README.md).
- **`login.py`** — interactive re-auth helper: `python login.py gemini|chatgpt`.
- **`client.py`** — tiny stdlib CLI/import client for the API (`./client.py "prompt"`, `--image FILE` to send images, or `from client import ask`).
- **`mcp_server.py`** — MCP (Model Context Protocol) stdio server, stdlib only: exposes `ask`, `generate_image`, `list_models` and `health` as native tools to Claude Code, Claude Desktop, Cursor or Zed. `main()` is the `browser-llm-mcp` console entry point.
- **`gen_asset.py`** — CLI to generate + post-process a website image asset (needs Pillow); `--ref FILE` restyles an existing asset instead of generating from scratch.
- **`serve.sh`** / **`install-service.sh`** / **`mode.sh`** / **`browser-llm-api.service.template`** — run the server and manage it as a background `systemd --user` service (generated for this clone).
- **`gemini_bot.py`** — standalone single-prompt Gemini prototype.
- **`AGENT_IMAGE_GUIDE.md`** — instructions to hand an AI coding agent so it uses this API to generate site image assets.

## Caveats

- **Fragile by nature** — each provider depends on its site's live DOM/selectors (Gemini: `model-response`; ChatGPT: `[data-message-author-role="assistant"]`, `[data-testid="send-button"]`). A UI change can break extraction. ChatGPT additionally sits behind Cloudflare/anti-bot.
- **Image input rides the same selectors.** ChatGPT's upload path is verified end-to-end; Gemini's uses the generic file-chooser mechanism with best-guess menu labels (its "Upload & tools" button is verified) — if Gemini stops receiving attachments, check `attach_click_path` in `providers/gemini.py` first. Attachments are capped by `MAX_ATTACHMENTS` / `MAX_ATTACHMENT_MB`, and a multi-turn conversation re-uploads its images on every request (each request drives a fresh chat), so long image conversations get slower.
- **One request at a time per provider** (per-provider lock); Gemini and ChatGPT run concurrently, callers to the same provider queue.
- **`usage` token counts are approximate** (word-split, not a real tokenizer).
- A hard crash can leave a stale `<profile>/SingletonLock`; `serve.sh` clears both providers' locks on start, so the service and a foreground run are both covered.

## Tests

The tricky pure logic is unit-tested and needs no browser: the completion decision (when is a
streamed answer / image done, including transient "Analyzing image"-style placeholders) in
`providers/base.py`, the API-key / loopback / origin-trust rules in `authz.py`, and the attachment
layer (spec forms, size/count limits, the local-and-same-origin path policy, remote inlining) in
`server.py`.

The history and delete layer is covered the same way: which caller is allowed to read or delete an
account's threads (an opaque `Origin: null` is not), which provider can answer at all, and which
gallery reference resolves to a file this server will unlink — traversal, percent-encoded
traversal, a symlink out of the image dir and a non-image extension are all refused.

The MCP layer is covered too: protocol framing, the `tools/list` contract, error mapping and the
off-the-read-loop dispatch that keeps a four-minute image generation from stalling a ping.

```bash
./venv/bin/python -m unittest discover -s tests -v     # 119 tests, ~0.03s
ruff check .                                           # lint, config in pyproject.toml
```

Both run in CI on every push and pull request, along with an editable install and a live MCP
handshake — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml). No browser is involved in
any of it, so a green build says the pure logic is sound, not that the sites still scrape.

## Authors & acknowledgments

- **browser-llm-api** — **Statotech Systems**, in partnership with **[Ebenworks](https://ebenworks.co/)**.
- **[staticB1](https://github.com/StaticB1)**
- **Ebenezer "Ebstar" Tarubinga**

Contributions welcome — open an issue or pull request.

## License

[MIT](LICENSE). Version is defined in `_version.py` (surfaced at `/version`, `/api/status`, and the UI footer).

## Disclaimer

This tool automates logged-in sessions on third-party sites (gemini.google.com, chatgpt.com) that have **no official API for this use**. It may violate those services' Terms of Service, it is **inherently fragile** (a site UI change can break extraction at any time), and it uses your own account/session. Use it for personal/experimental purposes, at your own risk, and review each provider's ToS. The authors provide no warranty (see [LICENSE](LICENSE)).

<p align="center">
  Made by <a href="https://ebenworks.co/">Ebenworks</a>
</p>
