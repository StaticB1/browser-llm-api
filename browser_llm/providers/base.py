"""
Provider abstraction for the browser-automation server.

Each provider drives a different chat web UI (Gemini, ChatGPT) but shares the
same lifecycle: open a chat, optionally attach input images, type a prompt,
submit, then poll the DOM for the streamed answer (and any generated images).
The generic completion loop lives in ``server.py``; providers supply the
site-specific URL, selectors, and extractors.

A provider is mostly declarative — set the class attributes (``chat_url``,
``profile_dir``, ``input_selector``, ``send_selectors``, ``stream_url_fragments``,
``supports_upload`` + its upload selectors) and implement the site-specific reads
(``get_response_text``, ``is_generating``, ``logged_in``, and optionally
``image_status`` / ``get_images``).
"""
import asyncio
import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager

logger = logging.getLogger("gemini_server")

# Chrome flags, passed to every uc.start() (server + login).
#
# The first four enable software (SwiftShader) GL so canvas/WebGL renders under a
# headless Xvfb display with no GPU. ChatGPT's GPT-image generation draws on a
# <canvas>; without a GL backend it stalls forever on the "rendering" tile.
# Harmless on a real GPU display.
#
# --disable-backgrounding-occluded-windows: on X11, Chrome marks a window that is
# off-screen or fully covered by another window as occluded, and every page in it
# goes to document.visibilityState "hidden". A hidden page gets no animation
# frames, so both sites' single-page apps take the prompt out of the composer
# and never render a turn: each drive then waited out the 420s deadline and
# answered an empty 200. That broke both providers from 2026-09-18 to 09-26,
# after a session mirroring the shared display parked these windows at
# +2400+1400 on a 1920x1080 screen and maximised another Chrome over the rest.
# With this flag an occluded window stays "visible". Measured on :98: off-screen
# and covered windows read "hidden" without it and "visible" with it.
CHROME_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
    "--disable-backgrounding-occluded-windows",
]


class RateLimited(RuntimeError):
    """The site refused a read because we asked too often. Distinct from a
    failure: the same call works again after a pause, so callers surface it as
    "try again shortly" rather than "broken"."""


def patch_cdp() -> None:
    """Suppress ``KeyError`` from unknown CDP events (e.g.
    ``DOM.adoptedStyleSheetsModified``) that ``nodriver``'s cdp parser raises.
    Call once at import time in every entry point."""
    import nodriver.cdp.util as _cdp_util
    _orig = _cdp_util.parse_json_event

    def _safe(json_data: dict):
        try:
            return _orig(json_data)
        except KeyError:
            return None

    _cdp_util.parse_json_event = _safe


# ---------------------------------------------------------------------------
# File-upload (image input) helpers — shared by every provider.
#
# Sending an image *into* a chat means driving the site's file picker. Two
# mechanisms cover every site we've seen, tried in this order:
#
#   1. An <input type="file"> that already exists in the DOM (ChatGPT keeps
#      hidden ones there at rest, labelled only by aria-label since its page of
#      11 Oct 2026). We hand its
#      objectId to CDP's DOM.setFileInputFiles, which fires real input/change
#      events — the page can't tell it apart from a human picking a file.
#   2. No such input (Gemini creates one on demand behind its "Upload & tools"
#      menu): turn on CDP file-chooser interception so clicking the menu can't
#      pop a native OS dialog (which would wedge the tab forever), click through
#      the provider's `attach_click_path`, and feed the files to the backend node
#      reported by the resulting Page.fileChooserOpened event.
#
# The element lookups pierce shadow roots, because Gemini's composer is a tree
# of Web Components.
# ---------------------------------------------------------------------------

# Nth <input type="file"> in the page (shadow-piercing), best candidate first:
# an image-only input beats an accept-anything input, which beats one that only
# takes e.g. PDFs. A camera input (`capture`) goes last, and when more than one
# file is being sent an input without `multiple` ranks below every one with it:
# on 14 Oct 2026 ChatGPT's first input was "Take a photo" (accept image/*,
# capture, single file), it took one of two references, and the second input
# then got both, so ChatGPT received three images. Returns the element itself
# (we need its objectId, not a copy).
_NTH_FILE_INPUT_JS = """
((n, many) => {
  const found = [];
  const walk = (root) => {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('input[type=file]').forEach(e => found.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  const score = (i) => {
    const a = (i.getAttribute('accept') || '').toLowerCase();
    let s = a.indexOf('image') >= 0 ? 2 : (!a ? 1 : 0);
    if (i.hasAttribute('capture')) s -= 3;
    if (many && !i.multiple) s -= 10;
    return s;
  };
  // A stable sort: equal scores keep document order.
  const ranked = found.map((e, k) => [e, k])
    .sort((x, y) => (score(y[0]) - score(x[0])) || (x[1] - y[1]));
  return ranked[n] ? ranked[n][0] : null;
})(%d, %s)
"""

_COUNT_FILE_INPUTS_JS = """
(() => {
  let n = 0;
  const walk = (root) => {
    if (!root || !root.querySelectorAll) return;
    n += root.querySelectorAll('input[type=file]').length;
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(document);
  return String(n);
})()
"""

