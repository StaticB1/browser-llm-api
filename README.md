# Gemini Browser API

Drives the **Gemini web UI** through an automated Chrome browser ([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and exposes it as an **OpenAI-compatible API**. No official Google API key — it uses a logged-in Gemini web session.

- **`server.py`** — FastAPI server on port **8081** (`/v1/chat/completions` streaming + non-streaming, `/v1/images/generations`, `/v1/models`).
- **`gemini_bot.py`** — standalone single-prompt prototype (hardcoded prompt → saves the answer).
- **`login_gemini.py`** — interactive re-auth helper (see below).
- **`gemini-api.service`** — systemd `--user` unit to run the server in the background.
- **`gen_asset.py`** — CLI to generate + post-process a website image asset (resize/crop/convert/favicon/transparency).
- **`AGENT_IMAGE_GUIDE.md`** — instructions to hand an AI coding agent so it uses this API to generate site image assets.

## How it works

The server keeps one persistent Chrome instance (profile in `gemini_profile/`, gitignored). On each request it opens Gemini, types the prompt, and reads the streamed answer out of the response element by walking the shadow DOM. Completion is detected via a CDP network signal (the `BardFrontendService` stream finishing) with a DOM-stability fallback. Requests are serialized (one at a time) by a global lock.

## Requirements

- Google Chrome, Python 3.12
- `pip install nodriver fastapi uvicorn pydantic`
- Chrome runs **non-headless** on purpose (Gemini blocks true headless). The background service renders to a virtual **Xvfb** display (via `xvfb-run`), so no real monitor is needed. Re-auth uses a real display (e.g. `DISPLAY=:1`). Requires the `xvfb` package.

## Run once (foreground)

```bash
python3 server.py            # serves http://localhost:8081/v1
# or the standalone prototype:
python3 gemini_bot.py
```

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

Everything depends on a logged-in Google session stored in `gemini_profile/`. When answers come back empty or you see the "Meet Gemini" / "Sign in" landing page, the session has expired.

**Re-auth must be done in the automation's own browser, on a real display.** The background service's Chrome is invisible (Xvfb), so you can't sign in there — the helper below opens a visible browser instead. Also, `nodriver` launches Chrome with `--password-store=basic`; a normal Chrome uses the system keyring, and cookies written by one cannot be decrypted by the other. So do **not** sign in with a plain `google-chrome` — use the helper:

```bash
systemctl --user stop gemini-api
DISPLAY=:1 python3 login_gemini.py   # a Chrome window opens — sign in; it auto-detects and closes
systemctl --user start gemini-api
```

## Image generation

Gemini generates images from a natural prompt. Every generated image is saved to disk **and** served over HTTP, so you get a file and a link.

**Storage path** — set `GEMINI_IMAGE_DIR` in the service unit (`Environment=GEMINI_IMAGE_DIR=…`); default `/home/b/Pictures/gemini`. The folder is created on startup and mounted at `/images/<file>`. `GEMINI_PUBLIC_URL` (default `http://localhost:8081`) is the base used to build the returned links — change it if you reach the server from another host. If the folder isn't writable, saving is skipped and it falls back to inline base64 / `data:` URLs.

- **In chat** — a prompt like "generate an image of …" returns the image inline in the assistant message as markdown pointing at the served file: `![AI generated](http://localhost:8081/images/gemini_….jpg)`. (Image prompts reply image-only — Gemini's accompanying text is internal "thinking", not a caption.)
- **Images endpoint** — OpenAI-style `POST /v1/images/generations`:

```bash
curl http://localhost:8081/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red bicycle on a beach at sunset"}'
# -> {"created": ..., "data": [{
#      "b64_json": "<jpeg base64>",
#      "url":  "http://localhost:8081/images/gemini_….jpg",
#      "path": "/home/b/Pictures/gemini/gemini_….jpg"
#    }]}
```

`n` and `size` are accepted but ignored — Gemini decides the count and dimensions. Internally the server waits for the `<img>` to finish rendering, reads its `blob:` URL out of the page as base64 (blob URLs can't be fetched over HTTP from outside the browser), writes it to `GEMINI_IMAGE_DIR`, and returns base64 + URL + path.

**Generating website assets** — output is always a ~1024×559 opaque JPEG, so for real assets (hero images, section backgrounds, textures, avatars, favicons) use `gen_asset.py`, which generates then post-processes with Pillow (resize/crop/convert/favicon/transparency). See **`AGENT_IMAGE_GUIDE.md`** for a ready-to-hand instruction set for an AI coding agent.

## Caveats

- **Fragile by nature** — it depends on Gemini's DOM/aria-labels (`model-response`, `Send message`, etc.). A Google UI change can break extraction.
- **One request at a time** (global lock); concurrent callers queue.
- **`usage` token counts are approximate** (word-split, not a real tokenizer).
- A hard crash can leave a stale `gemini_profile/SingletonLock`; the unit clears it on start (`ExecStartPre`).
