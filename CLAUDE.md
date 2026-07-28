# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is

Drives a **chat web UI through an automated Chrome browser**
([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and exposes it as an
**OpenAI-compatible HTTP API** — chat completions, image generation, **and image input** (vision +
image-to-image). There is **no official API key** for either backend; it piggybacks on a logged-in
web session stored in a local Chrome profile. Prompts are typed into the page, input images are
uploaded through the site's own file picker, and answers (text and generated images) are scraped
back out of the DOM.

A **mini web UI** (`webui/index.html`, single file, no build step) is served at
`http://localhost:8081/` — streaming chat (with 📎 image attachments), image generation and
image-to-image, gallery of saved images, a live per-provider status bar, and a **Status tab** with live telemetry (requests / errors / avg+last
latency / last-error / recycle countdown per provider), backed by `/api/status` + `/api/gallery`.

There is also an **embeddable chat widget** (`webui/widget.js`, served at `/widget.js`): a
self-contained, Shadow-DOM-isolated floating chat bubble that any other page on the LAN can add with
`<script src="http://<host>:8081/widget.js"></script>`. It auto-discovers this server as its API base
from its own script URL (CORS is already open) and streams from `/v1/chat/completions`. Config via
`data-*` attrs (`provider`, `title`, `accent`, `position`, `greeting`, `system`, `open`, `attach`); runtime
handle `window.BrowserLLMWidget` (`open`/`close`/`reset`/`config`). The Status tab shows a copy-paste
embed snippet + a "Preview widget on this page" button.

Two providers, selected per-request by the OpenAI **`model`** field:

| model | site | profile | images out | images in |
|-------|------|---------|------------|-----------|
| `gemini-browser` | gemini.google.com | `gemini_profile/` | yes | yes (chooser path) |
| `chatgpt-browser` | chatgpt.com | `chatgpt_profile/` | yes | yes (verified) |

Unknown/absent `model` → `DEFAULT_PROVIDER` (env, default `gemini-browser`).

This is inherently **fragile**: each provider depends on its site's live DOM/selectors, and ChatGPT
additionally sits behind Cloudflare/anti-bot. A UI change can silently break extraction or submission.

## Layout

```
server.py            # FastAPI app, port 8081. Model→provider router, the generic
                     #   completion loop, generic image persistence, attachment
                     #   (image-input) materialization + local-path policy, CDP patch.
                     #   main() is the `browser-llm` console entry point
                     #   (BROWSER_LLM_HOST/PORT). Serves "/" + /widget.js + /demo +
                     #   /version + /api/status (incl. per-provider telemetry:
                     #   _metrics + _record_request) + /api/gallery, and
                     #   /v1/images/edits (multipart or JSON).
_version.py          # single source of truth for __version__ (read by pyproject + server).
pyproject.toml       # packaging: metadata, deps, dynamic version, `browser-llm` entry point.
                     #   Flat layout — install editable from a clone (`pip install -e .`).
LICENSE              # MIT.
README.md            # project overview / usage.
QUICKSTART.md        # fast-path setup guide.
authz.py             # stdlib-only access control + remote-upstream config: loopback detection,
                     #   API-key gating rules (which paths need the key), REMOTE_PROVIDERS parsing.
                     #   Separate from server.py so tests import it without server's side effects.
client.py            # tiny stdlib-only CLI/importable client (no deps): `./client.py "prompt"`
                     #   or `from client import ask`. `--image FILE` (repeatable) sends
                     #   images with the prompt. Env BROWSER_LLM_API/BROWSER_LLM_MODEL/
                     #   BROWSER_LLM_API_KEY.
mode.sh              # toggle the systemd service's Chrome visibility (headless/visible) via a
                     #   drop-in override: `./mode.sh headless|visible`; `./mode.sh` shows current mode.
webui/index.html     # mini web UI (single file, no build step): streaming chat with
                     #   image attachments (click/paste/drop), image gen + image-to-image
                     #   with elapsed timer, gallery, live provider status,
                     #   Status tab (telemetry + embed snippet + version in footer).
webui/widget.js      # embeddable floating chat bubble (Shadow-DOM isolated, no build step);
                     #   served at /widget.js; auto-discovers API base from its own <script src>.
                     #   Has 📎 image attach (click/paste/drop); data-attach="0" hides it.
webui/widget-demo.html # standalone demo page (served at /demo) embedding the widget.
desktop/               # NATIVE Linux desktop app + tray widget (GTK3), a thin client of the
                       #   HTTP API — NOT browser automation. Runs on SYSTEM python3 (has
                       #   PyGObject); stdlib only, no venv/pip. GTK3 (not 4) on purpose:
                       #   AppIndicator (the tray) is GTK3-only and can't share a process
                       #   with GTK4. The tray widget's job: generate image assets for the
                       #   focused VS Code project.
  browser_llm_desktop.py # whole app: Api (stdlib urllib); ProjectManager (reads open VS Code
                       #   windows from ~/.config/Code/User/globalStorage/storage.json, resolves
                       #   the focused window via xdotool, auto-follows focus, remembers a save
                       #   folder per project in ~/.local/share/browser-llm-desktop/projects.json);
                       #   ChatStore (single SHARED + persisted conversation store — popup and
                       #   window are both views, so enlarging never loses the chat; chats.json);
                       #   ProjectImagePanel (gen or 📎 image-to-image -> save into
                       #   project, pick&remember folder);
                       #   ChatPanel, GalleryPanel/ImageViewer, StatusPanel, MainWindow (Chat/
                       #   Images/Gallery/Status + History menu), QuickChatWindow (Image|Chat
                       #   tabs), TrayApp.
  icon.svg run.sh install-desktop.sh browser-llm-desktop.desktop.in README.md
tests/               # unit tests (no browser needed):
                     #   ./venv/bin/python -m unittest discover -s tests
                     #   test_completion_tracker.py (done-decision incl. status
                     #   placeholders), test_authz.py, test_attachments.py
                     #   (attachment specs/limits/local-path policy/remote inlining)
providers/
  __init__.py        # PROVIDERS registry + get_provider(model) + DEFAULT_PROVIDER
  base.py            # Provider ABC, StreamMonitor, CompletionTracker (done-decision),
                     #   generic open_and_send(), generic file-upload/attach machinery
                     #   (file input + CDP file-chooser interception), patch_cdp()
  gemini.py          # GeminiProvider — shadow-DOM extraction, blob→b64 images,
                     #   upload via the "Upload & tools" menu + chooser interception
  chatgpt.py         # ChatGPTProvider — plain-DOM extraction, oaiusercontent/blob images,
                     #   upload via the hidden upload-photos-input; excludes INPUT images
                     #   from generated-image scans; shimmer-aware text/generating reads
login.py             # generic re-auth helper:  python login.py gemini|chatgpt
gen_asset.py         # CLI wrapper: POST /v1/images/generations (no model → DEFAULT_PROVIDER),
                     #   or /v1/images/edits with `--ref FILE` (image-to-image restyle),
                     #   then Pillow post-process (resize/crop/favicon/knockout) → asset file
AGENT_IMAGE_GUIDE.md # instructions to hand an AI agent for generating site image assets
gemini_bot.py        # standalone single-prompt prototype (Gemini only). UNCHANGED, not part of the server.
serve.sh             # run the server: venv python + display auto-detect (real $DISPLAY else Xvfb)
install-service.sh   # venv + deps + generate the systemd --user unit from the template
browser-llm-api.service.template  # unit template; install-service.sh substitutes the clone path
*_profile/           # per-provider Chrome user-data dirs. Gitignored. Never commit.
```

**Deps are installed in a local venv at `./venv`** (system Python is PEP-668
externally-managed, so a venv is required): `./venv/bin/pip install -r requirements.txt`.
Run the server with `./serve.sh` (foreground) or `./install-service.sh` (background
service); both use `./venv/bin/python`. Also needs Google Chrome and the system `xvfb` package. Python 3.12.
Pillow (in requirements.txt) is only needed by `gen_asset.py`.

## The `Provider` abstraction (`providers/base.py`)

Adding/altering a backend means editing a provider, not `server.py`. A provider is mostly
declarative — class attributes `name`, `chat_url`, `profile_dir`, `stream_url_fragments` (CDP
completion signal), `supports_images`, `image_text_is_caption`, `input_selector`, `send_selectors`,
`load_wait`, and for image input `supports_upload`, `attach_click_path`, `attachment_ready_js`,
`upload_timeout`, `upload_settle` — plus site-specific reads:

- `open_and_send(browser, prompt, attachments=None) -> (page, monitor)` — **generic in base**;
  navigates, attaches the `StreamMonitor`, uploads any `attachments` (see the image-input section),
  types into `input_selector`, clicks the first working `send_selectors` (Enter fallback). Override
  only for a site that needs something special.
- `attach_files(page, paths) -> int` / `wait_uploads_ready(page, n)` — **generic in base**; driven by
  the declarative `supports_upload`, `attach_click_path`, `attachment_ready_js`, `upload_timeout`,
  `upload_settle`.
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
4. `_build_prompt()` flattens the OpenAI `messages` array (system → `[Context/Instructions: …]` preamble; multi-turn → `User:`/`Assistant:` labels) and returns `(prompt, image specs)`.
5. `_attachment_files()` materializes those specs into files, then `provider.open_and_send()` opens the chat, uploads them, types, submits (temp files are cleaned up when the drive ends).
6. `_stream_completion()` polls, yielding text deltas from `provider.get_response_text()`. It **suppresses** transient status text — "Creating your image…" / "Analyzing image" (see `CompletionTracker._PLACEHOLDER_RE`, short text only) and thinking text while `image_status` reports an image pending — and keeps waiting until the `<img>` renders.
7. **Completion**: the `CompletionTracker` (in `base.py`, unit-testable without a browser) is fed one poll sample at a time and decides done via: image-stability (an `<img>` rendered and stable ≥4s), or text settled (text unchanged ≥2.5s while not generating), or a give-up guard (generation happened but no text — 10s, stretched to 45s while a status placeholder is on screen). The `StreamMonitor`'s HTTP `stream_url_fragments` signal (`cdp_fired_at`) is informational only. Deadline is progress-aware: base 420s, **extended up to 900s while the answer is still actively streaming** (text still growing or WebSocket frames still arriving), so long code/HTML answers aren't truncated. Then `provider.get_images()` runs and images are `_persist()`ed + appended.
8. The tab is **left open on purpose** — closing/navigating away destabilizes the browser.

## Image input — attachments (vision + image-to-image, added 2026-07-28)

Images can go **in** as well as out. The server materializes whatever the client sent into real
files, and the provider uploads them through the site's own file picker before submitting the prompt.

- **Wire formats** (`server.py`): OpenAI vision content parts
  (`{"type":"image_url","image_url":{"url":…}}`, plain-string `image_url`, and Anthropic's
  `{"type":"image","source":{…base64…}}` are all accepted — `ContentPart` is deliberately permissive),
  a non-OpenAI `images: [...]` shorthand on chat requests, `image`/`images` on
  `/v1/images/generations`, and **`POST /v1/images/edits`** which takes OpenAI's *multipart* upload
  (needs `python-multipart`) **or** the same JSON body.
- **Spec forms**: `data:` URL, bare base64, `http(s)` URL (downloaded server-side, size-capped),
  `file://`, or a local path. `_attachment_files()` is an async context manager that writes temps
  (sniffing the real extension from magic bytes) and deletes them after the drive; caller-supplied
  paths are used in place and never deleted. `MAX_ATTACHMENTS` (6), `MAX_ATTACHMENT_MB` (20).
- **Local paths are loopback-only** (`_client_may_send_paths`): a keyed LAN client could otherwise
  make the server upload any file off this box to ChatGPT. `ALLOW_REMOTE_FILE_PATHS=1` opts out.
  Proxied requests inline local paths as `data:` URLs (`_inline_paths_for_remote`) — a path is
  meaningless on the upstream's filesystem.
- **`_build_prompt` now returns `(prompt, specs)`** and annotates multi-turn text with
  `[N attached images]`, since every image lands in one composer message and the model otherwise
  can't tell which turn an image belonged to.
- **Provider side** (`providers/base.py`): `open_and_send(browser, prompt, attachments=None)` →
  `attach_files()`, which tries (1) an `<input type=file>` already in the DOM (shadow-piercing,
  best-scoring candidate first, verified by watching for the attachment chip) then (2) **CDP
  file-chooser interception**: `Page.setInterceptFileChooserDialog`, click through
  `attach_click_path`, then `DOM.setFileInputFiles` on the backend node from
  `Page.fileChooserOpened`. Interception stays on for the tab's life on purpose — an
  un-intercepted native dialog would wedge the renderer with nobody to dismiss it.
  `wait_uploads_ready()` polls `attachment_ready_js` (`{ready, busy}`) for two clean samples —
  `require_idle=False` while probing an input (chips only), then the full idle wait in
  `open_and_send` before submitting, because sending mid-upload loses the attachment.
- **ChatGPT** (verified live 2026-07-28): hidden `input[data-testid="upload-photos-input"]`
  (`accept=image/*`) exists at rest, so path (1) always wins; each accepted file renders a 144px
  tile with `button[aria-label^="Remove file …"]` — that's the chip count. **Gemini** keeps *no*
  file input at rest, so it needs path (2); its `button[aria-label="Upload & tools"]` is verified but
  the menu-item labels are best-guess (the box's Gemini profile was signed out) — check those first
  if a Gemini upload stops landing.
- **Don't let an input image look like a generated one.** ChatGPT's `image_status`/`get_images` scan
  the *whole page* (a generated image renders outside the assistant turn), and an uploaded image has
  the same `blob:`/`content?` URL shape. `_isInput()` excludes anything inside
  `[data-message-author-role="user"]`, a `form`, or a `file-tile` — without it every vision request
  completed instantly on "image stability" (truncating the text) and echoed the upload back into the
  gallery.
- **"Analyzing image" placeholder (fixed 2026-07-28):** on a vision request ChatGPT puts transient
  status text in the *same* `.markdown` node as the answer, marked `loading-shimmer aria-busy`, and
  the stop button can be absent during that phase — so `get_response_text` returned "Analyzing
  image", it settled for 2.5s, and *that* was returned as the whole answer. Fixed structurally:
  `get_response_text` returns `""` and `is_generating` returns True while that node is
  shimmering/aria-busy. Generic backstop in `CompletionTracker`: `_PLACEHOLDER_RE` also covers
  analyzing/analysed/reading/thinking/working **but only for text ≤ `PLACEHOLDER_MAX_LEN` (48)**, so
  a real answer opening with "Analyzing the image, …" isn't swallowed; while a placeholder shows, the
  give-up-empty window stretches to `SILENT_PLACEHOLDER_DONE` (45s) instead of 10s.

## Image generation

- **Extraction** is per-provider (`image_status` + `get_images`). Gemini reads `blob:` URLs to base64 by shadow-piercing; ChatGPT reads `oaiusercontent`/`blob:` `<img>`s in the last assistant turn, falling back to the remote `src` URL if CORS blocks the in-page fetch.
- **Storage** (`_persist`): images with inline `b64` are written to a **per-provider subfolder** of the base dir (`<IMAGE_DIR>/<provider>/<provider>_<ts>_<hash>.ext`, e.g. `chatgpt/…` vs `gemini/…`) and served at `/images/<provider>/<file>` (mounted `StaticFiles` serves nested dirs); the returned link uses `GEMINI_PUBLIC_URL`. The per-provider slug (`_provider_slug`) stops one provider's images from being mislabeled as another's. Remote-only images keep their `src`. If the dir isn't writable, saving is skipped (`_SAVE_ENABLED=False`).
- **In chat**: `_compose()` returns image-only markdown when `image_text_is_caption` is False (Gemini — its image-prompt prose is internal thinking), or text + images when True (ChatGPT — real caption).
- **Endpoint** `POST /v1/images/generations`: `{"created", "data":[{b64_json?, url?, path?}]}`. `n`/`size` accepted but ignored. **501** if the provider doesn't support images, **502** if it returned none.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `DEFAULT_PROVIDER` | `gemini-browser` | Provider used when `model` is unknown/absent. |
| `GEMINI_IMAGE_DIR` | `~/Pictures/browser-llm` | Base dir for saved images; each provider gets a subfolder (`chatgpt/`, `gemini/`). `IMAGE_DIR` also accepted. |
| `GEMINI_PUBLIC_URL` | `http://localhost:8081` | Base URL used to build returned image links. |
| `BROWSER_RECYCLE_AFTER_IMAGES` | `3` | Recycle a provider's browser after this many image gens (renderer bloats and times out otherwise). |
| `MAX_ATTACHMENTS` | `6` | Most input images one request may attach. |
| `MAX_ATTACHMENT_MB` | `20` | Per-attachment size ceiling (data URLs, downloads and local files alike). |
| `ALLOW_REMOTE_FILE_PATHS` | *(unset)* | Let **non-loopback** clients attach server-side file paths. Off by default — it's a file-read primitive. |
| `BROWSER_LLM_API_KEY` | *(unset)* | When set, **non-loopback** clients must send it (`Authorization: Bearer …` or `X-Api-Key`) on `/v1/*` and `/api/*`. Localhost stays open; pages/assets (`/`, `/widget.js`, `/images/*`, …) stay public. Makes binding to `0.0.0.0` sane. |
| `REMOTE_PROVIDERS` | *(unset)* | `model=url[,model=url…]` — proxy those models to **another browser-llm-api instance** instead of a local browser (overrides the local provider of the same name). E.g. a second install without a ChatGPT login sets `chatgpt-browser=http://<host-with-login>:8081`. |
| `REMOTE_API_KEY` | *(unset)* | Bearer key sent on proxied requests (the upstream's `BROWSER_LLM_API_KEY`). |

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

## Sharing a provider across machines (auth + remote proxying, added 2026-07-13)

- **Auth model:** localhost is always unauthenticated. With `BROWSER_LLM_API_KEY` set, non-loopback
  clients need the key on `/v1/*` + `/api/*`; pages/assets (`/`, `/ui`, `/widget.js`, `/demo`,
  `/version`, `/images/*`) stay public — image links must work in a bare `<img>`/browser, and the
  filenames are unguessable (uuid hex). CORS preflights (OPTIONS) are exempt — they can't carry auth
  headers; the real request is still checked. Decision helpers live in `authz.py` (unit-tested,
  `tests/test_authz.py`); the middleware in `server.py` is registered **after** CORSMiddleware so it
  runs before it.
- **Remote proxying:** `REMOTE_PROVIDERS="chatgpt-browser=http://<host>:8081"` +
  `REMOTE_API_KEY=<upstream key>` makes this install forward that model verbatim to the other
  instance (httpx, streaming relayed byte-for-byte; ~940s timeout ≥ the 900s max drive deadline). A
  remote mapping **overrides** the local provider of the same name and shows up in `/v1/models`,
  `/api/status` (`remote_upstream` field) and telemetry. Failures surface exactly like local ones:
  502 with detail non-streaming, in-band `[browser-llm error: remote: …]` chunk streaming. The
  proxy takes **no local lock** (the upstream's per-provider lock serializes), and lifespan skips
  pre-warming a remote default. Requires `httpx` (in requirements.txt; guarded import — local-only
  installs without it still run).
- **Proxying image input:** attachments ride along in the forwarded JSON, but a **local file path is
  rewritten to a `data:` URL first** (`_inline_paths_for_remote`, applied to `images`/`image` and to
  every content part) — the upstream would otherwise resolve the path against *its own* filesystem.
  `/v1/images/edits` proxies to the upstream's `/v1/images/edits` (multipart bytes are converted to
  `data:` URLs), so an upstream older than 0.2.0 answers 404 there.
- **This box (eben)** is set up as the upstream: systemd override
  (`~/.config/systemd/user/browser-llm-api.service.d/override.conf`) binds `0.0.0.0`, sets the API
  key + `GEMINI_PUBLIC_URL=http://192.168.1.34:8081`; ufw allows 8081 from 192.168.0.0/16 only.
- **Web UI key:** open `http://<host>:8081/#key=<key>` once — stored in localStorage, attached to
  all API fetches via a fetch wrapper. Widget: `data-key="<key>"` attr. `client.py`:
  `BROWSER_LLM_API_KEY` env.
- The venv's `pip` script has a stale shebang (venv predates a folder rename) — use
  `./venv/bin/python -m pip …`, not `./venv/bin/pip …`.

## Gotchas & conventions

- **The per-provider lock is taken INSIDE the SSE generator** in `chat_completions`
  (`server.py`). FastAPI runs the generator *after* the handler returns, so an `async with`
  around `return StreamingResponse(...)` releases the lock before the first poll and lets
  concurrent requests fight over one browser tab (that bug existed and was fixed 2026-07-08).
  Don't move it back out. Streaming failures are surfaced in-band as a
  `[browser-llm error: …]` chunk — raising would just cut the SSE dead.
- **CompletionTracker, authz and attachments have unit tests** — `./venv/bin/python -m unittest
  discover -s tests` (70 tests, no browser). If you change the done-decision logic in
  `providers/base.py` or the attachment layer in `server.py`, run/extend them.
- **CDP parser patch**: `patch_cdp()` (in `base.py`) monkeypatches `nodriver.cdp.util.parse_json_event`
  to swallow `KeyError` from unknown CDP events (e.g. `DOM.adoptedStyleSheetsModified`). Called at
  import time by `server.py` and `login.py`; call it in any new entry point.
- **`_build_prompt()` returns `(prompt, image_specs)`**, not a string — and `run_chat()` /
  `drive_once()` take `(provider, prompt, attachments)` rather than a messages list. Handlers flatten
  the messages themselves so they can validate attachments (and return a real 4xx) *before* the SSE
  generator starts.
- **nodriver gotchas hit while building uploads:** `page.evaluate()` **deep-serializes** its result,
  so it can't hand you a DOM element — use `eval_handle()` (raw `Runtime.evaluate` with
  `return_by_value=False`) and pass the RemoteObject's `object_id` to
  `cdp.dom.set_file_input_files`. Handler removal is **`page.remove_handler`** (singular):
  `remove_handlers` doesn't exist, and since ours ran inside `try/except` it silently leaked one
  `FileChooserOpened` callback per upload until fixed.
- **File-chooser interception is left ON for the tab's lifetime** (`_attach_via_chooser`). That's
  deliberate: an un-intercepted native file dialog blocks the renderer forever and this Chrome has
  no human to dismiss it. Don't "clean it up" by disabling it after an attach.
- **Attachment temp files must outlive the *upload*, not just the call** — Chrome reads them when the
  page uploads, so `_attachment_files()` wraps the whole drive as an async context manager and
  deletes them at the end. Caller-supplied paths are used in place and never deleted.
- **Never submit while an upload is in flight** — the prompt goes without the image. But don't
  require "upload idle" when *probing* which file input works either: a slow upload would look like
  a wrong input and the files get attached again to the next one (two chips → image sent twice).
  The probe waits for chips only (`require_idle=False`); `open_and_send` waits for idle before send.
- **Multipart `/v1/images/edits` needs `python-multipart`** (in requirements/pyproject). Without it
  Starlette's `request.form()` raises and the endpoint answers 400 with an install hint; the JSON
  body shape keeps working regardless.
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
  false positive after 45s. `get_response_text` also reads the Canvas side-panel editor
  (`.cm-content` / Monaco `.view-lines` / a non-composer `.ProseMirror`) and returns whichever is the LARGER
  payload — the message body or the canvas. (Was gated to "message body near-empty", which let a short intro
  like *"Here's the file:"* shadow a big canvas and return only the stub; now the canvas always competes.)
  Safe because each request opens a **fresh chat** (`open_and_send`), so any editor on the page is THIS
  answer's, never a stale prior turn; inline code blocks live inside `.markdown` and are already fenced into
  the message, so `msg` is a superset of them and only a genuine SIDE canvas can exceed it. Best-guess
  selectors — verify live if canvas answers look wrong; and CodeMirror virtualizes offscreen lines, so a very
  long canvas can still read partial. (Aside: ChatGPT sometimes *refuses* to emit very large content in one
  message and only offers a canvas — that's model behavior, not a capture bug; chunk the prompt or ask for
  inline fenced output.)
  A generated image is an `<img src="…/backend-api/estuary/content?id=file_…" alt="Generated image: …">`
  (same-origin → fetchable to base64), NOT `oaiusercontent`/`blob:`. The finished image is *not* inside
  a `data-message-author-role` element, so `image_status`/`get_images` scan the whole page.
- **ChatGPT code blocks are CodeMirror, and the stream is buffered (fixed 2026-07-08):** an inline code
  answer is NOT a plain `<pre><code class="language-x">` — it's a **CodeMirror editor** (`.cm-editor` /
  `.cm-content`, `#code-block-viewer`) with the language shown only as a toolbar pill (no `language-*`
  class) plus Copy/Run buttons, and **two** `<pre>`s (CM internals). A naive `innerText` read flattened
  the toolbar in with the code and dropped the markdown fence, so code answers came back as
  `"Python\nRun\ndef …"`. `get_response_text` now keeps `innerText` as the prose base (untouched — clean
  prose + list markers) and **surgically splices each code card into a ```fenced``` block**: it finds the
  editor, extracts the real code from `.cm-content` (or `.cm-line`s), reads the language from the toolbar,
  and replaces the card's flattened `innerText` chunk in-place (innerText-to-innerText match, so the
  substitution is reliable). Verified against the live DOM. **Because the extracted text *reshapes* near
  the end** (flattened while streaming → fenced once CM finalizes), append-only SSE deltas can't represent
  it — so ChatGPT sets `Provider.buffered_stream = True` (`providers/base.py`): `_stream_completion`
  suppresses incremental deltas and emits the final authoritative text **once** at completion
  (`CompletionTracker.text` holds the last non-empty full text). Gemini keeps incremental streaming
  (`buffered_stream = False`). Trade-off: ChatGPT answers appear all-at-once (spinner until done) instead
  of typing in — the cost of correctness for a reshaping source. Known limit: very long code may be partial
  (CodeMirror virtualizes offscreen lines); the Canvas side-panel fallback still applies when `.markdown`
  is near-empty.
- **ChatGPT session cookie is chunked**: `__Secure-next-auth.session-token.0` / `.1` (no un-suffixed
  name). `login.py` prefix-matches it and waits for it (not the DOM) before closing, so the session
  actually persists. Gemini's path is behaviorally unchanged from the original.
- **Service unit is generated, not committed** — `install-service.sh` fills
  `browser-llm-api.service.template` (`__INSTALL_DIR__` → the clone path) into
  `~/.config/systemd/user/browser-llm-api.service`; its `ExecStart` runs `serve.sh` (venv python +
  display auto-detect). No paths are hardcoded in the repo. `IMAGE_DIR` defaults to `~/Pictures/browser-llm`
  (override with `GEMINI_IMAGE_DIR`); if it isn't writable, image saving silently disables.
- **`usage` token counts are fake** — plain `.split()` word counts, not a real tokenizer.
- **Stale lock after a crash**: a hard crash leaves `<profile>/SingletonLock`; the systemd unit clears
  both profiles' locks in `ExecStartPre`. Running by hand? delete `*_profile/Singleton*`.
- **Logs**: `server.py` writes `server.log` (mode `w`, wiped each start) + stderr; under systemd the
  journal is the real log. `gemini_bot.py` writes `gemini_session.log`.
- **Dead browser/CDP connection used to wedge a provider until restart (fixed 2026-07-10):**
  `get_browser()` only recycled a cached browser after `BROWSER_RECYCLE_AFTER_IMAGES` image gens — it
  never noticed a browser that was still *in the cache* but actually dead (Chrome exited, or the CDP
  websocket dropped: `websockets.exceptions.ConnectionClosedError`, seen 2026-07-09 on a long-idle
  ChatGPT session). Every request reused the corpse and failed identically until a manual service
  restart (~20h that day), while `/api/status` kept reporting `browser_running: true`. Fixed in four
  layers (verified live by killing ChatGPT's Chrome mid-session — next request self-healed, HTTP 200):
  - `_browser_alive()` probe in `get_browser()`: `b.stopped` + a 5s `cdp.target.get_targets()` ping
    (via `Browser.send`, which also re-attaches a dropped-but-recoverable socket) — a dead cached
    browser is replaced *before* the request runs, so it succeeds instead of failing once.
  - `run_chat`/`drive_once` catch drive exceptions → `_evict_dead_browser()` pops the cache on
    transport-level errors (`ConnectionClosed`/`ConnectionError`/`OSError`, plus nodriver's
    `RuntimeError("WebSocket is not connected")`), so even mid-request death can't wedge the next one.
  - Eviction resets `_img_gen_count` — the bloat count belonged to the dead browser, not its successor.
  - `/api/status`'s `browser_running` now checks `not b.stopped` (free returncode check, no browser
    I/O), so the UI shows the truth when Chrome dies.

## Git

Default/main branch is **`master`** (there is no `main` branch), remote `origin` →
https://github.com/StaticB1/browser-llm-api. Never commit `*_profile/`, `*.log`,
`gemini_research_data.txt`, or `__pycache__/` (all gitignored).