# Click the first match among a list of alternatives. Each alternative is either
# a CSS selector or "text:<needle>", which matches a visible control whose
# aria-label/text contains the needle (case-insensitive). Shadow-piercing.
_CLICK_ALTS_JS = """
((alts) => {
  const vis = e => !!(e.offsetParent || (e.getClientRects && e.getClientRects().length));
  const hits = (root, sel, out) => {
    if (!root || !root.querySelectorAll) return;
    try { root.querySelectorAll(sel).forEach(e => out.push(e)); } catch (err) {}
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) hits(e.shadowRoot, sel, out); });
  };
  for (const alt of alts) {
    const out = [];
    if (alt.indexOf('text:') === 0) {
      const needle = alt.slice(5).toLowerCase();
      hits(document, 'button,[role=menuitem],[role=option],[role=button],li,a,span,div', out);
      const m = out.filter(e => vis(e) &&
        (((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')).toLowerCase().indexOf(needle) >= 0));
      // Prefer the innermost match so we don't click a wrapper that swallows it.
      const el = m.length ? m[m.length - 1] : null;
      if (el) { el.click(); return alt; }
    } else {
      hits(document, alt, out);
      const el = out.filter(vis)[0] || out[0];
      if (el) { el.click(); return alt; }
    }
  }
  return null;
})(%s)
"""


async def eval_handle(page, expression: str):
    """``Runtime.evaluate`` returning the raw RemoteObject (no deep
    serialization), so a DOM element's ``objectId`` can be handed to CDP DOM
    commands. ``page.evaluate`` deep-serializes instead, which loses the handle."""
    from nodriver import cdp
    remote, errors = await page.send(cdp.runtime.evaluate(
        expression=expression,
        return_by_value=False,
        await_promise=False,
        user_gesture=True,
        allow_unsafe_eval_blocked_by_csp=True,
    ))
    if errors:
        raise RuntimeError(f"JS error: {errors}")
    return remote


class StreamMonitor:
    """
    Watches CDP Network events for a provider's LLM streaming request and sets
    ``stream_done`` when it finishes. ``url_fragments`` are substrings that
    identify that request (provider-specific, e.g. ``BardFrontendService`` for
    Gemini or ``backend-api/conversation`` for ChatGPT).

    ``ws_fragments`` (optional) identify a *WebSocket* the provider streams over
    (ChatGPT uses ``ws.chatgpt.com``). We can't reliably read the completion
    marker out of multiplexed WS frames, so we don't use them as a done-signal;
    instead we record the time of the last frame as a coarse "still streaming"
    heartbeat, so the polling loop won't truncate a long answer that is plainly
    still in flight.
    """

    def __init__(self, url_fragments, ws_fragments=None):
        self._fragments = list(url_fragments)
        self._ws_fragments = list(ws_fragments or [])
        self._tracked: set[str] = set()
        self._ws_tracked: set[str] = set()
        self.stream_done = asyncio.Event()
        self.ws_seen = False
        self._last_ws_frame: float | None = None
        # What the PAGE asked its backend for, read from the tracked request's
        # JSON body (ChatGPT: {"model": "gpt-6-thinking", "thinking_effort":
        # "extended"}). The page's choice, not ours; the site may still route
        # the turn to another model, which only the conversation record shows.
        self.sent_model: dict = {}

    def attach(self, tab):
        try:
            from nodriver import cdp
            tab.add_handler(cdp.network.RequestWillBeSent, self._on_request)
            tab.add_handler(cdp.network.LoadingFinished, self._on_finished)
            tab.add_handler(cdp.network.LoadingFailed, self._on_finished_err)
            if self._ws_fragments:
                tab.add_handler(cdp.network.WebSocketCreated, self._on_ws_created)
                tab.add_handler(cdp.network.WebSocketFrameReceived, self._on_ws_frame)
        except Exception as e:
            logger.warning(f"CDP network monitor unavailable: {e}")

    # Chrome leaves postData out of requestWillBeSent unless Network.enable
    # names a size, and nodriver enables the domain without one, so until
    # 13 Oct 2026 sent_model stayed empty on every request. 256 KB holds a chat
    # request with a long prompt; larger bodies (file uploads) are left out.
    MAX_POST_DATA = 262144

    async def enable_bodies(self, tab) -> None:
        """Ask Chrome to include request bodies (up to MAX_POST_DATA) in the
        events attach() listens to. Call after attach(). Never raises."""
        try:
            from nodriver import cdp
            await tab.send(cdp.network.enable(max_post_data_size=self.MAX_POST_DATA))
        except Exception as e:
            logger.debug(f"request bodies unavailable to the monitor: {e}")

    def _on_request(self, event):
        url = event.request.url
        if self._fragments and any(k in url for k in self._fragments):
            self._tracked.add(event.request_id)
            self._note_model(getattr(event.request, "post_data", None))

    def _note_model(self, body) -> None:
        if not body or not isinstance(body, str) or '"model"' not in body:
            return
        try:
            data = json.loads(body)
        except Exception:
            return
        if isinstance(data, dict) and isinstance(data.get("model"), str):
            self.sent_model = {k: data[k] for k in ("model", "thinking_effort")
                               if isinstance(data.get(k), str)}

    def _on_finished(self, event):
        if event.request_id in self._tracked:
            self.stream_done.set()

    def _on_finished_err(self, event):
        if event.request_id in self._tracked:
            self.stream_done.set()

    def _on_ws_created(self, event):
        try:
            if any(k in (event.url or "") for k in self._ws_fragments):
                self._ws_tracked.add(event.request_id)
        except Exception:
            pass

    def _on_ws_frame(self, event):
        try:
            if event.request_id in self._ws_tracked:
                self.ws_seen = True
                self._last_ws_frame = time.monotonic()
        except Exception:
            pass

    def seconds_since_ws_frame(self, now: float | None = None) -> float | None:
        """Seconds since the last tracked WebSocket frame, or None if none seen."""
        if self._last_ws_frame is None:
            return None
        return (now if now is not None else time.monotonic()) - self._last_ws_frame


