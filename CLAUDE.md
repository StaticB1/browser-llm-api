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

**This install's own facts** (who decided what, its drop-ins, its LAN address, where the clone
lives on this machine and why its old path is a symlink) are in **`CLAUDE.local.md`**, which is
gitignored and loaded alongside this file. Read it before changing anything about the running
service. This file stays generic, because the repository is public.

@CLAUDE.local.md

A **web dashboard** (`browser_llm/webui/index.html`, single file, no build step) is served at
`http://localhost:8081/`. Layout and colour follow the conventions people know from ChatGPT: a
chat-history sidebar, one centred reading column, plain assistant prose, grey user bubbles and a
rounded composer. Four views, switched from the sidebar: Chat, Create image (generation and
image-to-image), Gallery, and Server (per-provider telemetry, backed by `/api/status` +
`/api/gallery`).

**Chat history lives in `localStorage`** under `blm.chats`, newest first, one object per
conversation (`{id, title, model, system, msgs, created, updated}`). The sidebar groups it by
day (Today / Yesterday / Previous 7 days / Previous 30 days / month), searches title and body,
and offers rename plus delete per row and delete-all from the top-bar kebab. A new chat is a
draft: it only enters the list on its first turn. Image attachments are data URLs, so `saveChats()`
handles a quota overflow by shedding image data from the oldest turns first, then whole old chats,
never conversation text. Other keys: `blm.cur` (last open chat), `blm.model`, `blm.theme`,
`blm.sidebar`, `blm.system` (default system prompt), `browserLlmApiKey`.

Before editing the dashboard, note the following. The theme switch is 3-state
(light / auto / dark) in the sidebar footer, persisted under `blm.theme`, and an inline script in
`<head>` resolves it onto `documentElement.dataset.theme` **before first paint**, so keep that
script where it is. Colours are CSS custom properties on `:root` (light) and
`:root[data-theme="dark"]`; add new colours as tokens, never as literal hex in a rule. Fonts are
local-only on purpose, Inter if installed else system, so the UI still renders offline. `md()` is a
small block grammar (headings, lists, tables, quotes, fenced code cards with Copy) that escapes
first and uses the plain text marker `@@BLMCODE<n>@@` for fences, never a NUL sentinel, which would
make the file binary to grep and is invalid in HTML source. A rule sets
`[hidden]{display:none!important}` because `.sq{display:grid}` otherwise beats the `hidden`
attribute and the top-bar buttons stay visible with the sidebar open. Streaming uses an
`AbortController`, so the send button becomes a stop button mid-answer.

There is also an **embeddable chat widget** (`browser_llm/webui/widget.js`, served at `/widget.js`): a
self-contained, Shadow-DOM-isolated floating chat bubble that any other page on the LAN can add with
`<script src="http://<host>:8081/widget.js"></script>`. It auto-discovers this server as its API base
from its own script URL (CORS is already open) and streams from `/v1/chat/completions`. Config via
`data-*` attrs (`provider`, `title`, `accent`, `position`, `greeting`, `system`, `open`, `attach`,
`theme`, `persist`, `key`); runtime handle `window.BrowserLLMWidget`
(`open`/`close`/`reset`/`config`). The Server view shows a copy-paste embed snippet and a
"Preview widget" button. The panel carries the same palette as the dashboard via `--w-*` tokens
inside its shadow root, its own `md()` with code cards, a stop button, and full-screen sizing under
520px. `data-theme` defaults to auto, which follows the HOST page's `prefers-color-scheme`, so it
never lands dark-on-light. The thread is remembered in `localStorage` under
`blm.widget.<api base>` and survives a reload; the header pencil clears it, and
`data-persist="0"` turns that off. `data-accent` is unset by default so the send button and bubble
follow the theme, black on light and white on dark; setting it pins one colour for both themes.

Two providers, selected per-request by the OpenAI **`model`** field:

| model | site | profile | images out | images in |
|-------|------|---------|------------|-----------|
| `chatgpt-browser` | chatgpt.com | `chatgpt_profile/` | yes, the default image provider | yes (verified) |
| `gemini-browser` | gemini.google.com | `gemini_profile/` | only if `IMAGE_PROVIDERS` names it | yes (chooser path) |

Unknown/absent `model` → `DEFAULT_PROVIDER` (default `chatgpt-browser`). `IMAGE_PROVIDERS` (comma
list; unset = every provider that can draw) gates image requests; others get 403.

This is inherently **fragile**: each provider depends on its site's live DOM/selectors, and ChatGPT
additionally sits behind Cloudflare/anti-bot. A UI change can silently break extraction or submission.

**Scope (2.0).** One person's or team's signed-in sessions on Linux, one request at a time per
provider. Forwarding a model to another install (`REMOTE_PROVIDERS`) was removed in 2.0; another
machine calls the API directly with `BROWSER_LLM_API_KEY`. Don't bring it back without a decision.

## Layout (2.0)

