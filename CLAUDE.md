# CLAUDE.md

Working instructions for this repository.

## What this is

Drives a **chat web UI through an automated Chrome browser**
([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and exposes it as an
**OpenAI-compatible HTTP API**: chat completions, image generation, and image input (vision +
image-to-image), plus design tools built on ChatGPT's image tool. It uses a logged-in web session
stored in a local Chrome profile, with **no official API key** for either backend. Prompts are typed
into the page, input images are uploaded through the site's own file picker, and answers (text and
generated images) are read back out of the DOM.

**This install's own facts** (its drop-ins, its LAN address, who decided what, where the clone
lives and why its old path is a symlink) are in **`CLAUDE.local.md`**, which is gitignored and
loaded alongside this file. Read it before changing anything about the running service. This file
stays generic, because the repository is public.

@CLAUDE.local.md

Two providers, selected per request by the OpenAI **`model`** field:

| model | site | profile | images out | images in |
|-------|------|---------|------------|-----------|
| `chatgpt-browser` | chatgpt.com | `chatgpt_profile/` | yes, the default image provider | yes (verified) |
| `gemini-browser` | gemini.google.com | `gemini_profile/` | only if `IMAGE_PROVIDERS` names it | yes (chooser path) |

Unknown/absent `model` → `DEFAULT_PROVIDER` (default `chatgpt-browser`). `IMAGE_PROVIDERS` (comma
list; unset = every provider that can draw) gates image requests; others get 403.

This is inherently **fragile**: each provider depends on its site's live DOM/selectors, and ChatGPT
also sits behind Cloudflare/anti-bot. A UI change can silently break extraction or submission.

**Scope (2.x).** One person's or team's signed-in sessions on Linux, one request at a time per
provider. Forwarding a model to another install (`REMOTE_PROVIDERS`) was removed in 2.0; another
machine calls the API directly with a key. Don't bring it back without a decision.

## Dashboard and widget

**Dashboard** (`browser_llm/webui/index.html`, single file, no build step) at
`http://localhost:8081/`: a 64px rail (Chat, Studio, Gallery, Server, then search and appearance)
that becomes a bottom tab bar under 820px; a chat list beside the conversation in Chat only (a
drawer on a phone); four views addressed by hash (`#chat`, `#studio`, `#gallery`, `#server`,
pushState so Back works). Design reference linear.app: near-black `#08090A` canvas in dark, a surface ladder, hairlines, one lavender
accent `#5E6AD2`. Chat has edit-and-resend, `model_used` + seconds under each answer (from the
closing SSE chunk), a waiting turn that names the stage, Try again and an image viewer. Studio is a
split workspace. Gallery pages 60 at a time by day (IntersectionObserver). Server charts `recent`
per provider and holds the widget builder (an iframe `srcdoc` preview). `Ctrl+K` palette (a
`<dialog>`) searches chats and runs commands; `?` lists shortcuts.

- **Chat history is in `localStorage`** under `blm.chats`, newest first
  (`{id, title, model, system, msgs, created, updated}`). A new chat enters the list on its first
  turn. Image attachments are data URLs, so `saveChats()` handles a quota overflow by shedding image
  data from the oldest turns first, then whole old chats, never conversation text. Other keys:
  `blm.cur`, `blm.model`, `blm.theme`, `blm.sidebar` (desktop chat list only; the phone drawer is
  never saved), `blm.system`, `blm.imgpreset`, `blm.studio` (links only, never data: URLs),
  `blm.wbuilder`, `browserLlmApiKey`. Assistant messages may carry `model`, `used` (the model that
  answered) and `secs`. Every read goes through `store`, which survives blocked storage.
- **Theme** is 3-state (light / auto / dark), persisted under `blm.theme`; an inline script in
  `<head>` sets `documentElement.dataset.theme` **before first paint**, so keep it where it is.
  Colours are tokens on `:root` and `:root[data-theme="dark"]`, never literal hex in a rule
  (tests/test_dashboard.py checks).
- Fonts are Geist and Geist Mono (OFL) in `webui/assets/`, served at `/assets` (in package-data), so
  the UI renders offline; no CDN. Icons are one `<symbol>` sprite used through `icon(name)`.
- `md()` escapes first and marks fences with the plain-text `@@BLMCODE<n>@@`, never a NUL sentinel
  (that makes the file binary to grep and is invalid HTML). Every link and image address in an
  answer goes through `safeUrl()` (http(s), mailto, same-site; data:/blob: for images only): a
  `javascript:` link on the server's origin would reach the trusted history and delete endpoints.
- `[hidden]{display:none!important}` is needed because `.icb{display:grid}` and friends beat the
  attribute. The shell is `overflow:clip`, not hidden, so `focus()`/`scrollIntoView` can't shift it.
  `renderLog(n)` animates only turns from index n. Streaming uses an `AbortController` (send becomes
  stop).