class CompletionTracker:
    """
    Provider-agnostic decision logic for *when a streamed answer is complete* —
    text and/or generated image. Extracted from the polling loop so it can be
    unit-tested with synthetic event sequences (no browser required).

    Feed it one ``(now, raw_text, is_generating, img_status)`` sample per poll;
    it returns ``(chunk, done_reason)`` where ``chunk`` is any new text to emit
    and ``done_reason`` is a short string (``"text"`` / ``"image"`` / ``"empty"``)
    once the answer has settled, else ``None``.

    ``"nostart"`` is the failure outcome: for ``NOT_STARTED_TIMEOUT`` seconds
    after sending, the page showed nothing at all (no user turn, no text, no
    generation, no image). The caller turns it into an error. Before it existed,
    that case rode the full 420s deadline and came back as an empty 200 with
    ``errors: 0``, which is how a hidden page (2026-09-18) looked like an expired
    login for eight days.
    """

    # tuning (seconds)
    SILENT_TEXT_DONE = 2.5          # not generating + text unchanged this long -> done
    SILENT_EMPTY_DONE = 10.0        # generated but nothing extractable -> give up
    SILENT_PLACEHOLDER_DONE = 45.0  # ...but be patient while a status placeholder shows
    IMAGE_STABLE = 4.0              # rendered-image count stable this long -> done
    WS_ACTIVE_WINDOW = 2.0          # a WS frame within this window == still streaming
    FALSE_CREATING_TIMEOUT = 45.0   # "creating" stuck with no image after gen ended -> ignore it
    # Nothing on the page this long after sending -> "nostart". A working site
    # shows the user turn within a few seconds, so this only needs to outlast a
    # slow page, not a slow model: any sign of life (user turn included) disarms it.
    NOT_STARTED_TIMEOUT = 60.0

    # Transient status lines a site shows *instead of* the answer: "Creating your
    # image…" (image gen) or "Analyzing image" (vision request). These must never
    # be surfaced as the reply — a short one settling for SILENT_TEXT_DONE used to
    # complete the request with the placeholder AS the whole answer. Only SHORT
    # text can be a placeholder, so a real answer that happens to open with
    # "Analyzing the …" is suppressed for a poll or two, then flows as it grows.
    _PLACEHOLDER_RE = re.compile(
        r'^(creating|generating|analy[sz]ing|analy[sz]ed|reading|thinking|working)\b', re.I)
    PLACEHOLDER_MAX_LEN = 48

    def __init__(self):
        self.text_len = 0
        self.text = ""          # current full (suppression-filtered) response text
        self.cdp_fired = False
        self._last_change: float | None = None
        self._saw_generation = False
        self._saw_creating = False
        self._last_loaded = 0
        self._loaded_since: float | None = None
        self._creating_since: float | None = None
        self._saw_placeholder = False
        self._first_sample: float | None = None
        self.started = False    # any sign the site took the prompt (see feed)

    def _is_placeholder(self, text: str) -> bool:
        """True for transient status text shown instead of the answer —
        'Creating your image…', 'Analyzing image', …"""
        t = (text or "").strip()
        return (bool(t) and len(t) <= self.PLACEHOLDER_MAX_LEN
                and bool(self._PLACEHOLDER_RE.match(t)))

    def silent_for(self, now: float) -> float:
        return 0.0 if self._last_change is None else now - self._last_change

    def feed(self, now, raw_text, is_generating, img, *, cdp_done=False, submitted=None):
        """``submitted`` is whether the page shows our user turn: True, False,
        or None when the provider cannot tell. Only used to decide ``started``."""
        if self._last_change is None:
            self._last_change = now
        if self._first_sample is None:
            self._first_sample = now

        loaded = img.get("loaded", 0)
        creating = bool(img.get("creating"))
        pending = img.get("pending", 0) > 0

        # Any sign of life at all, placeholders included. Deliberately broad:
        # "nostart" must never cut off a real answer, only a page that did nothing.
        if (submitted or is_generating or creating or pending or loaded
                or (raw_text or "").strip()):
            self.started = True

        # Track how long "creating" has been asserted with nothing rendered.
        if creating and loaded == 0:
            if self._creating_since is None:
                self._creating_since = now
        else:
            self._creating_since = None

        img_pending = creating or pending
        # False-positive guard: if "creating" has been stuck on with no image
        # rendered AND generation has already ended, it isn't a real image (e.g.
        # a code/canvas editor's <canvas>). Stop suppressing text and let the
        # normal text/empty completion fire instead of hanging to the deadline.
        if (img_pending and not is_generating and loaded == 0
                and self._creating_since is not None
                and (now - self._creating_since) >= self.FALSE_CREATING_TIMEOUT):
            img_pending = False
            self._saw_creating = False  # so a genuinely empty answer can complete

        # Never surface a status placeholder, or the thinking text shown while an
        # image is (really) rendering.
        placeholder = self._is_placeholder(raw_text)
        if placeholder:
            self._saw_placeholder = True
        text = "" if (placeholder or img_pending) else raw_text
        if text:
            self.text = text  # keep last non-empty; done can fire after a transient ""

        chunk = ""
        if len(text) > self.text_len:
            chunk = text[self.text_len:]
            self.text_len = len(text)
            self._last_change = now

        if cdp_done:
            self.cdp_fired = True

        if loaded != self._last_loaded:
            self._last_loaded = loaded
            self._loaded_since = now
        if creating or loaded > 0:
            self._saw_creating = True

        # Image completion: an image rendered, it's no longer "creating", and the
        # set has been stable a few seconds (fires even if a stop button lingers).
        if (loaded > 0 and not creating and self._loaded_since is not None
                and (now - self._loaded_since) >= self.IMAGE_STABLE):
            return chunk, "image"

        if is_generating:
            self._saw_generation = True
            return chunk, None

        # Generation stopped. If an image is still rendering, keep waiting.
        if img_pending:
            return chunk, None

        silent = now - self._last_change
        # Text completion: settled + not generating. Don't require a CDP signal
        # or a length floor — short replies and WebSocket-streamed providers
        # (ChatGPT) never fire the HTTP stream signal.
        if self.text_len > 0 and silent >= self.SILENT_TEXT_DONE:
            return chunk, "text"
        # Guard: generation happened but produced no extractable text — return
        # rather than hang. Suppressed once an image was in flight. A visible
        # status placeholder means the site IS still working (e.g. ChatGPT
        # analyzing an uploaded image with no stop button showing), so wait
        # longer before declaring the answer empty.
        empty_after = (self.SILENT_PLACEHOLDER_DONE if self._saw_placeholder
                       else self.SILENT_EMPTY_DONE)
        if (self.text_len == 0 and self._saw_generation and not self._saw_creating
                and silent >= empty_after):
            return chunk, "empty"
        if (not self.started
                and now - self._first_sample >= self.NOT_STARTED_TIMEOUT):
            return chunk, "nostart"
        return chunk, None