```
browser_llm/                 # THE PACKAGE. Imports inside it are relative (from . import x).
  __init__.py                #   loads .env FIRST (config.load_env), then exposes __version__.
                             #   Every module reads env at import time, so the file must be
                             #   applied before any submodule imports; that is why it is here.
  __main__.py                #   python -m browser_llm == the browser-llm command.
  cli.py                     #   `browser-llm serve|login|ask|image|design|mcp|health|version`.
                             #   Dispatches to each module's main() with sys.argv set.
  config.py                  #   .env loading (environment wins) and env(new, *old) for the
                             #   2.0 names: BROWSER_LLM_IMAGE_DIR (was GEMINI_IMAGE_DIR/IMAGE_DIR),
                             #   BROWSER_LLM_PUBLIC_URL (was GEMINI_PUBLIC_URL). Stdlib only.
  _version.py                #   single source of truth for __version__ (pyproject reads it).
  server.py                  #   FastAPI app, port 8081. Model→provider router, the generic
                             #   completion loop, image persistence, attachment materialization +
                             #   local-path policy, CDP patch. Serves "/" + /widget.js + /demo +
                             #   /version + /api/status (+ per-provider telemetry) + /api/gallery +
                             #   /api/design/kinds, /v1/images/edits (multipart or JSON), and the
                             #   account-history endpoints behind _require_trusted +
                             #   _history_provider. _gallery_target is the ONLY thing allowed to
                             #   name a file for unlink. BROWSER_LLM_PREWARM=0 starts no browser.
  authz.py                   #   stdlib-only access control: loopback + origin trust, which
                             #   paths need the key. Separate so tests import it without the
                             #   server's side effects.
  providers/
    __init__.py              #   PROVIDERS registry, get_provider, DEFAULT_PROVIDER, IMAGE_PROVIDERS
    limits.py                #   UsageLimitReached, quota_refusal, refusal_from_message → HTTP 429
    base.py                  #   Provider ABC, StreamMonitor, CompletionTracker, generic
                             #   open_and_send, upload machinery (file input ranked: multi-file
                             #   first, camera last; CDP chooser fallback), patch_cdp, RateLimited,
                             #   history hooks (session_tab reuses the ONE tab).
    gemini.py                #   shadow-DOM extraction, blob→b64 images, "Upload & tools" chooser
    chatgpt.py               #   plain-DOM extraction; selector LISTS at the top (both page layouts);
                             #   Create image mode; model picker; usage counter; history layer as
                             #   in-page JS against the site's backend-api.
  design.py                  #   KINDS (13) + compose() (STRUCTURAL rules, no style words);
                             #   DIRECTION (--direction) per group; run_set() (one image per item,
                             #   first + previous as references, manifest.json, resume, --redo,
                             #   --directions/--pick, --lock, quota_plan refusal, preview.png);
                             #   extract_tokens() and review() (JSON from ChatGPT). The prompt part
                             #   is stdlib-only because server.py imports it.
  gen_asset.py               #   one asset: POST generations/edits, then Pillow shaping (crop,
                             #   favicon, knockout only if no alpha, --upscale). Raises
                             #   GenAssetError (code 3 = limit), NEVER SystemExit: the MCP tool runs
                             #   it on a worker thread, where SystemExit killed the thread silently.
                             #   render_full() is shared by main() and the MCP tool; keep it so.
                             #   Falls back to sys.path imports when copied alone into a project.
  client.py                  #   stdlib client: ask(prompt, images=, ephemeral=, model_variant=)
  mcp_server.py              #   MCP stdio JSON-RPC (no `mcp` package): ask, generate_image,
                             #   design_set, design_tokens, design_review, list_models, health.
                             #   Thin client of the HTTP API. tools/call on worker threads; main()
                             #   joins them on EOF. stdout carries protocol frames ONLY: every log
                             #   line goes to stderr or the client sees corrupt JSON.
  login.py                   #   sign-in helper; cli() is `browser-llm login <provider>`.
  webui/                     #   index.html (dashboard), widget.js, widget-demo.html. Shipped in
                             #   the wheel (package-data); server finds them next to server.py.
scripts/                     # each cd's to the checkout root ("$(dirname "$0")/..") first.
  serve.sh                   #   venv python + display auto-detect; runs `python -m browser_llm
                             #   serve`. Calls ensure-venv.sh and REFUSES to start without a venv;
                             #   never falls back to system python3. Clears profile Singleton locks.
  ensure-venv.sh             #   build/repair ./venv, pip install -e .; `check` = read-only probe.
                             #   Every message to stderr (mcp.sh's stdout is the protocol channel).
  mcp.sh                     #   ensure-venv, then exec python -m browser_llm mcp.
  healthcheck.sh             #   watchdog: venv integrity + liveness + a daily model ping.
  install-service.sh         #   venv + units from deploy/systemd/ + linger.
  mode.sh                    #   service display: headless (Xvfb) / visible.
deploy/systemd/              # browser-llm-api.service.template, browser-llm-health.{service.template,timer}
desktop/                     # GTK3 tray app on SYSTEM python3 (PyGObject), stdlib only, a thin HTTP
                             #   client. GTK3 on purpose: AppIndicator is GTK3-only.
docs/                        # api, images-and-design, agent-image-guide, mcp, web-ui,
                             #   configuration, running, architecture, quickstart, releasing,
                             #   design-upgrade (the 0.5.0 audit and live test), img/
tests/                       # unit tests, no browser, no network (see "Tests" below)
gen_asset.py design.py client.py serve.sh mcp.sh healthcheck.sh
                             # FORWARDING FILES. Outside callers run these by path (systemd units
                             #   installed before 2.0, Claude's MCP config, skills, other repos'
                             #   scripts, a co-maintainer's machine). Do not delete or "tidy" them;
                             #   they are not in the wheel.
CHANGELOG.md CONTRIBUTING.md SECURITY.md LICENSE README.md .env.example
.github/                     # ci.yml (lint, tests, editable + wheel install, MCP handshake),
                             #   issue and PR templates. Actions has been billing-locked on the
                             #   owner's account since 2026-08-18; run the steps by hand.
*_profile/                   # per-provider Chrome user-data dirs. Gitignored. NEVER move or commit:
                             #   moving one signs that account out.