- To work on it next to the service: `env -u DISPLAY BROWSER_LLM_PREWARM=0 BROWSER_LLM_PORT=<free
  port> ./venv/bin/browser-llm serve` (no DISPLAY, so a stray request cannot open Chrome over the
  service's windows) and mock `/v1/*` and the delete endpoints in the test browser.

**Embeddable widget** (`browser_llm/webui/widget.js`, served at `/widget.js`): a Shadow-DOM chat
bubble any LAN page adds with `<script src="http://<host>:8081/widget.js"></script>`. It finds its
API base from its own script URL (CORS is open) and streams from `/v1/chat/completions`. Its
`data-*` attributes and the `window.BrowserLLMWidget` handle (`ask()` returns a Promise and queues
while busy; `on`/`off` events) are listed in its header comment and in docs/web-ui.md;
tests/test_widget.py keeps the header comment matching the code.
- Palette via `--w-*` tokens inside the shadow root; Geist from one document-level `@font-face`
  named "BLM Geist" (an `@font-face` inside a shadow root does not apply). Own `md()` and the same
  `safeUrl()`, a stop button, full-screen under 520px.
- `data-theme` defaults to auto, following the HOST page's `prefers-color-scheme`. The thread is kept
  in `localStorage` under `blm.widget.<api base>`; the header pencil clears it, `data-persist="0"`
  turns it off. The default accent is the brand lavender in both themes; `data-accent` pins one.

## Layout

```
browser_llm/                 # THE PACKAGE. Imports inside it are relative (from . import x).
  __init__.py                #   loads .env FIRST (config.load_env), then exposes __version__.
                             #   Every module reads env at import time, so it must come first.
  __main__.py                #   python -m browser_llm == the browser-llm command.
  cli.py                     #   `browser-llm serve|login|ask|image|design|mcp|health|version`.
  config.py                  #   .env loading (environment wins) and env(new, *old) for renamed
                             #   settings (BROWSER_LLM_IMAGE_DIR, BROWSER_LLM_PUBLIC_URL). Stdlib only.
  _version.py                #   single source of truth for __version__ (pyproject reads it).
  server.py                  #   FastAPI app, port 8081. Router, completion loop, image persistence,
                             #   attachments + local-path policy, CDP patch. Serves "/", /widget.js,
                             #   /demo, /version, /api/status, /api/gallery, /api/design/kinds,
                             #   /v1/images/edits and the account-history endpoints (behind
                             #   _require_trusted + _history_provider). BROWSER_LLM_PREWARM=0
                             #   starts no browser.
  authz.py                   #   stdlib-only access control: loopback + origin trust, which paths
                             #   need the key, guest_may. Separate so tests import it cleanly.
  guests.py                  #   stdlib-only guest keys: parse_guest_keys (name:key:images),
                             #   ImageQuota (rolling 24 h per guest, reserve/commit/release, JSON
                             #   in BROWSER_LLM_STATE_DIR).
  providers/
    __init__.py              #   PROVIDERS registry, get_provider, DEFAULT_PROVIDER, IMAGE_PROVIDERS
    limits.py                #   UsageLimitReached, quota_refusal, refusal_from_message → HTTP 429
    base.py                  #   Provider ABC, StreamMonitor, CompletionTracker, open_and_send,
                             #   upload machinery, patch_cdp, RateLimited, session_tab, CHROME_ARGS.
    gemini.py                #   shadow-DOM extraction, blob→b64 images, "Upload & tools" chooser
    chatgpt.py               #   plain-DOM extraction; selector LISTS at the top; Create image mode;
                             #   model picker; usage counter; history as in-page JS (backend-api).
  design.py                  #   KINDS (13) + compose() (structural rules, no style words);
                             #   DIRECTION; run_set() (references, manifest.json, resume, --redo,
                             #   --directions/--pick, --lock, quota_plan, preview.png);
                             #   extract_tokens(), review(). Prompt part is stdlib-only (server imports it).
  gen_asset.py               #   one asset: POST generations/edits, then Pillow shaping (crop,
                             #   favicon, knockout only if no alpha, --upscale). Raises
                             #   GenAssetError (code 3 = limit), NEVER SystemExit: the MCP tool runs
                             #   it on a worker thread, where SystemExit kills the thread silently.
                             #   render_full() is shared by main() and the MCP tool; keep it so.
                             #   Falls back to sys.path imports when copied alone into a project.
  client.py                  #   stdlib client: ask(prompt, images=, ephemeral=, model_variant=)
  mcp_server.py              #   MCP stdio JSON-RPC (no `mcp` package): ask, generate_image,
                             #   design_set, design_tokens, design_review, list_models, health.
                             #   tools/call on worker threads. stdout carries protocol frames ONLY:
                             #   every log line goes to stderr.
  login.py                   #   `browser-llm login <provider>`.
  webui/                     #   index.html, widget.js, widget-demo.html, assets/ (in the wheel).
scripts/                     # each cd's to the checkout root first.
  serve.sh                   #   venv python + display auto-detect; REFUSES to start without a venv
                             #   (never falls back to system python3). Clears profile Singleton locks.
  ensure-venv.sh             #   build/repair ./venv, pip install -e .; `check` = read-only probe.
                             #   Every message to stderr (mcp.sh's stdout is the protocol channel).
  mcp.sh                     #   ensure-venv, then exec python -m browser_llm mcp.
  healthcheck.sh             #   watchdog: venv integrity + liveness + a daily model ping.
  install-service.sh         #   venv + units from deploy/systemd/ + linger.
  mode.sh                    #   service display: headless (Xvfb) / visible.
  github-settings.sh         #   repo description, topics, settings. Needs an ADMIN (push access
                             #   gets 404); --dry-run shows the calls.
deploy/systemd/              # browser-llm-api.service.template, browser-llm-health.{service.template,timer}
desktop/                     # GTK3 tray app on SYSTEM python3 (PyGObject), stdlib only, a thin HTTP
                             #   client. GTK3 on purpose: AppIndicator is GTK3-only.
docs/                        # user docs; design-upgrade.md is the 0.5.0 audit and measured run.
tests/                       # unit tests, no browser, no network
gen_asset.py design.py client.py serve.sh mcp.sh healthcheck.sh
                             # FORWARDING FILES. Outside callers run these by path (old systemd
                             #   units, MCP configs, skills, other repos' scripts). Do not delete
                             #   or "tidy" them; they are not in the wheel.
.github/                     # ci.yml (lint, tests, editable + wheel install, MCP handshake). Actions
                             #   has been billing-locked on the owner's account since 2026-08-18;
                             #   run the steps by hand (docs/releasing.md).
*_profile/                   # per-provider Chrome user-data dirs. Gitignored. NEVER move or commit:
                             #   moving one signs that account out.
.env CLAUDE.local.md         # gitignored, machine-local.
```

**Tests:** `./venv/bin/python -m unittest discover -s tests` (no browser, no network). Each file is
named for what it covers (completion tracker, authz, attachments, MCP, history, visibility, ChatGPT
page selectors, Plus limits, design, gallery records, widget, dashboard, guests). Extend them when
you change the done-decision logic, the attachment layer, or anything that decides who may read or
delete an account's history. Ruff selects E/F/W only; keep it clean.

**Deps live in `./venv`** (system Python is PEP-668): `scripts/ensure-venv.sh` builds it. Use
`./venv/bin/python -m pip …`, not `./venv/bin/pip …` (a venv older than a folder rename has a stale
shebang in its pip script). Needs Google Chrome and the `xvfb` package. Python 3.12. Pillow is a
core dependency.

## The `Provider` abstraction (`browser_llm/providers/base.py`)

Adding or altering a backend means editing a provider, not `server.py`. A provider is mostly
declarative: `name`, `chat_url`, `profile_dir`, `stream_url_fragments`, `supports_images`,
`image_text_is_caption`, `input_selector`, `send_selectors`, `load_wait`, `user_turn_selector` (a
sign of life for the "nostart" check), and for uploads `supports_upload`, `attach_click_path`,
`attachment_ready_js`, `upload_timeout`, `upload_settle`. Site-specific reads:

- `open_and_send(browser, prompt, attachments=None) -> (page, monitor)`: generic; navigates,
  attaches the `StreamMonitor`, uploads, types through `type_prompt()` (key events by default;
  ChatGPT inserts the text in one call), clicks the first working `send_selectors` (Enter fallback).
- `attach_files(page, paths) -> int` / `wait_uploads_ready(page, n)`: generic, driven by the
  declarative upload attributes.
- `get_response_text(page)`, `is_generating(page)`, `image_status(page) -> {loaded, pending,
  creating}`, `get_images(page) -> [{mime, b64|src, alt}]` (`src` = remote URL when the in-page
  fetch is CORS-blocked), `logged_in(page)` (used by `login.py`).

Everything downstream (`_stream_completion`, `run_chat`, `drive_once`, image persistence,
`StreamMonitor`, `CompletionTracker`) is generic and provider-parameterised.

## How a request flows (`browser_llm/server.py`)

1. `get_provider(req.model)` picks the provider.
2. **One persistent Chrome per provider** (`_browsers[name]`, started lazily) with its profile.
3. **Per-provider `asyncio.Lock`** (`_locks[name]`) serialises requests within a provider; the two
   providers run concurrently.
4. `_build_prompt()` flattens `messages` (system → `[Context/Instructions: …]`; multi-turn →
   `User:`/`Assistant:` labels, annotated `[N attached images]`) and returns `(prompt, specs)`.
5. `_attachment_files()` materialises the specs, then `provider.open_and_send()` uploads, types and
   submits.
6. `_stream_completion()` polls `get_response_text()`, suppressing transient status text ("Creating
   your image…", "Analyzing image") and thinking text while an image is pending.
7. **Completion** (`CompletionTracker`, unit-testable): an `<img>` rendered and stable ≥4s; or text
   unchanged ≥2.5s while not generating; or a give-up guard (generation happened but no text: 10s,
   `SILENT_PLACEHOLDER_DONE`, 45s, while a placeholder shows). Failure outcome `nostart`: nothing on the page for
   `NOT_STARTED_TIMEOUT` (60s) after sending raises an error. `open_and_send` first calls
   `ensure_visible()`. The HTTP stream signal (`cdp_fired`) is informational only. Deadline 420s, extended up to 900s while
   the answer is still streaming. Then `get_images()` runs and images are `_persist()`ed.
8. The tab is **left open on purpose**: closing or navigating away destabilises the browser.

## Image input (vision + image-to-image)

- **Wire formats:** OpenAI vision parts (`{"type":"image_url",…}`, plain-string `image_url`),
  Anthropic's `{"type":"image","source":{…base64…}}` (`ContentPart` is deliberately permissive), an
  `images: [...]` shorthand on chat, `image`/`images` on `/v1/images/generations`, and
  **`POST /v1/images/edits`** as OpenAI multipart (needs `python-multipart`; without it the endpoint
  answers 400 with an install hint) **or** the same JSON body.
- **Spec forms:** `data:` URL, bare base64, `http(s)` URL (downloaded, size-capped), `file://`, or
  a local path. Temps sniff the real extension and are deleted after the drive; caller paths are
  used in place and never deleted. `MAX_ATTACHMENTS` (6), `MAX_ATTACHMENT_MB` (20).
- **Local paths need a loopback *and* same-origin caller** (`_client_may_send_paths`), otherwise a
  keyed LAN client or any website open in the operator's browser could make the server upload any
  file to ChatGPT. `ALLOW_REMOTE_FILE_PATHS=1` opts out of both checks.
- **Upload paths:** (1) an `<input type=file>` already in the DOM (shadow-piercing, best-ranked
  first, verified by the attachment chip), then (2) **CDP file-chooser interception**
  (`Page.setInterceptFileChooserDialog`, click `attach_click_path`, `DOM.setFileInputFiles` from
  `Page.fileChooserOpened`). `wait_uploads_ready()` polls `attachment_ready_js` (`{ready, busy}`)
  for two clean samples. ChatGPT keeps hidden inputs at rest, so (1) wins; each file shows
  `button[aria-label="Remove <file name>"]`, which is the chip count and must be right, or
  `_attach_via_input` tries the next input with the same file. Gemini keeps no input at rest and needs (2); its
  `button[aria-label="Upload & tools"]` is verified but the menu-item labels are best-guess: check
  them first if a Gemini upload stops landing.
- **Input ranking:** a `capture` input ranks last; when several files go, an input without
  `multiple` ranks below every one with it; an input that takes some files gets the rest one at a
  time. Otherwise ChatGPT's single-file "Take a photo" input takes one of two references and the
  next input gets both (three images sent).
- **Don't let an input image look like a generated one.** ChatGPT's `image_status`/`get_images`
  scan the whole page (a generated image renders outside the assistant turn), and an upload has the
  same URL shape. `_isInput()` excludes anything inside the user turn (`_USER_TURN_SEL`, both
  layouts), a `form`, a `file-tile` or `[data-composer-attachments]`.
- **"Analyzing image" placeholder:** ChatGPT puts status text in the answer node marked
  `loading-shimmer aria-busy`, sometimes with no stop button. `get_response_text` returns `""` and
  `is_generating` True while it shimmers. Backstop: `_PLACEHOLDER_RE` in `CompletionTracker`, only for
  text ≤ `PLACEHOLDER_MAX_LEN` (48), so a real answer opening "Analyzing the image, …" survives.

## Image generation

- **Extraction** per provider. Gemini reads `blob:` URLs to base64 by shadow-piercing. ChatGPT's
  generated image is `<img src="…/backend-api/estuary/content?id=file_…" alt="Generated image: …">`
  (same-origin, fetchable to base64), outside the assistant turn, so the scan covers the page.
- **Storage** (`_persist`): `<IMAGE_DIR>/<provider>/<provider>_<ts>_c<12 hex conv id>_<rand>.ext`
  (the `c…_` stamp only when the conversation id was read), served at `/images/<provider>/<file>`; links use `BROWSER_LLM_PUBLIC_URL`. `_provider_slug` keeps
  providers apart. Remote-only images keep their `src`. Unwritable dir → saving is skipped
  (`_SAVE_ENABLED=False`).
- **In chat**: `_compose()` returns image-only markdown when `image_text_is_caption` is False
  (Gemini's image prose is internal thinking), text + images when True (ChatGPT).
- **`POST /v1/images/generations`** answers `{"created", "data":[{b64_json?, url?, path?, width,
  height, mime, revised_prompt?}], "model_used"}`. `n` ignored; `size`, `background`,
  `model_variant` honoured (`Provider.image_prompt` writes size and background in words; no style
  words). **501** provider can't draw, **403** `IMAGE_PROVIDERS` leaves it out, **429** with
  `Retry-After` on a usage limit, **502** no image otherwise (the detail quotes the page).
- **Usage limits** (`providers/limits.py`): before an image, `ChatGPTProvider.prepare_page` POSTs
  `/backend-api/conversation/init` in the page and reads `limits_progress`; `quota_refusal` raises
  when `image_gen.remaining <= CHATGPT_IMAGE_RESERVE` or the feature is in `blocked_features`. After a request with
  no image, `refusal_from_message` matches ChatGPT's refusal wording ("You've hit the Plus plan
  limit…", "You're generating images too quickly…") and works out the reset. The counter shows the
  plan's ceiling and does not drop as images are made, so the text parser is the stop that fires.
- **Sizes (Plus):** the tool picks a shape near the asked ratio: 1254x1254, 1672x941 (16:9),
  1731x909 (1.91:1), 1122x1402 (4:5). "Transparent background" gives a real RGBA PNG. Exact pixels
  come from gen_asset's crop.

## Design kinds, sets, tokens and review

docs/images-and-design.md is the user guide; docs/design-upgrade.md the measured run.

- **Prompts are structural by default, art-directed on request:** no style words unless
  `--direction`. tests/test_design.py checks a style-word list against every kind; a kind that needs
  one is a design change, not a fix.
- **Consistency comes from reference images, not one long conversation:** each item is a fresh chat
  with the first and the previous item attached (6 attachments at most), and the prompt says not to
  repeat the references' components, photographs or text. Continuing one thread would need a drive
  that opens an existing conversation and ignores its earlier images, which the one-tab rule makes
  delicate.
- **Upscaling is Pillow only** (Lanczos + unsharp mask); no model download.
- **A set refuses when it does not fit the day's images** unless `--partial`; the site's 429 stays
  the real stop, and a stopped set resumes.
- `/v1/images/*` take `kind`, `direction`, `theme`, `platform`, `system`, `copy_space` and answer
  `prompt_sent` and `image_tool`. A kind is composed before a remote proxy. Tokens ask ChatGPT for
  JSON with colours measured from the pixels; only interface colours go in the list.

## Ephemeral conversations

`"ephemeral": true` on a chat completion deletes the conversation once the answer is read. Unset
falls back to `BROWSER_LLM_EPHEMERAL` (`_EPHEMERAL_DEFAULT`, off).

- `Provider.discard_conversation(page)` is False in `base.py`. ChatGPT reads the id from
  `location.pathname` (`/c/<uuid>`), takes the token from `/api/auth/session`, and
  `PATCH /backend-api/conversation/<id>` `{"is_visible": false}`, exactly what the sidebar's Delete
  does; it survives a sidebar redesign.
- **A failed delete never fails the request** (`_discard` logs and swallows).
- Image generation is deliberately not covered.
- The MCP `ask` tool sets it by default (`keep_chat: true` opts out); `client.ask()` takes
  `ephemeral=`. Tests: `AskEphemeralTest` in `tests/test_mcp.py`.

## Account history: import, lazy load, cascading delete

The dashboard lists and opens the conversations already in the **provider account**, and a delete
removes the local chat, its images and the thread on the site.

- **In-page JS against the site's backend API** (`providers/chatgpt.py`: `_LIST_JS`, `_ONE_JS`,
  `_DELETE_JS`), token from `/api/auth/session`: `GET /backend-api/conversations?offset&limit&
  order=updated`, `GET /backend-api/conversation/<id>`, `PATCH … {"is_visible": false}`. No sidebar
  scraping.
- **Titles first, bodies when opened.** `/api/history` returns titles (stubs, `c.stub`);
  `/api/conversation/{id}` fills one in. Importing all bodies at once draws **429 "Too many
  requests"** and silently loses most of them. `RateLimited` (in `base.py`) maps to HTTP 429.
- **The list API's `total` is fake** (always `offset + page + 1`); paging stops on a short page.
- **Support is declared:** `Provider.supports_history` (True only for ChatGPT) and `site_origin`.
  Gemini has no readable list: `/api/history` answers 501 and the dashboard hides Import.
- **Deleting a chat cascades:** the local chat, its image files (`/api/gallery/delete`) and the
  account's conversations (`/api/conversations/delete`). Each turn is its own conversation on the
  site, so a chat records every turn's id in `convs` (`noteThread()`, `chatThreads()`). Still open:
  a turn stopped from the dashboard, or one that fails after the site made the conversation, never
  reports its id, so that conversation is left behind. The confirm dialog names all three counts.
- **Images carry their conversation in the filename** (`_conv_stamp`), so
  `/api/conversations/delete` sweeps by stamp (`_conversation_images`, returns `images_deleted`) even
  for an imported chat. The id is read **before** `_persist` (`_read_conversation_id`). Unstamped
  older images are reachable only from the gallery; never match them by timestamp.
- **Bulk delete is ONE page call** (`delete_conversations`): the token once, 8 PATCHes in flight
  (more draws HTTP 500s), and the dashboard sends ids in chunks of 40 with a progress toast. One
  call per id took about 4 s each and outlived the browser's `fetch`.
- **A delete cannot be undone** (a PATCH back to visible answers 404). Build any bulk-delete list
  from exact conversation ids, never from a time or title prefix.
- **A PATCH on an already-deleted conversation answers 404**: `_split_already_gone` returns those
  as `already_gone`, and the dashboard counts them as deleted. A 429 or 5xx is retried twice with
  backoff. Re-check reported failures against the list before believing them.
- **`_gallery_target()` is the only thing allowed to name a file for unlink**: it strips the URL and
  `/images/` prefix, percent-decodes *before* the `..` check, resolves symlinks, and requires a
  known image extension inside `IMAGE_DIR`. `tests/test_history.py` pins the refusals.
  `_unlink_saved(path)` deletes the image, then its record `_sidecar(path)` = `<image>.json`.
- **Studio images carry a record**: `_run_image_request(record=…)` writes `<image>.json` (brief,
  kind, references, prompt_sent, size, model, model_used, image_tool, width, height, created) via
  `_write_record`, which never fails the image. `/api/gallery?offset=` returns `record` and
  `has_more`. Chat-turn images get no record.
- **These endpoints are same-origin only** (`_require_trusted`); cross-origin gets 403.
- **The dashboard sends `ephemeral: false`** on every chat completion; without it an install with
  `BROWSER_LLM_EPHEMERAL=1` would delete every dashboard chat on the site. The `conversation_id`
  round-trips (`_note_conversation` → the closing SSE chunk / `body["conversation_id"]`).

## Configuration (env vars)

docs/configuration.md is the one table of every setting with its default; keep it complete when you
add one. Settings can also come from a `.env` file (config.py; the environment wins). What the
table does not say:

- Every module reads env at import time, which is why `browser_llm/__init__.py` loads `.env` first.
- `~` in `BROWSER_LLM_IMAGE_DIR` is expanded in code, because .env and systemd expand nothing.
- Renamed settings still read their old names through `config.env(new, *old)` and log once
  (`GEMINI_IMAGE_DIR`/`IMAGE_DIR`, `GEMINI_PUBLIC_URL`). `REMOTE_PROVIDERS`/`REMOTE_API_KEY` were
  removed in 2.0; a server that still sets them logs a warning and ignores them.
- `serve.sh` exports the same `DEFAULT_PROVIDER` default as the code; keep the two equal.
- `BROWSER_LLM_GUEST_KEYS` also locks the LAN out when no owner key is set.
- `BROWSER_LLM_PREWARM=0` (no browser at startup) is for a second instance and for tests; CI sets
  `BROWSER_LLM_NO_DOTENV`.

## Running

```bash
./serve.sh                                     # foreground → http://localhost:8081/v1
./venv/bin/browser-llm health                  # one-screen state; every provider "idle" = safe to restart

# background (systemd --user): venv + generated units + linger, one command:
scripts/install-service.sh
journalctl --user -u browser-llm-api -f        # logs live in the journal, NOT server.log

# the watchdog (venv integrity + liveness + a daily model ping), every 10 min:
systemctl --user list-timers browser-llm-health.timer
scripts/healthcheck.sh --deep                  # run it by hand, including the model ping
tail ~/.local/state/browser-llm-api/health.log # its log; silent when healthy

# serve.sh auto-detects the display: a working $DISPLAY (an Xvfb display counts) else its own xvfb-run.
# a second server that opens no browser (dashboard work next to the service):
BROWSER_LLM_PREWARM=0 BROWSER_LLM_PORT=8097 ./venv/bin/browser-llm serve
```

**Before any restart, read `/api/status` and don't go ahead while any provider is busy**: a restart
cuts the request in flight (the client gets a 200 with a partial reply). The service runs this
working tree, so a restart also loads whatever is on disk: make a provider change in one write, or
in a worktree, when someone else may restart the service.

## Authentication: the #1 failure mode

**Each provider needs its own login** (separate profile). A sign-in or "verify you're human" wall on
the page means that provider's session expired. **Empty answers alone do not prove that**: a hidden
page also answers nothing (see the gotcha). Read the page first: its CDP port is in
`pgrep -af 'user-data-dir=./chatgpt_profile'`, and `document.visibilityState`, the URL and the
composer tell you more than the answer does. Re-auth on a real display only when the page shows a
sign-in:

```bash
systemctl --user stop browser-llm-api
DISPLAY=:1 ./venv/bin/browser-llm login gemini   # or: chatgpt — visible Chrome opens; sign in; auto-closes
systemctl --user start browser-llm-api
```

**Use the login helper, not a normal Chrome:** `nodriver` launches Chrome with
`--password-store=basic`, a normal Chrome uses the system keyring, and cookies written by one
cannot be decrypted by the other. The service's Chrome is on Xvfb, so you can't sign in there; the
helper opens a visible window in the same cookie store. ChatGPT's session cookie is chunked
(`__Secure-next-auth.session-token.0` / `.1`); `login.py` prefix-matches it and waits for it before
closing.

## Access from other machines (API key)

- **Auth model:** localhost is always unauthenticated. With `BROWSER_LLM_API_KEY` set, non-loopback
  clients need the key on `/v1/*` + `/api/*`; pages/assets (`/`, `/ui`, `/widget.js`, `/demo`,
  `/version`, `/images/*`) stay public (image links must work in a bare `<img>`; filenames are
  unguessable). CORS preflights are exempt; the real request is still checked. Helpers in
  `browser_llm/authz.py` (`tests/test_authz.py`); the middleware is registered **after**
  CORSMiddleware so it runs before it.
- **Another machine calls the API directly** (`BROWSER_LLM_API` + `BROWSER_LLM_API_KEY` on the client).
- **Give other people a guest key, never the owner's** (`BROWSER_LLM_GUEST_KEYS`, guests.py). The
  owner's key passes `_require_trusted` (curl sends no Origin), so its holder can list, read and
  delete the account's whole history and the gallery. A guest key is checked FIRST, from any
  address, against an allowlist (`authz.guest_may`), so a later endpoint stays closed to guests.
  Its images are reserved before the request queues, committed with the saved file names, released
  on failure or no image; images drawn in a guest's chat count too, and at zero its chat gets
  `_WORDS_ONLY`. A guest's drives run ephemeral. `/api/status` for a guest drops telemetry
  (`last_error` can quote someone's page) and adds `guest`; `design.quota_plan` plans against it.
- **A leaked key** (an unknown caller in the journal): change `BROWSER_LLM_API_KEY`, restart once
  nothing is in flight, and block the address at the firewall. Saved images carry their
  conversation in the filename, so a caller's images can be traced and deleted.
- **Web UI key:** open `http://<host>:8081/#key=<key>` once (stored in localStorage, attached by a
  fetch wrapper). Widget: `data-key`. CLI and `client.ask`: `BROWSER_LLM_API_KEY`.

## Gotchas and conventions

Module names below are inside `browser_llm/`; shell scripts are in `scripts/` (the top-level copies
forward to them).

**Security**
- **Loopback is NOT trusted.** CORS is `allow_origins=["*"]` on purpose (the widget), so any website
  open in the operator's browser can POST here from `127.0.0.1`. With a local file path that was a
  file-read primitive (`_resolve_local_attachment` checks only existence and size;
  `DOM.setFileInputFiles` ignores `accept`). `authz.origin_is_trusted(origin, host)` trusts no
  `Origin` (curl/CLI/desktop) or one matching the addressed host; all three attachment endpoints use
  it. **Don't simplify the path policy back to a loopback check.**
- **`Origin: null` is NOT a missing Origin.** A sandboxed `srcdoc` iframe sends `Origin: null` and,
  with CORS at `*`, can read the reply. `null` falls through to the host comparison and fails it.
  Cost: a page opened from `file://` can't use the gated endpoints. Test:
  `test_opaque_origin_is_not_trusted`.

**Browser and tabs**
- **One tab per profile, always.** `session_tab()` reuses a tab on the site's origin or navigates
  the FIRST target (`browser.get(url)` without `new_tab`). A second live chatgpt.com tab makes the
  next drive read 0 chars to the deadline: ChatGPT multiplexes one WebSocket per profile. Opening
  and closing a tab is not a fix.
- **A tab pool cannot run two ChatGPT conversations in one Chrome** (tried and reverted): only one
  answer completes; the throttling flags change nothing. Real concurrency needs one browser per slot,
  a separate profile each (cloning, a login per profile, a Chrome's RAM each). The lock stays.
- **The per-provider lock is taken INSIDE the SSE generator** in `chat_completions`. FastAPI runs the
  generator after the handler returns, so a lock around `return StreamingResponse(...)` releases
  before the first poll. Streaming failures go in-band as a `[browser-llm error: …]` chunk.
- **Non-headless is mandatory:** the sites block headless Chrome. Background = Xvfb, never
  `--headless`.
- **ChatGPT image generation works under Xvfb** with the SwiftShader GL flags in `CHROME_ARGS`
  (images render progressively on a `<canvas>`); the service makes images on an Xvfb display. If
  canvas generation ever stalls there, run the server on a real display (`DISPLAY=:1 ./serve.sh`).
  An image takes about a minute; the 420s deadline covers slow ones.
- **A hidden page answers nothing, on both providers.** Chrome on X11 treats an off-screen or covered
  window as occluded; the page reads `visibilityState == "hidden"`, gets no animation frames, and
  both sites drop the typed prompt without rendering a turn, so a drive returns an empty 200. Three
  layers: `--disable-backgrounding-occluded-windows` in `CHROME_ARGS`; `ensure_visible()` restores
  and raises a hidden window (`Browser.setWindowBounds` + `Page.bringToFront`) or errors naming the
  cause; `nostart` fails a drive that shows nothing for 60s. Chrome persists a window's position in
  `chatgpt_profile/Default/Preferences`, so a restart alone is no fix. **Don't move or cover the
  service windows on its display, and don't remove the flag.**
- **A dead browser/CDP connection must not wedge a provider.** `_browser_alive()` in `get_browser()`
  checks `b.stopped` + a 5s `cdp.target.get_targets()` ping and replaces a dead cached browser;
  `run_chat`/`drive_once` call `_evict_dead_browser()` on transport errors (`ConnectionClosed`,
  `ConnectionError`, `OSError`, nodriver's `RuntimeError("WebSocket is not connected")`); eviction
  resets `_img_gen_count`; `/api/status`'s `browser_running` checks `not b.stopped`. Prove a change
  here by killing Chrome mid-session and checking the next request heals.
- **CDP parser patch:** `patch_cdp()` (in `base.py`) makes `nodriver.cdp.util.parse_json_event`
  swallow `KeyError` from unknown CDP events. Called at import by `server.py` and `login.py`; call it
  in any new entry point.
- **nodriver:** `page.evaluate()` deep-serialises its result, so it can't return a DOM element; use
  `eval_handle()` (raw `Runtime.evaluate`, `return_by_value=False`) and pass the RemoteObject's
  `object_id` to `cdp.dom.set_file_input_files`. Handler removal is `page.remove_handler`
  (singular); `remove_handlers` doesn't exist.
- **Stale lock after a crash:** `<profile>/SingletonLock`; `serve.sh` clears both profiles' locks on
  start. Running `browser-llm serve` directly? Delete `*_profile/Singleton*` yourself.

**Uploads**
- **File-chooser interception stays ON for the tab's life** (`_attach_via_chooser`): an
  un-intercepted native dialog blocks the renderer and nobody is there to dismiss it.
- **Attachment temps must outlive the upload**, so `_attachment_files()` wraps the whole drive.
- **Never submit while an upload is in flight** (the prompt goes without the image), but don't
  require idle when *probing* inputs (a slow upload looks like a wrong input and the file is attached
  twice). The probe waits for chips only (`require_idle=False`); `open_and_send` waits for idle.
- `_build_prompt()` returns `(prompt, image_specs)`; `run_chat()` / `drive_once()` take
  `(provider, prompt, attachments)`. Handlers flatten messages themselves so they can return a real
  4xx before the SSE generator starts.

**ChatGPT's page**
- **Every selector is a LIST** holding the layout before 11 Oct 2026 and the one after, at the top of
  `providers/chatgpt.py`. Composer: `#prompt-textarea` or `div.ProseMirror[contenteditable]` in
  `form[data-chatgpt-composer]`. CDP char events don't type "\n", so `type_prompt` inserts the whole
  prompt with one `Input.insertText`. Send: the form's `button[type=submit]` "Send" (appears only
  after typing). Answer: `[data-chatgpt-search-unit-key$=":assistant"]` and its
  `[data-markdown-text-style="assistant-message"]` root. Generating: `button[aria-label="Stop"]` and
  `[data-talvt-turn-state="in_progress"]`; a signal that sticks holds every request to the deadline,
  so only add one after watching it clear.
- **When the page is redesigned** (every request fails at "input box not found"):
  `Provider.composer_hint()` says whether the page still holds an editable element (a redesign) or
  none (a sign-in wall). Read the live page over CDP, measure every selector on a real turn, a code
  answer and an upload, and add the new layout to each list rather than replacing it.
- **ChatGPT streams over WebSocket** (`ws.chatgpt.com`), so the HTTP CDP signal never fires.
  Completion relies on `is_generating` + image stability; WS frames (`ws_url_fragments`) are only a
  heartbeat that extends the deadline.
- **Canvas false positive:** only a large, image-shaped `<canvas>` (min side ≥256px) counts as image
  generation (code editors draw on narrow canvases); "creating" with no image after generation ends
  is dropped after 45s. `get_response_text` also reads the Canvas side-panel editor (`.cm-content`,
  Monaco `.view-lines`, a non-composer `.ProseMirror`) and returns the larger payload. Selectors are
  best-guess; CodeMirror virtualises offscreen lines, so a very long canvas can read partial.
- **Code blocks are CodeMirror cards** (one `[data-markdown-copy="code-block"]` with a toolbar);
  `get_response_text` keeps `innerText` for prose and splices each card into a fenced block with the
  language from the toolbar. The text reshapes at the end, so ChatGPT sets
  `Provider.buffered_stream = True`: one final delta at completion (`CompletionTracker.text` holds
  the last non-empty full text). Gemini streams incrementally. Code comes from `.cm-content` (or
  its `.cm-line`s).
- **"Create image" mode is a pill INSIDE the editor**
  (`span[data-inline-selection-pill][data-system-hint-type="picture_v2"]`, from the + button
  `aria-label="Add files and more"`). Select-all + insert or Backspace deletes it; choosing the tool
  after typing drops it at the caret; it survives a reload as part of the draft. Hence the order in
  `prepare_page`: empty the composer, choose the tool, attach, then `type_prompt` inserts at the END;
  a text request clears any pill first. `composer_text` leaves pills out, or `_send_landed` would
  read a leftover pill as an unsent prompt. If the menu item is
  missing, it logs and sends in words (`image_tool: false`).
- **A send that doesn't land leaves a draft.** `type_prompt` selects the whole composer first and
  checks the text; `open_and_send` confirms the send (our turn shown or composer empty) and retries
  one click, then Enter.
- **The model picker** (`button[aria-label="Select ChatGPT model"]`, measured on Plus 13 Oct 2026):
  its label shows the power word ("High"; GPT-5.6 Sol shows "5.6 Instant", so `effort_matches`
  looks for the word). The menu has `[data-model-picker-power-slider]` with three
  `span[data-selected]` dots and `[role=menuitemradio]` rows with `aria-checked`, inert until
  `[data-model-picker-view-toggle]` is clicked. Clicks must be real CDP mouse events (`_click_xy`);
  Escape closes it. The choice persists, so `select_variant` caches it. The cookies
  `oai-last-model-config` / `oai-default-model-config` do NOT track the picker. `?model=` in the URL
  is not honoured (it redirects to bare chatgpt.com).
- **What answered comes from the conversation record**, not the picker:
  `GET /backend-api/conversation/<id>` gives `metadata.model_slug`, `resolved_model_slug` and
  `thinking_effort`. Image turns are routed by the site to another model whatever the picker says,
  and a record can name no model at all. Fallback: the page's own request body (`model`,
  `thinking_effort` on `/backend-api/f/conversation`), which Chrome only puts in `requestWillBeSent`
  when `Network.enable` names `maxPostDataSize` (`StreamMonitor.enable_bodies` does).
- **Open: empty answers at GPT-6 High** on two long evaluation prompts (not reproduced; Medium
  answered). `get_response_text` logs "answer read failed" with the cause (at most every 30 s): read
  that line first, and keep the page (`ephemeral: false`) to inspect its DOM.

**Gemini**
- **Gemini image generation failed for the account tested** (no image in ~15s, 502 "did not return
  an image"; Gemini says image creation is unavailable). Account/region, not code. An install can
  refuse Gemini images with `IMAGE_PROVIDERS=chatgpt-browser` (403).

**Service, venv and logs**
- **The service unit is generated, not committed:** `scripts/install-service.sh` fills
  `deploy/systemd/browser-llm-api.service.template` (`__INSTALL_DIR__`) into
  `~/.config/systemd/user/browser-llm-api.service`, `ExecStart` = `scripts/serve.sh`. A unit from
  before 2.0 runs the top-level `serve.sh`, which forwards. No paths are hardcoded in the repo.
- **Never reinstate a system-python fallback in `serve.sh`.** System python can never hold nodriver,
  so it only crash-loops. A running process keeps its imports, so a deleted venv stays invisible
  until the next restart; `ensure-venv.sh` rebuilds it, `healthcheck.sh` probes integrity as well as
  liveness every 10 minutes, and what it can't repair raises a `critical` alert.
- **The watchdog's daily deep check spends one real turn per provider**, on purpose: an expired
  session leaves the server healthy while returning nothing. Keep it one short ephemeral completion.
  It waits while a provider is busy (`deep postponed` in the log, `deep-last` left unset, retried
  every run): a ping queues behind the request in flight, and a GPT-6 High answer takes up to ten
  minutes, so pinging a busy provider times out at 180 s and raises a false alert.
- **`usage` token counts are fake** (`.split()` word counts).
- **Logs:** `server.py` writes `server.log` (mode `w`, relative path, wiped each start) + stderr;
  under systemd the journal is the real log. Importing `server.py` from the repo directory
  truncates the live `server.log`, so import it from elsewhere.

## Git

Default branch **`master`** (no `main`), remote `origin` → https://github.com/StaticB1/browser-llm-api.
Never commit `*_profile/`, `*.log`, `gemini_research_data.txt`, or `__pycache__/` (all gitignored).
Releases follow docs/releasing.md.