_NAMED_RATIOS = [(1, 1), (16, 9), (9, 16), (3, 2), (2, 3), (4, 3), (3, 4), (4, 5),
                 (5, 4), (21, 9), (9, 21), (191, 100), (2, 1), (1, 2), (3, 1), (1, 3)]


def parse_size(size) -> tuple[int, int] | None:
    """'1600x900' (or '1600*900', '1600 x 900') -> (1600, 900); else None."""
    m = re.match(r"^\s*(\d{2,5})\s*[x×*]\s*(\d{2,5})\s*$", str(size or ""), re.I)
    return (int(m.group(1)), int(m.group(2))) if m else None


def size_instruction(size) -> str:
    """The size of an image request in words, or "" for none/auto.

    ``size`` is ``WxH`` or one of square/landscape/portrait. The image tools
    on these sites make a few fixed sizes (ChatGPT: 1254x1254 square on
    13 Oct 2026), so the words ask for the orientation and the nearest aspect
    ratio and keep the subject clear of the edges; the caller crops to the
    exact pixels afterwards (gen_asset.py does)."""
    s = str(size or "").strip().lower()
    if not s or s == "auto":
        return ""
    if s in ("square", "landscape", "portrait"):
        return f"Format: {s} orientation."
    wh = parse_size(s)
    if not wh:
        return ""
    w, h = wh
    if abs(w - h) <= max(w, h) * 0.04:
        orient = "square"
    else:
        orient = "landscape" if w > h else "portrait"
    rw, rh = min(_NAMED_RATIOS, key=lambda r: abs(r[0] / r[1] - w / h))
    if abs(rw / rh - w / h) > 0.03 * (w / h):
        from math import gcd
        g = gcd(w, h)
        rw, rh = w // g, h // g
    ratio = "1.91:1" if (rw, rh) == (191, 100) else f"{rw}:{rh}"
    return (f"Size: the final asset is exactly {w}x{h} pixels, aspect ratio {ratio} "
            f"({orient}). Make the image {orient} at that aspect ratio, or the "
            f"closest one the image tool offers, and keep everything important "
            f"away from the edges so a crop to exactly {w}x{h} loses nothing.")


