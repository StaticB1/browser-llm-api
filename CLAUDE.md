# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is

Drives a **chat web UI through an automated Chrome browser**
([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and exposes it as an
**OpenAI-compatible HTTP API** — chat completions **and** image generation. There is **no official
API key** for either backend; it piggybacks on a logged-in web session stored in a local Chrome
profile. Prompts are typed into the page and answers (text and generated images) are scraped back
out of the DOM.

Two providers, selected per-request by the OpenAI **`model`** field:

| model | site | profile | images |
|-------|------|---------|--------|
| `gemini-browser` | gemini.google.com | `gemini_profile/` | yes |
| `chatgpt-browser` | chatgpt.com | `chatgpt_profile/` | yes |

Unknown/absent `model` → `DEFAULT_PROVIDER` (env, default `gemini-browser`).

This is inherently **fragile**: each provider depends on its site's live DOM/selectors, and ChatGPT
additionally sits behind Cloudflare/anti-bot. A UI change can silently break extraction or submission.

## Layout

```
server.py            # FastAPI app, port 8081. Model→provider router, the generic
                     #   completion loop, generic image persistence, CDP patch.
providers/
  __init__.py        # PROVIDERS registry + get_provider(model) + DEFAULT_PROVIDER
  base.py            # Provider ABC, StreamMonitor, CompletionTracker (done-decision),
                     #   generic open_and_send(), patch_cdp()
  gemini.py          # GeminiProvider — shadow-DOM extraction, blob→b64 images
  chatgpt.py         # ChatGPTProvider — plain-DOM extraction, oaiusercontent/blob images
login.py             # generic re-auth helper:  python login.py gemini|chatgpt
gen_asset.py         # CLI wrapper: POST /v1/images/generations (no model → DEFAULT_PROVIDER),
                     #   then Pillow post-process (resize/crop/favicon/knockout) → asset file
AGENT_IMAGE_GUIDE.md # instructions to hand an AI agent for generating site image assets
gemini_bot.py        # standalone single-prompt prototype (Gemini only). UNCHANGED, not part of the server.
serve.sh             # run the server: venv python + display auto-detect (real $DISPLAY else Xvfb)
install-service.sh   # venv + deps + generate the systemd --user unit from the template
browser-llm-api.service.template  # unit template; install-service.sh substitutes the clone path
*_profile/           # per-provider Chrome user-data dirs. Gitignored. Never commit.
```

There is no `requirements.txt`. **Deps are installed in a local venv at `./venv`** (system Python
is PEP-668 externally-managed, so a venv is required): `./venv/bin/pip install nodriver fastapi
uvicorn pydantic`. Run the server with `./serve.sh` (foreground) or `./install-service.sh` (background
service); both use `./venv/bin/python`. Also needs Google Chrome and the system `xvfb` package. Python 3.12.
`gen_asset.py` additionally needs **Pillow** (`./venv/bin/pip install Pillow`).

## The `Provider` abstraction (`providers/base.py`)

Adding/altering a backend means editing a provider, not `server.py`. A provider is mostly
declarative — class attributes `name`, `chat_url`, `profile_dir`, `stream_url_fragments` (CDP
completion signal), `supports_images`, `image_text_is_caption`, `input_selector`, `send_selectors`,
`load_wait` — plus site-specific reads:

- `open_and_send(browser, prompt) -> (page, monitor)` — **generic in base**; navigates, attaches the
  `StreamMonitor`, types into `input_selector`, clicks the first working `send_selectors` (Enter
  fallback). Override only for a site that needs something special.
- `get_response_text(page) -> str` — current text of the last assistant turn (UI chrome stripped).
- `is_generating(page) -> bool` — is the model still producing?
- `image_status(page) -> {loaded, pending, creating}` — default no-op (text-only).
- `get_images(page) -> [{mime, b64|src, alt}]` — default `[]`. `b64` = read inline; `src` = remote
  URL fallback when the in-page fetch is CORS-blocked.
- `logged_in(page) -> bool` — used by `login.py`.

Everything downstream (`_stream_completion`, `run_chat`, `drive_once`, image persistence, the
`StreamMonitor`, and the `CompletionTracker` that decides *when an answer is done*) is **generic
and provider-parameterized** in `server.py`/`base.py`.

## How a request flows (`server.py`)

1. `get_provider(req.model)` picks the provider.
2. **One persistent Chrome per provider** (`_browsers[name]`, started lazily) with that provider's profile.
3. **Per-provider `asyncio.Lock`** (`_locks[name]`) serializes requests within a provider; Gemini and ChatGPT can run concurrently.
4. `_build_prompt()` flattens the OpenAI `messages` array (system → `[Context/Instructions: …]` preamble; multi-turn → `User:`/`Assistant:` labels).
5. `provider.open_and_send()` opens the chat, types, submits.
6. `_stream_completion()` polls, yielding text deltas from `provider.get_response_text()`. It **suppresses** the "Creating your image…" placeholder / thinking text while `image_status` reports an image pending, and keeps waiting until the `<img>` renders.
7. **Completion**: the `CompletionTracker` (in `base.py`, unit-testable without a browser) is fed one poll sample at a time and decides done via: image-stability (an `<img>` rendered and stable ≥4s), or text settled (text unchanged ≥2.5s while not generating), or a give-up guard (generation happened but no text). The `StreamMonitor`'s HTTP `stream_url_fragments` signal (`cdp_fired_at`) is informational only. Deadline is progress-aware: base 420s, **extended up to 900s while the answer is still actively streaming** (text still growing or WebSocket frames still arriving), so long code/HTML answers aren't truncated. Then `provider.get_images()` runs and images are `_persist()`ed + appended.
8. The tab is **left open on purpose** — closing/navigating away destabilizes the browser.

## Image generation

- **Extraction** is per-provider (`image_status` + `get_images`). Gemini reads `blob:` URLs to base64 by shadow-piercing; ChatGPT reads `oaiusercontent`/`blob:` `<img>`s in the last assistant turn, falling back to the remote `src` URL if CORS blocks the in-page fetch.
- **Storage** (`_persist`, generic): images with inline `b64` are written to `GEMINI_IMAGE_DIR` and served at `/images/<file>` (mounted `StaticFiles`); the returned link uses `GEMINI_PUBLIC_URL`. Remote-only images keep their `src`. If the dir isn't writable, saving is skipped (`_SAVE_ENABLED=False`).
- **In chat**: `_compose()` returns image-only markdown when `image_text_is_caption` is False (Gemini — its image-prompt prose is internal thinking), or text + images when True (ChatGPT — real caption).
- **Endpoint** `POST /v1/images/generations`: `{"created", "data":[{b64_json?, url?, path?}]}`. `n`/`size` accepted but ignored. **501** if the provider doesn't support images, **502** if it returned none.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `DEFAULT_PROVIDER` | `gemini-browser` | Provider used when `model` is unknown/absent. |
| `GEMINI_IMAGE_DIR` | `~/Pictures/gemini` | Where generated images are saved (shared across providers). |
| `GEMINI_PUBLIC_URL` | `http://localhost:8081` | Base URL used to build returned image links. |
| `BROWSER_RECYCLE_AFTER_IMAGES` | `3` | Recycle a provider's browser after this many image gens (renderer bloats and times out otherwise). |

## Running

```bash
./serve.sh                                     # foreground → http://localhost:8081/v1

# background (systemd --user): venv + generated unit + linger, one command:
./install-service.sh
journalctl --user -u browser-llm-api -f        # logs live in the journal, NOT server.log

# serve.sh auto-detects the display: real $DISPLAY (ChatGPT images work) else headless Xvfb.
# On a headless box, force a real display for ChatGPT image gen:
DISPLAY=:1 ./serve.sh
```

## Authentication — the #1 failure mode

**Each provider needs its own login** (separate profile). Empty answers / a sign-in or "verify you're
human" wall ⇒ that provider's session expired. Re-auth on a real display:

```bash
systemctl --user stop browser-llm-api
DISPLAY=:1 ./venv/bin/python login.py gemini   # or: chatgpt — visible Chrome opens; sign in; auto-closes
systemctl --user start browser-llm-api
```

**Why you must use `login.py`, not a normal Chrome:** `nodriver` launches Chrome with
`--password-store=basic`, while a normal Chrome uses the system keyring. Cookies written by one
**cannot be decrypted by the other**. The service's Chrome is also invisible (Xvfb), so you can't
sign in there — the helper opens a real, visible window in the *same* cookie store.

## Gotchas & conventions

- **CDP parser patch**: `patch_cdp()` (in `base.py`) monkeypatches `nodriver.cdp.util.parse_json_event`
  to swallow `KeyError` from unknown CDP events (e.g. `DOM.adoptedStyleSheetsModified`). Called at
  import time by `server.py` and `login.py`; call it in any new entry point.
- **Non-headless is mandatory** — the sites block true headless Chrome. Background = Xvfb virtual
  display, never `--headless`.
- **ChatGPT image generation REQUIRES a GPU / real display — it does NOT work under headless Xvfb.**
  GPT-image renders progressively on a `<canvas>`, which stalls indefinitely under Xvfb's software
  rendering (even with SwiftShader GL flags, which are set in `CHROME_ARGS` and help other cases).
  So: **ChatGPT text and Gemini run fine under the Xvfb systemd service, but ChatGPT *image* requests
  must run with the server on a real display** (e.g. `DISPLAY=:1 ./venv/bin/python server.py`).
  Verified working on `:1` (produced a real 1536×1024 PNG in ~40s). Image gen on the free "Go" tier
  is also slow/variable (30s–4min+), hence the 420s completion deadline.
- **ChatGPT specifics (verified):** composer `#prompt-textarea`; send `button[data-testid="send-button"]`
  (only appears after typing — the composer shows a Voice button at rest); response text in the last
  `[data-message-author-role="assistant"] .markdown`; generation state = `[data-testid="stop-button"]`.
  ChatGPT **streams over WebSocket** (`ws.chatgpt.com`), so the HTTP CDP stream signal never fires —
  completion relies on `is_generating` going false + image-stability, NOT on `cdp_fired_at`. WS frames
  are tracked (`ws_url_fragments`) only as a "still streaming" heartbeat that extends the deadline for
  long answers; they are **not** parsed for a done-signal.
- **ChatGPT big-text / "canvas" hang (fixed):** `image_status`'s "creating" flag counted *any* `<canvas>`
  on the page as image generation. ChatGPT's code/Canvas editors (Monaco/CodeMirror) draw on `<canvas>`,
  so long code/HTML answers were mis-read as "image pending" → text suppressed + loop rode the full
  deadline → 7-min hang returning nothing. Fix: only a **large, image-shaped** canvas (min side ≥256px)
  counts (image-render canvas is 512–1024px; editor minimap/gutter canvases are narrow). Belt-and-suspenders
  in `CompletionTracker`: if "creating" stays set with no image after generation ends, it's treated as a
  false positive after 45s. `get_response_text` also falls back to reading the Canvas side-panel editor
  (`.cm-content` / Monaco `.view-lines` / a non-composer `.ProseMirror`) when the message body is near-empty
  — best-guess selectors, verify live if canvas answers look wrong.
  A generated image is an `<img src="…/backend-api/estuary/content?id=file_…" alt="Generated image: …">`
  (same-origin → fetchable to base64), NOT `oaiusercontent`/`blob:`. The finished image is *not* inside
  a `data-message-author-role` element, so `image_status`/`get_images` scan the whole page.
- **ChatGPT session cookie is chunked**: `__Secure-next-auth.session-token.0` / `.1` (no un-suffixed
  name). `login.py` prefix-matches it and waits for it (not the DOM) before closing, so the session
  actually persists. Gemini's path is behaviorally unchanged from the original.
- **Service unit is generated, not committed** — `install-service.sh` fills
  `browser-llm-api.service.template` (`__INSTALL_DIR__` → the clone path) into
  `~/.config/systemd/user/browser-llm-api.service`; its `ExecStart` runs `serve.sh` (venv python +
  display auto-detect). No paths are hardcoded in the repo. `IMAGE_DIR` defaults to `~/Pictures/gemini`
  (override with `GEMINI_IMAGE_DIR`); if it isn't writable, image saving silently disables.
- **`usage` token counts are fake** — plain `.split()` word counts, not a real tokenizer.
- **Stale lock after a crash**: a hard crash leaves `<profile>/SingletonLock`; the systemd unit clears
  both profiles' locks in `ExecStartPre`. Running by hand? delete `*_profile/Singleton*`.
- **Logs**: `server.py` writes `server.log` (mode `w`, wiped each start) + stderr; under systemd the
  journal is the real log. `gemini_bot.py` writes `gemini_session.log`.

## Git

Default/main branch is **`master`** (there is no `main` branch), remote `origin` →
https://github.com/StaticB1/google_api. Never commit `*_profile/`, `*.log`,
`gemini_research_data.txt`, or `__pycache__/` (all gitignored).