.env CLAUDE.local.md         # gitignored, machine-local.
```

**Tests:** `./venv/bin/python -m unittest discover -s tests` (no browser). test_completion_tracker,
test_authz (key gating, origin trust), test_attachments (specs, limits, local-path + origin policy),
test_mcp (framing, tools/list contract, error mapping, off-the-read-loop dispatch), test_history
(trusted-caller gating, 501s, _gallery_target refusals), test_visibility (occlusion flag,
ensure_visible), test_chatgpt_page (every selector list keeps both layouts), test_plus (limits →
429, model_variant, size words, IMAGE_PROVIDERS 403, gen_asset sizes/variants/exit 3, draft
replaced not appended), test_design (kinds' words with no style word unless --direction, set
references/resume/redo/directions/pick, quota, tokens/review parsing, server kind fields,
gen_asset --kind/--upscale, Create image mode, file-input ranking under node, partial attach,
failed answer read logged), test_v2 (.env, renamed settings, the command). Ruff selects E/F/W only
(bugbear's B904 would touch eight exception handlers for no behaviour change); keep it clean.

**Deps live in `./venv`** (system Python is PEP-668): `scripts/ensure-venv.sh` builds it and installs
the package editable. Run with `./serve.sh` (foreground) or `scripts/install-service.sh` (service).
Needs Google Chrome and the `xvfb` package. Python 3.12. Pillow is a core dependency since 2.0.

## The `Provider` abstraction (`browser_llm/providers/base.py`)

Adding/altering a backend means editing a provider, not `server.py`. A provider is mostly
declarative — class attributes `name`, `chat_url`, `profile_dir`, `stream_url_fragments` (CDP
completion signal), `supports_images`, `image_text_is_caption`, `input_selector`, `send_selectors`,
`load_wait`, `user_turn_selector` (the submitted prompt's own element, a sign of life for the
"nostart" check), and for image input `supports_upload`, `attach_click_path`, `attachment_ready_js`,
`upload_timeout`, `upload_settle` — plus site-specific reads:

- `open_and_send(browser, prompt, attachments=None) -> (page, monitor)` — **generic in base**;
  navigates, attaches the `StreamMonitor`, uploads any `attachments` (see the image-input section),
  types into `input_selector` through `type_prompt()` (key events by default; ChatGPT inserts the
  text in one call), clicks the first working `send_selectors` (Enter fallback). Override
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

## How a request flows (`browser_llm/server.py`)

1. `get_provider(req.model)` picks the provider.
2. **One persistent Chrome per provider** (`_browsers[name]`, started lazily) with that provider's profile.
3. **Per-provider `asyncio.Lock`** (`_locks[name]`) serializes requests within a provider; Gemini and ChatGPT can run concurrently.
4. `_build_prompt()` flattens the OpenAI `messages` array (system → `[Context/Instructions: …]` preamble; multi-turn → `User:`/`Assistant:` labels) and returns `(prompt, image specs)`.
5. `_attachment_files()` materializes those specs into files, then `provider.open_and_send()` opens the chat, uploads them, types, submits (temp files are cleaned up when the drive ends).
6. `_stream_completion()` polls, yielding text deltas from `provider.get_response_text()`. It **suppresses** transient status text — "Creating your image…" / "Analyzing image" (see `CompletionTracker._PLACEHOLDER_RE`, short text only) and thinking text while `image_status` reports an image pending — and keeps waiting until the `<img>` renders.
7. **Completion**: the `CompletionTracker` (in `base.py`, unit-testable without a browser) is fed one poll sample at a time and decides done via: image-stability (an `<img>` rendered and stable ≥4s), or text settled (text unchanged ≥2.5s while not generating), or a give-up guard (generation happened but no text — 10s, stretched to 45s while a status placeholder is on screen). It also has one failure outcome, `nostart`: nothing at all on the page (no user turn, text, generation or image) for `NOT_STARTED_TIMEOUT` (60s) after sending, which `_stream_completion` raises as an error instead of riding the deadline to an empty answer. Before any of that, `open_and_send` calls `ensure_visible()`, which refuses to drive a hidden page (see the hidden-page gotcha). The `StreamMonitor`'s HTTP `stream_url_fragments` signal (`cdp_fired_at`) is informational only. Deadline is progress-aware: base 420s, **extended up to 900s while the answer is still actively streaming** (text still growing or WebSocket frames still arriving), so long code/HTML answers aren't truncated. Then `provider.get_images()` runs and images are `_persist()`ed + appended.
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
- **Local paths need a loopback *and* same-origin caller** (`_client_may_send_paths`): a keyed LAN
  client could otherwise make the server upload any file off the server's machine to ChatGPT — and so could any
  website open in the operator's browser, since CORS is wide open and a drive-by POST arrives from
  127.0.0.1 (see the origin gotcha below). `ALLOW_REMOTE_FILE_PATHS=1` opts out of both checks.
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
- **ChatGPT** (verified live 2026-07-28, again 2026-10-12): hidden file inputs exist at rest, so
  path (1) always wins. Until 11 Oct 2026 that was `input[data-testid="upload-photos-input"]` and
  each file rendered a 144px tile with `button[aria-label^="Remove file …"]`; since then there are
  four inputs labelled only by aria-label ("Take a photo", "Attach photos or videos", "Attach
  photos", "Attach files"; the top-ranked one took the file) and each file shows
  `button[aria-label="Remove <file name>"]`. That button is the chip count, and it must be right:
  "no chip" makes `_attach_via_input` try the next input with the same file, up to four times. **Gemini** keeps *no*
  file input at rest, so it needs path (2); its `button[aria-label="Upload & tools"]` is verified but
  the menu-item labels are best-guess (the box's Gemini profile was signed out) — check those first
  if a Gemini upload stops landing.
- **Don't let an input image look like a generated one.** ChatGPT's `image_status`/`get_images` scan
  the *whole page* (a generated image renders outside the assistant turn), and an uploaded image has
  the same `blob:`/`content?` URL shape. `_isInput()` excludes anything inside the user turn
  (`_USER_TURN_SEL`: `[data-message-author-role="user"]` before 11 Oct 2026,
  `[data-chatgpt-search-unit-key$=":user"]` since, where the sent image sits outside the bubble),
  a `form`, a `file-tile` or `[data-composer-attachments]` — without it every vision request
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
- **Endpoint** `POST /v1/images/generations`: `{"created", "data":[{b64_json?, url?, path?, width, height, mime, revised_prompt?}], "model_used"}`. `n` ignored; `size`, `background` and `model_variant` are honoured (`Provider.image_prompt` writes size and background into the prompt in words, ChatGPT's adds "Use the image generation tool to create this image."; no style words, per the design-with-chatgpt rule). **501** if the provider doesn't support images, **403** if `IMAGE_PROVIDERS` leaves it out, **429** with `Retry-After` on a usage limit (`providers/limits.py`), **502** if it returned none otherwise (the detail quotes the page).
- **Usage limits** (`providers/limits.py`). Before an image, `ChatGPTProvider.prepare_page` POSTs `/backend-api/conversation/init` from inside the page and reads `limits_progress`; `quota_refusal` raises `UsageLimitReached` when `image_gen.remaining <= CHATGPT_IMAGE_RESERVE` or the feature is in `blocked_features`. After a request with no image, `refusal_from_message` matches ChatGPT's second-person refusal wording ("You've hit the Plus plan limit for image generation ... resets in 23 hours", "You're generating images too quickly ... wait for 3 minutes") and works out the reset. Both become 429. On 13 Oct 2026 the counter read 120 (24 h window) and did not drop after 7 images, so the text parser is the backstop that will actually fire.

## Design kinds, sets, tokens and review (added 2026-10-14)

Image-first design, as Codex does with GPT-6 Astra and Images 2.0: one large readable image per
section, references for consistency, then build and compare screenshots. docs/design-upgrade.md is
the audit, the decisions and the live test; docs/images-and-design.md is the user guide.

- **Prompts are structural by default and art-directed on request.** No style words unless
  `--direction`. tests/test_design.py checks a list of style words against every kind; a kind
  that needs one is a design change, not a fix.
- **Consistency comes from reference images, not one long conversation.** Each item is a fresh
  chat with the first item and the previous one attached (6 attachments at most), and the prompt
  says not to repeat the references' components, photographs or text (the first live run copied
  the hero's search bar into section 2 without that line). Continuing one thread so the image tool
  sees its own images is a possible later step; it needs a drive that opens an existing
  conversation and ignores its earlier images, which the one-tab rule makes delicate.
- **Upscaling is Pillow only** (Lanczos + unsharp mask); no model download.
- **A set refuses when it does not fit the day's images** unless `--partial`; the counter
  overstates, so the site's 429 stays the real stop, and a stopped set resumes.
- `/v1/images/*` take `kind`, `direction`, `theme`, `platform`, `system`, `copy_space` and answer
  `prompt_sent` and `image_tool`. Tokens ask ChatGPT for JSON and hand it colours measured from the
  pixels; only interface colours belong in the list (photo tones go under imagery).

## Ephemeral conversations (added 2026-08-20)

`"ephemeral": true` on a chat completion deletes the conversation once the answer has been read, for
callers whose prompts are tooling rather than someone's own chat. Unset falls back to
`BROWSER_LLM_EPHEMERAL` (`_EPHEMERAL_DEFAULT`, off), so an install can flip the default for clients
that predate the field.

- `Provider.discard_conversation(page)` returns False in `base.py`; a provider that can reach its own
  history overrides it. It runs after `get_images()`, on the same page, before the drive returns.
- **ChatGPT does it over the site's own backend API, not the sidebar menu** — read the conversation id
  out of `location.pathname` (`/c/<uuid>`), take the access token from `/api/auth/session`, then
  `PATCH /backend-api/conversation/<id>` with `{"is_visible": false}`. That is exactly what the
  sidebar's own Delete does (a soft delete), and it survives a sidebar redesign. Verified live
  2026-08-20: the journal logged `conversation deleted (ephemeral request)`.
- **A failed delete never fails the request** (`_discard` logs and swallows) — the answer is already
  in hand, and throwing it away because a cleanup call 404'd would be the worse trade.
- Image generation is deliberately **not** covered: an asset you keep usually comes with a thread you
  want to go back to.
- The MCP `ask` tool sets it by default and takes `keep_chat: true` to opt out; `client.ask()` takes
  `ephemeral=`. Tests: `AskEphemeralTest` in `tests/test_mcp.py`.

## Account history: import, lazy load, cascading delete (added 2026-08-20)

The dashboard can list and open the conversations that already exist in the **provider account**,
not just the ones this browser created, and a delete here means the local chat, the images it
generated and the thread on the site.

- **All of it runs as in-page JS against the site's own backend API**, from inside the logged-in
  tab — `providers/chatgpt.py`, `_LIST_JS` / `_ONE_JS` / `_DELETE_JS`. The access token comes from
  `/api/auth/session`; the endpoints are `GET /backend-api/conversations?offset&limit&order=updated`,
  `GET /backend-api/conversation/<id>` and `PATCH /backend-api/conversation/<id>`
  `{"is_visible": false}`. No scraping of the sidebar: a redesign doesn't break it.
- **Titles first, bodies one at a time.** `/api/history` returns titles only and the dashboard
  writes them as **stubs** (`c.stub`); `/api/conversation/{id}` fills one in when it is opened, and
  the filled chat is saved. Loading bodies only when opened matters: importing 200 bodies up front makes the
  site answer **429 "Too many requests"** partway through and the import silently lost 131 of 200
  conversations before this was redesigned. The per-thread read is the rate-limited call; the list
  call is not. `RateLimited` (in `base.py`) maps to HTTP 429 so the UI can say "wait a minute"
  instead of reporting the history as broken.
- **The list API's `total` is fake** — it answers `offset + page + 1` forever. Paging stops on a
  short page instead, and `has_more` in the response is derived that way.
- **Provider support is declared, not assumed:** `Provider.supports_history` (False in `base.py`,
  True for ChatGPT) and `site_origin`. Gemini exposes no readable list, so `/api/history` answers
  501 and the dashboard hides Import for it. Forwarded models no longer exist (removed in 2.0).
- **Deleting a chat cascades**, in the UI's own words on the toast: the local chat, its saved image
  files (`/api/gallery/delete`, matched out of the message text by the `/images/<provider>/<file>`
  paths) and the account's conversation (`/api/conversations/delete`). The confirm dialog names all
  three counts before it does any of it.
- **A saved image carries its conversation in the filename** (`_conv_stamp`, 2026-08-20):
  `chatgpt_<ts>_c<12 hex of the conversation id>_<rand>.png`. Matching images out of the message
  text only works for a chat this dashboard ran itself — an **imported** chat holds the site's own
  image URLs and names no local file, so deleting it left the pictures on disk with nothing left to
  ever link them. `/api/conversations/delete` now sweeps by stamp (`_conversation_images`) and
  returns `images_deleted`, so the delete works with no help from the client. The id is therefore
  read **before** `_persist`, not after (`_read_conversation_id`); `_note_conversation` is now just
  the handoff to the client. Images saved before this are unstamped and unmatchable — guessing from
  timestamps would delete the wrong picture, so they are only reachable from the gallery.
- **Bulk delete is ONE page call, not one per id** (`delete_conversations`, 2026-08-20). The naive
  per-id shape cost ~4.2s each — a CDP round trip plus its own `/api/auth/session` read — so a real
  196-chat clear-out ran for 13 minutes, and the browser's `fetch` gave up long before the end: the
  work finished server-side, uvicorn never logged a response line because the client had gone, and
  the operator saw the sidebar empty with no toast and concluded nothing had happened. The batch JS
  reads the token once and runs 8 PATCHes in flight; measured on 36 ids, 34.6s at 4 concurrent requests, 17.5s at
  8, 17.2s at 16 with an HTTP 500 appearing — the site stops rewarding parallelism around 8. The
  dashboard also sends ids in chunks of 40 with a running progress toast, so a long delete reports
  as it goes instead of at the end.
- **A PATCH on an already-deleted conversation answers 404**, which is how to check whether a thread
  is really gone; a 429 or 5xx is retried twice with backoff, a 404 is reported as-is. A batch that
  reports failures is worth re-checking against the list before believing them: the 4 that failed
  with `TypeError: Failed to fetch` in the 13-minute run had in fact been deleted.
- **`_gallery_target()` is the only thing allowed to name a file for unlink**: it strips the URL and
  the `/images/` prefix, percent-decodes *before* the `..` check, resolves symlinks, and requires
  the result to sit inside `IMAGE_DIR` with a known image extension. `tests/test_history.py` pins
  the refusals.
- **These four endpoints are same-origin only** (`_require_trusted`), because they read and delete
  someone's real history. Cross-origin gets 403 with an explanation. See the `Origin: null` gotcha
  below — that one is subtle and was live.
- **The dashboard sends `ephemeral: false`** on every chat completion. It has to: an install can set
  `BROWSER_LLM_EPHEMERAL=1` for its tooling callers, and without the field every chat the dashboard started
  deleted itself on the site and the returned `conversation_id` pointed at nothing. The id now
  round-trips (`_note_conversation` → the closing SSE chunk / `body["conversation_id"]`) and is what
  makes a cascading delete possible for a chat you just had.

## Configuration (env vars)

All of them can also come from a `.env` file (config.py; the environment wins). docs/configuration.md
is the user-facing table; this one carries the reasons.

| Var | Default | Meaning |
|-----|---------|---------|
| `DEFAULT_PROVIDER` | `chatgpt-browser` | Provider used when `model` is unknown/absent. `serve.sh` exports the same default (it used to export `gemini-browser`, which beat the code). |
| `IMAGE_PROVIDERS` | *(every provider that can draw)* | Comma list of providers allowed to make images; others get 403. |
| `CHATGPT_MODEL` | `GPT-6` | Picker row selected before each ChatGPT request. |
| `CHATGPT_EFFORT` | `high` | Picker power slider: `instant` / `medium` / `high` (aliases fast, standard, extended). |
| `CHATGPT_IMAGE_RESERVE` | `0` | Images on ChatGPT's own counter that requests here must leave for the account owner. |
| `CHATGPT_READ_MODEL` | `1` | Read the conversation record after each answer for `model_used` (one extra GET). |
| `CHATGPT_IMAGE_TOOL` | `1` | Put the composer in the site's Create image mode before an image request; `0` = words only. |
| `BROWSER_LLM_IMAGE_DIR` | `~/Pictures/browser-llm` | Base dir for saved images, one subfolder per provider. Pre-2.0 `GEMINI_IMAGE_DIR` / `IMAGE_DIR` still read (config.env logs once). `~` is expanded in code because .env and systemd expand nothing. |
| `BROWSER_LLM_PUBLIC_URL` | `http://localhost:8081` | Base URL of returned image links. Pre-2.0 `GEMINI_PUBLIC_URL` still read. |
| `BROWSER_RECYCLE_AFTER_IMAGES` | `8` | Recycle a provider's browser after this many image gens. Was 3 for the free plan's renderer bloat; on Plus the renderer stayed at 479-548 MB settled, 754-813 MB peak, over 7 images (13 Oct 2026). |
| `MAX_ATTACHMENTS` | `6` | Most input images one request may attach. |
| `MAX_ATTACHMENT_MB` | `20` | Per-attachment size ceiling (data URLs, downloads and local files alike). |
| `BROWSER_LLM_HOST` | `127.0.0.1` | Interface to bind. Localhost-only by default (2026-08-11): the server drives logged-in accounts, so LAN exposure is opt-in. |
| `BROWSER_LLM_PORT` | `8081` | Port to bind. |
| `BROWSER_LLM_API_KEY` | *(unset)* | When set, **non-loopback** clients must send it (`Authorization: Bearer …` or `X-Api-Key`) on `/v1/*` and `/api/*`. Localhost stays open; pages/assets (`/`, `/widget.js`, `/images/*`, …) stay public. Makes binding to `0.0.0.0` sane. |
| `ALLOW_REMOTE_FILE_PATHS` | *(unset)* | Let **non-loopback or cross-origin** clients attach server-side file paths. Off by default — it's a file-read primitive. |
| `BROWSER_LLM_EPHEMERAL` | `0` | Delete the conversation after any chat completion that doesn't set `ephemeral` itself. |
| `BROWSER_LLM_PREWARM` | `1` | `0` starts no browser at startup: for a second instance (port 8097 works) that must not open Chrome over the service's windows, and for tests. |
| `BROWSER_LLM_ENV_FILE` / `BROWSER_LLM_NO_DOTENV` | *(unset)* | Read this .env instead / read none. CI sets NO_DOTENV. |
| `REMOTE_PROVIDERS`, `REMOTE_API_KEY` | — | Removed in 2.0. A server that still sets `REMOTE_PROVIDERS` logs a warning and ignores it. |

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

## Authentication — the #1 failure mode

**Each provider needs its own login** (separate profile). A sign-in or "verify you're human" wall on
the page ⇒ that provider's session expired. **Empty answers alone do not prove that**: from
2026-09-18 to 09-26 both providers answered nothing while both were signed in, because their pages
were hidden (see the hidden-page gotcha). Read the page first — its CDP port is in
`pgrep -af 'user-data-dir=./chatgpt_profile'`, and `document.visibilityState`, the URL and the
composer tell you more than the answer does. Re-auth on a real display only when the page shows a
sign-in:

```bash
systemctl --user stop browser-llm-api
DISPLAY=:1 ./venv/bin/browser-llm login gemini   # or: chatgpt — visible Chrome opens; sign in; auto-closes
systemctl --user start browser-llm-api
```

**Why you must use the login helper, not a normal Chrome:** `nodriver` launches Chrome with
`--password-store=basic`, while a normal Chrome uses the system keyring. Cookies written by one
**cannot be decrypted by the other**. The service's Chrome is also invisible (Xvfb), so you can't
sign in there — the helper opens a real, visible window in the *same* cookie store.

## Access from other machines (API key)

- **Auth model:** localhost is always unauthenticated. With `BROWSER_LLM_API_KEY` set, non-loopback
  clients need the key on `/v1/*` + `/api/*`; pages/assets (`/`, `/ui`, `/widget.js`, `/demo`,
  `/version`, `/images/*`) stay public — image links must work in a bare `<img>`/browser, and the
  filenames are unguessable (uuid hex). CORS preflights (OPTIONS) are exempt — they can't carry auth
  headers; the real request is still checked. Decision helpers live in `browser_llm/authz.py`
  (unit-tested, `tests/test_authz.py`); the middleware in `server.py` is registered **after**
  CORSMiddleware so it runs before it.
- **Another machine calls the API directly** with the key (`BROWSER_LLM_API` + `BROWSER_LLM_API_KEY`
  on the client). Forwarding through a second install (`REMOTE_PROVIDERS`) was removed in 2.0.
- **A leaked key** (an unknown caller in the journal): change `BROWSER_LLM_API_KEY`, restart once
  nothing is in flight, and block the address at the firewall. Saved images carry their
  conversation in the filename, so a caller's images can be traced and deleted.
- **Web UI key:** open `http://<host>:8081/#key=<key>` once — stored in localStorage, attached to
  all API fetches via a fetch wrapper. Widget: `data-key="<key>"` attr. CLI and `client.ask`:
  `BROWSER_LLM_API_KEY` env.
- Use `./venv/bin/python -m pip …`, not `./venv/bin/pip …`: a venv that predates a folder rename
  has a stale shebang in its pip script.

## Gotchas & conventions

Module names below (`server.py`, `providers/chatgpt.py`, …) are inside `browser_llm/`; shell
scripts are in `scripts/` (the top-level copies forward to them).

- **Loopback is NOT the same as trusted (fixed 2026-08-11).** CORS is `allow_origins=["*"]` on
  purpose — the widget is embedded on other pages — so any website open in the operator's browser can
  make it POST here, and that request arrives from `127.0.0.1` and passes every loopback check.
  Reproduced live before the fix: `Origin: https://evil.example` plus a local file path got the server
  to read that file, upload it to ChatGPT and return the model's description of it, i.e. a file-read
  primitive for any path a page can guess (no image check — `_resolve_local_attachment` only tests
  existence and size, and `DOM.setFileInputFiles` ignores the input's `accept`). Fix:
  `authz.origin_is_trusted(origin, host)` — trust no `Origin` (curl/CLI/desktop app; a browser cannot
  omit it cross-origin) or one matching the addressed host, and gate `_client_may_send_paths` on it.
  All three attachment endpoints share that helper. Unit-tested in `tests/test_authz.py` +
  `tests/test_attachments.py`; verified live in three states (drive-by 400, same-origin UI passes,
  keyed LAN client still refused). **Don't "simplify" the path policy back to a loopback check.**
- **`Origin: null` is NOT a missing Origin (fixed 2026-08-20).** `origin_is_trusted` treated the
  literal string `null` as "no browser set this" and returned True, which handed every gated
  endpoint back to any page: a site can give itself an opaque origin with
  `<iframe sandbox="allow-scripts" srcdoc>`, the frame's `fetch` then carries `Origin: null`, and
  with CORS at `*` the frame reads the reply and `postMessage`s it to its parent. Reproduced live
  from a page on `http://localhost:3000` before the fix: its own direct call to `/api/history` got
  403, the sandboxed one got **200 and the account's chat titles**; the same trick reached the
  file-path attachment primitive. Now `null` falls through to the host comparison and fails it
  (`urlsplit("null").netloc == ""`). Verified after the fix in the same browser: 403 on the history
  read, 400 on the file-path attachment. Cost: a page opened from `file://` can't use the gated
  endpoints, which is the right trade. Tests: `test_opaque_origin_is_not_trusted` in
  `tests/test_authz.py`.
- **One tab per profile, always — a history call must not open a second one.** `session_tab()` in
  `base.py` reuses a tab already on the site's origin and otherwise navigates the FIRST target
  (`browser.get(url)` without `new_tab`), which is the tab a drive uses. A second live chatgpt.com
  tab makes the *next* drive read 0 chars for the full deadline: ChatGPT multiplexes one WebSocket
  per profile and the second conversation never renders an assistant turn. Opening a tab and
  closing it afterwards is NOT a fix — that was tried, and the drive still hung. This is the same
  wall as the tab-pool attempt below, reached from a different direction.
- **The per-provider lock is taken INSIDE the SSE generator** in `chat_completions`
  (`server.py`). FastAPI runs the generator *after* the handler returns, so an `async with`
  around `return StreamingResponse(...)` releases the lock before the first poll and lets
  concurrent requests fight over one browser tab (that bug existed and was fixed 2026-07-08).
  Don't move it back out. Streaming failures are surfaced in-band as a
  `[browser-llm error: …]` chunk — raising would just cut the SSE dead.
- **CompletionTracker, authz, attachments and the history layer have unit tests** —
  `./venv/bin/python -m unittest discover -s tests` (245 tests, no browser). If you change the
  done-decision logic in `providers/base.py`, the attachment layer in `server.py`, or anything that
  decides who may read or delete an account's history, run/extend them.
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
- **A deleted venv used to be invisible for days, then a silent crash loop (fixed 2026-09-07).**
  `serve.sh` fell back to the system python3 when `venv/bin/python` was missing. System python is
  PEP-668 and can never hold nodriver, so the fallback could only ever produce
  `ModuleNotFoundError: No module named 'nodriver'` — 241 restarts at `RestartSec=5`, nothing on
  8081, and no alarm anywhere. The nastier half: the venv had been gone for **ten days** and nobody
  could tell, because the running process already held its imports and kept serving normally. Only
  the next restart exposed it. What deleted it was never established — no script on the box, no cron
  entry and no shell history matches, so the fix does not depend on knowing. Three layers, each
  fault-injected live: `ensure-venv.sh` rebuilds instead of falling back (venv deleted + service
  restarted → self-repaired in 7s, 0 restarts); `healthcheck.sh` probes **integrity as well as
  liveness** every 10 minutes, which is the only check that would have caught the latent ten-day
  window; and anything it cannot repair itself raises a `critical` alert through the configured
  sink rather than retrying forever in silence. Don't reinstate the `else PY=python3` fallback —
  it converts a two-minute self-repair into an outage.
- **The watchdog's daily deep check spends one real turn per provider.** That is deliberate: a
  provider session expiring is this project's #1 failure mode and the server stays perfectly healthy
  while returning nothing, so no free probe can see it. Keep it to one short ephemeral completion:
  the turn is spent on a real account, which may be shared with other clients.
- **Non-headless is mandatory** — the sites block true headless Chrome. Background = Xvfb virtual
  display, never `--headless`.
- **ChatGPT image generation REQUIRES a GPU / real display — it does NOT work under headless Xvfb.**
  GPT-image renders progressively on a `<canvas>`, which stalls indefinitely under Xvfb's software
  rendering (even with SwiftShader GL flags, which are set in `CHROME_ARGS` and help other cases).
  So: **ChatGPT text and Gemini run fine under the Xvfb systemd service, but ChatGPT *image* requests
  must run with the server on a real display** (e.g. `DISPLAY=:1 ./serve.sh`).
  Verified working on `:1` (produced a real 1536×1024 PNG in ~40s). Image gen on the free "Go" tier
  is also slow/variable (30s–4min+), hence the 420s completion deadline.
- **ChatGPT specifics (verified 2026-10-12):** every selector is a LIST holding the layout before
  11 Oct 2026 and the one after, defined once at the top of `providers/chatgpt.py`. Composer:
  `#prompt-textarea`, now a `div.ProseMirror[contenteditable]` inside `form[data-chatgpt-composer]`
  with no id. Typing: CDP char events still type letters into it but a "\n" types nothing, so
  `type_prompt` puts the whole prompt in with one `Input.insertText` (each line break becomes a
  paragraph, as a paste would). Send: the form's
  `button[type=submit]`, labelled "Send" (was `button[data-testid="send-button"]`); it only appears
  after typing, the composer shows a Voice button at rest. Answer: the last assistant turn
  (`[data-chatgpt-search-unit-key$=":assistant"]`, was `[data-message-author-role="assistant"]`)
  and its `[data-markdown-text-style="assistant-message"]` root (was `.markdown`). Generating: the
  form's `button[aria-label="Stop"]` and `[data-talvt-turn-state="in_progress"]` (was
  `[data-testid="stop-button"]`); both cleared at the end of every measured answer, and a signal
  that sticks holds every request to the deadline, so only add one after watching it clear.
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
  the assistant turn element, so `image_status`/`get_images` scan the whole page.
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
- **Service unit is generated, not committed** — `scripts/install-service.sh` fills
  `deploy/systemd/browser-llm-api.service.template` (`__INSTALL_DIR__` → the clone path) into
  `~/.config/systemd/user/browser-llm-api.service`; its `ExecStart` runs `scripts/serve.sh` (venv
  python + display auto-detect). A unit installed before 2.0 runs the top-level `serve.sh`, which
  forwards, so it keeps working without a reinstall. No paths are hardcoded in the repo. The image
  folder defaults to `~/Pictures/browser-llm` (`BROWSER_LLM_IMAGE_DIR`); if it isn't writable,
  image saving silently disables.
- **A hidden page answers nothing, on both providers (fixed 2026-09-26).** On 2026-09-18 at 01:58 a
  sign-in session mirrored the service's Xvfb display to the desktop, ran `xdotool windowmove
  <id> 2400 1400` on both service windows (off a 1920x1080 screen) and maximised another
  Chrome over the rest. Chrome on X11 treats an off-screen or fully covered window as occluded and
  every page in it reads `document.visibilityState == "hidden"`; a hidden page gets no animation
  frames, so both sites took the typed prompt out of the composer and never rendered a turn (no
  `/c/<id>` URL, no user message, no streaming request). Each drive then waited 420s and returned an
  empty 200 with `errors: 0`, and the watchdog's alert text blamed an expired login, which is
  what everyone believed for eight days while `/api/auth/session` showed a live session. Proved both
  ways before the fix: on the unchanged service, one uncovered pixel made the page `visible` and
  each provider answered "OK" in 17-18s; parked again, Gemini returned `''` after 433s. Three
  layers, each checked live: `--disable-backgrounding-occluded-windows` in `CHROME_ARGS` keeps an
  occluded window visible (off-screen, covered and even unmapped windows read `visible` with it;
  Gemini answered with its window parked at `+2400+1400` again); `ensure_visible()` restores and
  raises a window that is still hidden (`Browser.setWindowBounds` + `Page.bringToFront` — moving
  alone is not enough when another window covers the origin) and otherwise raises an error naming
  the cause; and `nostart` fails a drive that shows nothing for 60s (measured: 62s, was 420s). The
  watchdog now logs the server's own `detail` instead of "returned nothing". Chrome also persisted
  the parked position in `chatgpt_profile/Default/Preferences` and reopened at `+1890+1050` with
  a corner on-screen, so a restart alone would not have been a dependable fix. **Don't move or
  cover the service windows on its display, and don't remove the flag.**
- **ChatGPT's "Create image" mode is a pill INSIDE the editor (measured live 14 Oct 2026, Plus,
  nothing sent).** The composer's + button (`aria-label="Add files and more"`) opens a menu whose
  "Create image" item inserts `span[data-inline-selection-pill][data-system-hint-type="picture_v2"]`
  (contenteditable=false, text "Create image") at the caret. Three traps, each reproduced: selecting
  the editor's contents and inserting text, which is exactly type_prompt's draft guard, deletes the
  pill, and so does select-all + Backspace; choosing the tool after typing drops the pill wherever
  the caret is (it landed mid-word, "line tw<pill>o"); and the pill is part of the draft, so it
  survives a reload and would turn the next text request into a picture. Hence the order in
  `prepare_page`: empty the composer, choose the tool, attach files (an attachment leaves the pill
  alone), then `type_prompt` inserts at the END without select-all; a text request clears any
  pill first. `composer_text` leaves pills out, or `_send_landed` would read a leftover pill as an
  unsent prompt. A failure to find the menu item logs and sends in words (`image_tool: false`).
- **Several attachments went to a single-file camera input first (fixed 14 Oct 2026).** File
  inputs were ranked by `accept` alone, so ChatGPT's four tied and document order won: "Take a
  photo" (accept image/*, `capture`, no `multiple`) came first. Given a design set's two
  references it took one, the chip count stalled at 1 of 2, and `_attach_via_input` handed BOTH
  files to the next input: ChatGPT received three images (seen live in the user turn). Now a
  `capture` input ranks last, an input without `multiple` ranks below every one with it when
  more than one file goes, and an input that takes some of the files gets the rest one at a
  time instead of the next input getting all of them. Verified live: "attached 2 file(s) via
  file input #0", two images in the sent turn.
- **Open: empty answers at GPT-6 High (14 Oct 2026, not reproduced).** Two 3,568-character
  prompts from an evaluation script came back "Done (empty). 0 chars" after 3-5 minutes while
  the conversation record said gpt-6-thinking (extended) answered; the same prompt at Medium
  returned 10,915 chars, and a 17,336-char tokens answer at High read fine. `get_response_text`
  used to turn a script error into "" without a word; it now logs "answer read failed" with the
  cause (at most every 30 s). Read that line before guessing next time, and keep the page
  (`ephemeral: false`) to inspect its DOM.
- **A restart serves whatever is on disk, and it cuts whatever is in flight (14 Oct 2026).**
  The service runs this working tree. A restart at 15:12:41, to load a changed setting, landed
  while providers/chatgpt.py was between edits. That time the file held only new constants
  and chat kept working; a minute later the same restart would have loaded a `prepare_page` that
  called methods not yet written, and every chat or image drive would have raised AttributeError
  while history reads, which skip `prepare_page`, carried on and hid it. Make a provider change in
  one write (or in a worktree, as 2.0 was) when someone else may restart the service. The other
  half: a restart at 15:47 went ahead while `/api/status` showed ChatGPT `busy: true`, and cut an evaluation
  request 35 s into its answer; the client still got a 200 with a partial reply. Before any
  restart, read `/api/status` and do not go ahead while any provider is busy.
- **ChatGPT redesigned its page on 11 Oct 2026 and every request failed for 36 hours (fixed
  2026-10-12).** The new page dropped `#prompt-textarea`, `data-message-author-role` and nearly every
  `data-testid` at once, so the composer, the send and stop buttons, the answer turn, the upload input
  and the attachment chip all stopped matching. Requests failed at `input box not found` from 11 Oct
  00:02, and the watchdog's alert, titled as empty answers with an expired login first in its list of
  causes, did not name a redesign. Two silent traps came with it: the sent image now sits in the user
  turn outside the bubble, so the old `_isInput()` would have counted it as a generated image, and the
  chip is now `Remove <file name>`, so the old count would have attached the image four times. Code
  cards became one `[data-markdown-copy="code-block"]` element with a button inside the editor pane,
  which left "Python\nRun" in front of every fence, and line breaks typed as key events vanished from
  the prompt (whether the old composer did the same was never measured). The fix
  keeps both layouts in one list per selector (top of `providers/chatgpt.py`), and
  `Provider.composer_hint()` now says whether the page still holds an editable element (a redesign)
  or none (a sign-in wall). When this happens again, read the live page over CDP first (the port is in
  `pgrep -af 'user-data-dir=./chatgpt_profile'`), measure every selector in the provider on a real
  turn, a code answer and an upload, and add the new layout to each list rather than replacing it.
- **Two tabs in one Chrome cannot drive two ChatGPT conversations (tried and reverted
  2026-08-18).** A per-provider tab pool replacing the `asyncio.Lock` *looks* like the obvious way
  to run requests in parallel, and mechanically it works: two tabs navigate and submit 0.2s apart,
  confirmed in the logs across three runs. But only ONE answer ever completes — the other reads
  **0 chars for the full 421s deadline** and times out. Chrome's background-tab throttling was the
  obvious suspect and is NOT the cause: `--disable-background-timer-throttling`,
  `--disable-backgrounding-occluded-windows` and `--disable-renderer-backgrounding` changed
  nothing. The remaining explanation is that ChatGPT's SPA does not tolerate two live
  conversations in one profile (it multiplexes one WebSocket, `ws.chatgpt.com`), so the second
  tab never renders an assistant turn for `get_response_text` to read. **Real concurrency needs
  one browser per slot — a separate profile per tab — not a tab pool.** That is a much bigger
  change: profile cloning, a login per profile, and roughly a Chrome's worth of RAM each. Don't
  re-attempt the tab-pool version; the per-provider lock stays.
- **Gemini image generation failed for the account tested (2026-08-18).** Gemini *text* works, but any
  image request comes back in ~15s with no image and `/v1/images/generations` answers 502
  `"gemini-browser did not return an image"`. Asked in words, Gemini says: *"I can search for
  images, but can't create any for you at the moment. You might be signed out or image creation
  may not be available in your location yet."* Account/region, not a code bug — reproduced with
  raw curl against the endpoint. Since 13 Oct 2026 the default is `chatgpt-browser`; an install
  can refuse Gemini images outright with `IMAGE_PROVIDERS=chatgpt-browser` (403).
- **ChatGPT image gen DID work under an Xvfb display on 2026-08-18** — a 512×512 knockout
  PNG in ~90s via the service as installed. That contradicts the "requires a real display" note
  below, which was written before the SwiftShader flags landed in `CHROME_ARGS`. One dated
  observation, not a refutation: if canvas gen stalls again, a real display is still the fix.
- **ChatGPT does NOT honour a `?model=` URL parameter** (probed live 2026-08-18 against the
  logged-in profile: `chatgpt.com/?model=gpt-5-thinking` and `?model=gpt-5-instant` both redirect
  to bare `chatgpt.com/`). Don't add a `?model=` shortcut believing it works; model selection
  clicks the picker (next note).
- **The ChatGPT Plus model picker (measured 13 Oct 2026).** `button[aria-label="Select
  ChatGPT model"]` shows the power label ("High"; GPT-5.6 Sol shows "5.6 Instant", so
  `effort_matches` looks for the word). Its menu has `[data-model-picker-power-slider]` with three
  `span[data-selected]` dots (Instant, Medium, High) and `[role=menuitemradio]` model rows with
  `aria-checked` (GPT-6, GPT-5.6 Sol, GPT-5.5 "leaving 14 Oct"); the rows are in the DOM but inert
  until `[data-model-picker-view-toggle]` is clicked. Clicks must be real CDP mouse events
  (`_click_xy`); Escape closes the menu. The choice persists across reloads, so `select_variant`
  caches the model and only opens the menu when something differs. The non-secret cookies
  `oai-last-model-config` / `oai-default-model-config` do NOT track the picker; don't read them.
- **What answered: the conversation record, not the picker.** `GET /backend-api/conversation/<id>`
  (Bearer from `/api/auth/session`) gives each assistant message `metadata.model_slug`,
  `resolved_model_slug` and `thinking_effort`. GPT-6 at High answers text as `gpt-6-thinking` /
  `extended`; image turns are routed by the site to `gpt-5-4-thinking` (or `gpt-5-4-auto-thinking`)
  whatever the picker says, Instant images to `gpt-6`; one Instant image's record named NO model
  (its assistant message carried only `model_switcher_deny`), the next one did. The page's own
  request body (`model`, `thinking_effort` on `/backend-api/f/conversation`) is the fallback,
  and Chrome only puts bodies in `requestWillBeSent` when `Network.enable` names
  `maxPostDataSize` — nodriver doesn't, so `StreamMonitor.enable_bodies` does (until 76f5e94
  `requested_by_page` was never filled). The answer's "Try again" menu shows the page's own label
  ("Try again • 6 Thinking").
- **A send that doesn't land leaves a draft (13 Oct 2026).** The send click missed once; the prompt
  stayed in the composer and the site persisted it as a draft, so the retry's `insertText` appended
  the prompt to itself and ChatGPT got it twice. `type_prompt` now selects the whole composer
  first (insertText replaces the selection) and checks the text; `open_and_send` confirms a send
  (our turn shown or composer empty) and retries one click, then Enter.
- **ChatGPT's image sizes (13 Oct 2026, Plus).** The tool picks a shape near the asked ratio:
  1254x1254 square, 1672x941 for 16:9, 1731x909 for 1.91:1, 1122x1402 for 4:5. It honours
  "transparent background" with a real RGBA PNG. Exact pixels come from gen_asset's crop.
- **`usage` token counts are fake** — plain `.split()` word counts, not a real tokenizer.
- **Stale lock after a crash**: a hard crash leaves `<profile>/SingletonLock`; `serve.sh` clears both
  profiles' locks on every start, so the service and a foreground run are both covered. Running
  `browser-llm serve` directly, without `serve.sh`? delete `*_profile/Singleton*` yourself.
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