class Provider(ABC):
    # --- declarative config (override per provider) ---
    name: str = ""
    chat_url: str = ""
    profile_dir: str = ""
    stream_url_fragments: list = []
    # WebSocket URL substrings the provider streams over (used only as a
    # "still streaming" heartbeat, not a completion signal). Empty = none.
    ws_url_fragments: list = []
    supports_images: bool = False
    # (cookie_name, domain_substring) that definitively proves a signed-in
    # session, if the site has one. login.py waits for this cookie to be
    # written before closing, which is far more reliable than a DOM check
    # (the DOM can look logged-in mid-redirect, before the session cookie is set).
    session_cookie: tuple | None = None
    # True when the provider's prose alongside a generated image is a real
    # caption worth keeping (ChatGPT); False when it's internal "thinking"
    # chrome to drop (Gemini).
    image_text_is_caption: bool = False
    # Composer + submit selectors used by the generic open_and_send().
    input_selector: str = 'div[contenteditable="true"]'
    send_selectors: list = ['button[aria-label="Send message"]']
    load_wait: float = 6.0  # seconds to let the page settle before typing
    # CSS for the turn the site renders for OUR prompt once it is submitted. Only
    # read until the answer starts, as one of the signs of life that keep the
    # "nostart" failure (CompletionTracker) from firing. Empty = cannot tell.
    user_turn_selector: str = ""
    # When True, do NOT stream incremental deltas — emit the final full answer
    # once at completion. Needed for providers whose extracted text *reshapes*
    # near the end (e.g. ChatGPT: a code answer flattens to "Python\nRun\n<code>"
    # while streaming, then becomes a ```fenced``` block once its CodeMirror card
    # finalizes). Append-only SSE can't un-send that divergent prefix, so we
    # buffer. Incremental (False) is fine for providers whose text only grows.
    buffered_stream: bool = False

    # --- account-side history (optional) ---
    # True when the provider can read and delete its own conversation list.
    # The history endpoints and the dashboard's import button turn themselves
    # off for a provider that leaves this False.
    supports_history: bool = False
    # Origin the history calls run against, so a page carrying the signed-in
    # session can be found among the browser's open tabs.
    site_origin: str = ""

    # --- image input (upload) ---
    # True when this provider can accept attached files (images) with a prompt.
    supports_upload: bool = False
    # Click path used only when no <input type="file"> exists in the DOM: a list
    # of steps, each step a list of alternatives ("<css>" or "text:<needle>")
    # tried in order. Clicking the last step is expected to open the file
    # chooser, which CDP intercepts (see the module docstring above).
    attach_click_path: list = []
    # JS returning JSON {"ready": <attachment chips visible>, "busy": <upload in
    # flight>}. Used to wait until the site has actually accepted+uploaded the
    # files before we submit — sending too early loses the attachment.
    attachment_ready_js: str = ""
    upload_timeout: float = 90.0    # max wait for chips to appear / upload to finish
    upload_settle: float = 2.0      # extra pause once the site reports it's ready

    def new_monitor(self) -> StreamMonitor:
        return StreamMonitor(self.stream_url_fragments, self.ws_url_fragments)

    # ------------------------------------------------------------------
    # Per-request options (model choice, image requests)
    # ------------------------------------------------------------------
    async def prepare_page(self, page, options: dict) -> None:
        """Called once the fresh chat page is visible, before anything is
        attached or typed. ``options`` carries ``kind`` ("chat" or "image")
        and ``variant`` (the request's ``model_variant``, or None). Default:
        nothing. ChatGPT sets its model picker here and, for an image, reads
        the site's own image counter, raising ``UsageLimitReached`` when too
        few are left."""

    async def answered_model(self, page) -> dict:
        """Which model the site says answered, from its own record of the
        conversation, read after the answer is in. {} when it cannot tell."""
        return {}

    def status_extra(self) -> dict:
        """Extra fields for this provider's /api/status entry."""
        return {}

    def image_prompt(self, prompt: str, size: str | None = None,
                     background: str | None = None) -> str:
        """The prompt an image request types: the caller's words, then the
        size and background asked for, in words, since the sites' image
        tools take them from the conversation."""
        lines = [prompt.strip()]
        sized = size_instruction(size)
        if sized:
            lines.append(sized)
        if (background or "").strip().lower() == "transparent":
            lines.append("Background: transparent (a PNG with an alpha channel), "
                         "with no backdrop, floor or shadow plate behind the subject.")
        return "\n\n".join(lines)

    # ------------------------------------------------------------------
    # Uploads (generic; see the module docstring for the two mechanisms)
    # ------------------------------------------------------------------
    async def attach_files(self, page, paths: list) -> int:
        """Attach local files to the composer's file picker. Returns how many
        files were handed over (0 = nothing worked). Generic across providers."""
        paths = [str(p) for p in paths]
        if not paths:
            return 0
        if await self._attach_via_input(page, paths):
            return len(paths)
        if await self._attach_via_chooser(page, paths):
            return len(paths)
        raise RuntimeError(
            f"[{self.name}] could not attach {len(paths)} file(s): no usable file "
            f"input and the file chooser never opened (site UI changed, or signed out?)"
        )

    async def _attach_via_input(self, page, paths: list) -> bool:
        """Feed the files to an <input type=file> already in the page. Tries each
        candidate input (best-scored first) and keeps the one the site reacts to."""
        from nodriver import cdp
        try:
            count = int(await page.evaluate(_COUNT_FILE_INPUTS_JS) or 0)
        except Exception:
            count = 0
        many = "true" if len(paths) > 1 else "false"
        for i in range(min(count, 4)):
            remote = None
            try:
                remote = await eval_handle(page, _NTH_FILE_INPUT_JS % (i, many))
                if not remote or not remote.object_id:
                    continue
                before = await self.attachment_count(page)
                await page.send(cdp.dom.set_file_input_files(
                    files=list(paths), object_id=remote.object_id))
                logger.info(f"[{self.name}] attached {len(paths)} file(s) via file input #{i}")
                # Confirm the site took them; a wrong input (e.g. an avatar
                # picker) silently swallows the files, so try the next one.
                # Only wait for the CHIPS here, not for the upload to finish:
                # requiring idle would time out on a slow upload and re-attach
                # the same files to the next input (two chips, image sent twice).
                # open_and_send waits for idle before submitting.
                if await self.wait_uploads_ready(page, before + len(paths), timeout=15.0,
                                                 require_idle=False):
                    return True
                got = (await self.attachment_count(page)) - before
                if 0 < got < len(paths):
                    # The input took some of the files: a single-file input.
                    # Giving all of them to the next input would attach the
                    # first ones twice, so hand this one the rest, one each.
                    logger.warning(f"[{self.name}] file input #{i} took {got} of "
                                   f"{len(paths)} file(s); adding the rest one at a time")
                    for p in paths[got:]:
                        n0 = await self.attachment_count(page)
                        await page.send(cdp.dom.set_file_input_files(
                            files=[p], object_id=remote.object_id))
                        if not await self.wait_uploads_ready(page, n0 + 1, timeout=15.0,
                                                             require_idle=False):
                            logger.warning(f"[{self.name}] {p} did not show as attached; "
                                           f"sending without it rather than twice")
                            break
                    return True
                logger.warning(f"[{self.name}] file input #{i} showed no attachment; trying next")
            except Exception as e:
                logger.warning(f"[{self.name}] file input #{i} rejected the files: {e}")
            finally:
                if remote is not None and remote.object_id:
                    try:
                        await page.send(cdp.runtime.release_object(object_id=remote.object_id))
                    except Exception:
                        pass
        return False

    async def _attach_via_chooser(self, page, paths: list) -> bool:
        """Click the site's attach affordance and answer the file chooser over
        CDP. Interception stays ON for the tab's lifetime on purpose: an
        un-intercepted native file dialog would block the renderer forever, and
        this browser has no human to dismiss it."""
        from nodriver import cdp
        if not self.attach_click_path:
            return False

        opened = asyncio.Event()
        box: dict = {}

        def _on_chooser(ev):
            box["backend_node_id"] = ev.backend_node_id
            opened.set()

        try:
            page.add_handler(cdp.page.FileChooserOpened, _on_chooser)
            await page.send(cdp.page.enable())
            await page.send(cdp.page.set_intercept_file_chooser_dialog(enabled=True))
        except Exception as e:
            logger.warning(f"[{self.name}] file-chooser interception unavailable: {e}")
            return False

        try:
            for step, alts in enumerate(self.attach_click_path):
                if opened.is_set():
                    break  # an earlier click already opened it
                clicked = await page.evaluate(_CLICK_ALTS_JS % json.dumps(list(alts)))
                logger.info(f"[{self.name}] attach step {step + 1}: "
                            f"{clicked or 'no match for ' + str(list(alts))}")
                if not clicked:
                    continue
                try:
                    await asyncio.wait_for(opened.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass  # not the final step, or the menu is still animating
            if not opened.is_set():
                try:
                    await asyncio.wait_for(opened.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning(f"[{self.name}] file chooser never opened")
                    return False

            await page.send(cdp.dom.set_file_input_files(
                files=list(paths), backend_node_id=box["backend_node_id"]))
            logger.info(f"[{self.name}] attached {len(paths)} file(s) via file chooser")
            # Chips only (see _attach_via_input); open_and_send waits for idle.
            return await self.wait_uploads_ready(page, len(paths), require_idle=False)
        except Exception as e:
            logger.warning(f"[{self.name}] chooser attach failed: {e}")
            return False
        finally:
            # Singular remove_handler — this runs on every attach, so leaving a
            # handler behind would pile up one stale callback per request.
            try:
                page.remove_handler(cdp.page.FileChooserOpened, _on_chooser)
            except Exception:
                pass

    async def attachment_count(self, page) -> int:
        """Attachment chips in the composer now (0 when unreadable or when the
        provider declares no ``attachment_ready_js``)."""
        if not self.attachment_ready_js:
            return 0
        try:
            raw = await page.evaluate(self.attachment_ready_js)
            return int(json.loads(raw).get("ready") or 0) if isinstance(raw, str) else 0
        except Exception:
            return 0

    async def wait_uploads_ready(self, page, n: int, timeout: float = None,
                                 require_idle: bool = True) -> bool:
        """Wait until the site shows ``n`` attachment chips (and, with
        ``require_idle``, that no upload is still in flight). Without
        ``attachment_ready_js`` we can only pause and hope."""
        if n <= 0:
            return True
        if not self.attachment_ready_js:
            await asyncio.sleep(self.upload_settle)
            return True
        deadline = time.monotonic() + (self.upload_timeout if timeout is None else timeout)
        stable = 0
        while time.monotonic() < deadline:
            ready, busy = 0, False
            try:
                raw = await page.evaluate(self.attachment_ready_js)
                if isinstance(raw, str):
                    st = json.loads(raw)
                    ready, busy = int(st.get("ready") or 0), bool(st.get("busy"))
            except Exception:
                pass
            if ready >= n and not (busy and require_idle):
                stable += 1
                if stable >= 2:  # two consecutive clean polls, then settle
                    await asyncio.sleep(self.upload_settle)
                    return True
            else:
                stable = 0
            await asyncio.sleep(1.0)
        logger.warning(f"[{self.name}] upload not confirmed ready after "
                       f"{self.upload_timeout if timeout is None else timeout:.0f}s "
                       f"(wanted {n} attachment(s))")
        return False

    # ------------------------------------------------------------------
    # Page visibility (see the note on CHROME_ARGS)
    # ------------------------------------------------------------------
    async def page_visibility(self, page) -> str:
        """``document.visibilityState`` of the drive's page, or "" if unreadable."""
        try:
            state = await page.evaluate("document.visibilityState")
        except Exception:
            return ""
        return state if isinstance(state, str) else ""

    async def ensure_visible(self, page) -> None:
        """Refuse to drive a hidden page, after trying to un-hide it.

        A hidden page takes the prompt and never renders an answer, so without
        this check the drive would wait out the whole deadline for nothing.
        ``--disable-backgrounding-occluded-windows`` keeps an off-screen or
        covered window visible, so this only fires for what the flag cannot
        cover (a minimised or unmapped window, or a Chrome started without the
        flag). The repair is the browser's own: restore the window, put it at
        the screen's origin and raise it. If the page is still hidden, raise an
        error that names the cause instead of returning an empty answer.
        """
        state = await self.page_visibility(page)
        if state in ("visible", ""):  # "" = unreadable; don't block a drive on it
            return
        logger.warning(f"[{self.name}] page is {state!r} before sending; restoring "
                       f"its window on-screen and raising it")
        try:
            from nodriver import cdp
            # TargetID(...)/WindowID(...) because the commands call .to_json() on
            # their arguments, which a plain str or int does not have.
            window_id, _bounds = await page.send(cdp.browser.get_window_for_target(
                cdp.target.TargetID(page.target.target_id)))
            window_id = cdp.browser.WindowID(window_id)
            # A window state change cannot be combined with a position change.
            await page.send(cdp.browser.set_window_bounds(
                window_id, cdp.browser.Bounds(window_state=cdp.browser.WindowState.NORMAL)))
            await page.send(cdp.browser.set_window_bounds(
                window_id, cdp.browser.Bounds(left=0, top=0)))
            # Moving alone is not enough when another window covers the origin
            # (measured 2026-09-26): the raise is what makes it visible.
            await page.send(cdp.page.bring_to_front())
        except Exception as e:
            logger.warning(f"[{self.name}] could not restore the window: {e}")
        for _ in range(10):
            await asyncio.sleep(0.5)
            state = await self.page_visibility(page)
            if state == "visible":
                logger.info(f"[{self.name}] page visible again")
                return
        display = os.environ.get("DISPLAY", "?")
        raise RuntimeError(
            f"[{self.name}] the chat page is {state or 'unreadable'!r}, so it would "
            f"never render an answer: its Chrome window on DISPLAY {display} is "
            f"off-screen, covered or minimised, and restoring it did not help. "
            f"Check `DISPLAY={display} xwininfo -root -tree` for the window's position."
        )

    async def has_user_turn(self, page):
        """True/False: does the page show our submitted prompt? None when the
        provider declares no ``user_turn_selector`` or the read failed."""
        if not self.user_turn_selector:
            return None
        try:
            return bool(await page.evaluate(
                "!!document.querySelector(%s)" % json.dumps(self.user_turn_selector)))
        except Exception:
            return None

    async def type_prompt(self, page, input_box, prompt: str) -> None:
        """Put the prompt into the composer. Default: one CDP key event per
        character (nodriver's send_keys). Override for a composer that drops
        some characters typed that way (ChatGPT's drops line breaks)."""
        await input_box.send_keys(prompt)

    async def composer_hint(self, page) -> str:
        """Why the composer was not found, read from what the page does hold.

        A site redesign leaves an editable element that the selector no longer
        matches; a sign-in or verification wall, or a page that never loaded,
        leaves none. On 11 Oct 2026 ChatGPT's redesign produced a bare "input box
        not found" and the watchdog filed it as an expired login. Returns "" when
        the page cannot be read."""
        try:
            raw = await page.evaluate(
                "JSON.stringify({ce: document.querySelectorAll('[contenteditable=\"true\"]').length,"
                " ta: document.querySelectorAll('textarea').length, url: location.href})")
            st = json.loads(raw) if isinstance(raw, str) else {}
            ce, ta = int(st.get("ce") or 0), int(st.get("ta") or 0)
        except Exception:
            return ""
        if ce or ta:
            return (f"; the page has {ce} contenteditable and {ta} textarea element(s), so "
                    f"the site has probably changed its composer: read the live page and "
                    f"update input_selector in {type(self).__module__}")
        return (f"; the page has no editable element at all (a sign-in or verification "
                f"wall, or a page that did not load) at {str(st.get('url') or '')[:100]}")

    async def composer_text(self, page) -> str | None:
        """What the composer holds now, or None when it cannot be read."""
        try:
            v = await page.evaluate(
                "(function(){var e=document.querySelector(%s);"
                "return e?(e.value!==undefined?e.value:(e.innerText||'')):null;})()"
                % json.dumps(self.input_selector))
        except Exception:
            return None
        return v if isinstance(v, str) else None

    async def _send_landed(self, page) -> bool | None:
        """True once the page shows our turn or the composer has emptied,
        False while neither, None when the provider cannot tell."""
        turn = await self.has_user_turn(page)
        if turn:
            return True
        left = await self.composer_text(page)
        if left is not None and not left.strip():
            return True
        if turn is None and left is None:
            return None
        return False

    async def _click_send(self, page) -> bool:
        for sel in self.send_selectors:
            try:
                btn = await page.select(sel, timeout=3)
                if btn:
                    await btn.click()
                    return True
            except Exception:
                continue
        return False

    async def open_and_send(self, browser, prompt: str, attachments: list = None,
                            options: dict | None = None):
        """Open a fresh chat, attach any input images, type the prompt, submit.
        Returns (page, monitor).

        Generic across providers that use a contenteditable/textarea composer
        plus a send button. Override only if a site needs something special.
        ``options`` goes to ``prepare_page`` (model choice, request kind).
        """
        page = await browser.get(self.chat_url)
        monitor = self.new_monitor()
        monitor.attach(page)
        await monitor.enable_bodies(page)

        logger.info(f"[{self.name}] waiting for page to load...")
        await asyncio.sleep(self.load_wait)
        await self.ensure_visible(page)
        # The caller's own dict, so what prepare_page notes (ChatGPT: the
        # picker reading) reaches the caller.
        await self.prepare_page(page, options if options is not None else {})

        if attachments:
            if not self.supports_upload:
                raise RuntimeError(f"[{self.name}] does not support image input")
            logger.info(f"[{self.name}] attaching {len(attachments)} input file(s)...")
            await self.attach_files(page, attachments)

        input_box = await page.select(self.input_selector, timeout=20)
        if not input_box:
            raise RuntimeError(f"[{self.name}] input box not found ({self.input_selector})"
                               + await self.composer_hint(page))

        logger.info(f"[{self.name}] sending prompt ({len(prompt)} chars)...")
        await self.type_prompt(page, input_box, prompt)
        # Nudge frameworks that only react to a real 'input' event.
        await page.evaluate(
            "const el = document.querySelector(%r); "
            "if (el) el.dispatchEvent(new Event('input', {bubbles: true}));"
            % self.input_selector
        )
        await asyncio.sleep(1.2)

        # An upload can still be in flight while we type; submitting then would
        # send the prompt without the image (or with a half-uploaded one).
        if attachments:
            await self.wait_uploads_ready(page, len(attachments))

        if not await self._click_send(page):
            await input_box.send_keys("\n")

        # A click can land while the page re-renders the button and do nothing
        # (13 Oct 2026, ChatGPT: the prompt sat in the composer, the drive
        # failed at "nostart" after 60 s, and the site kept the prompt as a
        # draft). Confirm the send; if neither our turn nor an empty composer
        # shows within a few seconds, click once more, then try Enter.
        for retry in ("click", "enter", None):
            landed = None
            for _ in range(8):
                await asyncio.sleep(0.75)
                landed = await self._send_landed(page)
                if landed is not False:
                    break
            if landed is not False or retry is None:
                break
            logger.warning(f"[{self.name}] the prompt is still in the composer after "
                           f"sending; trying the send again ({retry})")
            if retry == "click" and await self._click_send(page):
                continue
            try:
                await input_box.send_keys("\n")
            except Exception as e:
                logger.warning(f"[{self.name}] Enter fallback failed: {e}")

        return page, monitor

    @abstractmethod
    async def get_response_text(self, page) -> str:
        """Current text of the last assistant response (UI chrome stripped)."""

    @abstractmethod
    async def is_generating(self, page) -> bool:
        """True while the model is still producing output."""

    async def image_status(self, page) -> dict:
        """Generated-image render status: how many have rendered (loaded), how
        many are still blank (pending), and whether a loading placeholder is
        showing. Default: none (text-only provider)."""
        return {"loaded": 0, "pending": 0, "creating": False}

    async def get_images(self, page) -> list:
        """Return [{mime, b64|src, alt}] for generated images in the last
        response. Default: none."""
        return []

    @asynccontextmanager
    async def session_tab(self, browser):
        """Yield a page on the site's own origin, for calls that need the
        signed-in session but no drive (reading or deleting the conversation
        list).

        Reuses a tab that is already on the site: these calls are fetches
        against the site's own API and never navigate, so they cannot disturb a
        conversation sitting there. When no such tab exists it navigates the
        browser's FIRST tab — the very tab a drive uses (``browser.get`` without
        ``new_tab`` always takes ``targets[0]``), so the profile still has
        exactly one page and looks no different from a normal drive.

        **Never open a second tab here.** ChatGPT multiplexes one WebSocket per
        profile: with two live tabs on the site, one conversation gets the
        stream and the other reads 0 chars until the deadline. That is the same
        wall 2026-08-18's tab-pool attempt hit, and opening a session tab
        alongside the drive's reproduced it on 2026-08-20.
        """
        origin = self.site_origin or self.chat_url
        for tab in list(getattr(browser, "tabs", None) or []):
            url = getattr(getattr(tab, "target", None), "url", "") or ""
            if origin and url.startswith(origin):
                yield tab
                return

        page = await browser.get(self.chat_url)
        # A tab mid-navigation answers about:blank for a moment, and a relative
        # fetch there has no origin to resolve against.
        for _ in range(40):
            try:
                here = await page.evaluate("location.origin")
            except Exception:
                here = ""
            if isinstance(here, str) and here and origin.startswith(here):
                break
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError(f"[{self.name}] could not reach {origin}")
        yield page

    async def conversation_id(self, page) -> str:
        """Id of the conversation the current drive landed on, "" if unknown.

        Returned to the caller so a client that keeps its own copy of a chat can
        later delete the account-side thread that belongs to it.
        """
        return ""

    async def list_conversations(self, page, limit: int = 200,
                                 offset: int = 0) -> dict:
        """The account's conversation titles, newest first:
        ``{"conversations": [{id, title, created, updated}], "has_more": bool}``.

        Titles only, deliberately. Reading every thread's messages up front is
        what trips a site's rate limiter, so a body is fetched one at a time by
        ``fetch_conversation`` when someone actually opens it — the same way the
        site's own sidebar behaves. Default: no readable history.
        """
        return {"conversations": [], "has_more": False}

    async def fetch_conversation(self, page, conv_id: str) -> dict:
        """One conversation with its messages:
        ``{id, title, created, updated, messages: [{role, content, ts}]}``.

        Raises ``RateLimited`` when the site refuses for asking too often, so a
        caller can say "try again shortly" instead of reporting a fault.
        """
        raise NotImplementedError(f"[{self.name}] cannot read a conversation")

    async def delete_conversation(self, page, conv_id: str) -> bool:
        """Delete one conversation by id. This is the account's own thread, not
        a local copy, so it is as destructive as the site's own Delete."""
        return False

    async def delete_conversations(self, page, ids: list) -> tuple:
        """Delete many, returning ``(deleted_ids, {failed_id: why})``.

        One id at a time is the naive shape and it is unusably slow: each one
        costs a CDP round trip plus a session read, which measured ~4s, so
        clearing 70 chats took five minutes with the client still waiting. A
        provider that can do the whole list inside one page call should override
        this; the loop stays as the fallback for one that can't.
        """
        done, failed = [], {}
        for conv_id in ids:
            if await self.delete_conversation(page, conv_id):
                done.append(conv_id)
            else:
                failed[conv_id] = "delete refused"
        return done, failed

    async def discard_conversation(self, page) -> bool:
        """Delete the conversation this drive just created.

        An ephemeral request is scratch work: the caller wanted an answer, not a
        thread in the account's history. Called after the answer (and any images)
        have been read, never before. Failure is not fatal — the answer is
        already in hand — so implementations should log and return False rather
        than raise. Default: the provider keeps no history we can remove.
        """
        return False

    @abstractmethod
    async def logged_in(self, page) -> bool:
        """True if a usable, signed-in session is present (used by login.py)."""
