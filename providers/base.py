"""
Provider abstraction for the browser-automation server.

Each provider drives a different chat web UI (Gemini, ChatGPT) but shares the
same lifecycle: open a chat, type a prompt, submit, then poll the DOM for the
streamed answer (and any generated images). The generic completion loop lives
in ``server.py``; providers supply the site-specific URL, selectors, and
extractors.

A provider is mostly declarative — set the class attributes (``chat_url``,
``profile_dir``, ``input_selector``, ``send_selectors``, ``stream_url_fragments``)
and implement the site-specific reads (``get_response_text``, ``is_generating``,
``logged_in``, and optionally ``image_status`` / ``get_images``).
"""
import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod

logger = logging.getLogger("gemini_server")

# Chrome flags enabling software (SwiftShader) GL so canvas/WebGL renders under a
# headless Xvfb display with no GPU. ChatGPT's GPT-image generation draws on a
# <canvas>; without a GL backend it stalls forever on the "rendering" tile.
# Harmless on a real GPU display. Passed to every uc.start() (server + login).
CHROME_ARGS = [
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
]


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

    def _on_request(self, event):
        url = event.request.url
        if self._fragments and any(k in url for k in self._fragments):
            self._tracked.add(event.request_id)

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
    """

    # tuning (seconds)
    SILENT_TEXT_DONE = 2.5          # not generating + text unchanged this long -> done
    SILENT_EMPTY_DONE = 10.0        # generated but nothing extractable -> give up
    IMAGE_STABLE = 4.0              # rendered-image count stable this long -> done
    WS_ACTIVE_WINDOW = 2.0          # a WS frame within this window == still streaming
    FALSE_CREATING_TIMEOUT = 45.0   # "creating" stuck with no image after gen ended -> ignore it

    _PLACEHOLDER_RE = re.compile(r'^(creating|generating)\b', re.I)

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

    def _is_placeholder(self, text: str) -> bool:
        """True for transient 'Creating your image…' loading text."""
        return bool(text) and bool(self._PLACEHOLDER_RE.match(text.strip()))

    def silent_for(self, now: float) -> float:
        return 0.0 if self._last_change is None else now - self._last_change

    def feed(self, now, raw_text, is_generating, img, *, cdp_done=False):
        if self._last_change is None:
            self._last_change = now

        loaded = img.get("loaded", 0)
        creating = bool(img.get("creating"))
        pending = img.get("pending", 0) > 0

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

        # Never surface the loading placeholder / thinking text shown while an
        # image is (really) rendering.
        text = "" if (self._is_placeholder(raw_text) or img_pending) else raw_text
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
        # rather than hang. Suppressed once an image was in flight.
        if (self.text_len == 0 and self._saw_generation and not self._saw_creating
                and silent >= self.SILENT_EMPTY_DONE):
            return chunk, "empty"
        return chunk, None


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
    # When True, do NOT stream incremental deltas — emit the final full answer
    # once at completion. Needed for providers whose extracted text *reshapes*
    # near the end (e.g. ChatGPT: a code answer flattens to "Python\nRun\n<code>"
    # while streaming, then becomes a ```fenced``` block once its CodeMirror card
    # finalizes). Append-only SSE can't un-send that divergent prefix, so we
    # buffer. Incremental (False) is fine for providers whose text only grows.
    buffered_stream: bool = False

    def new_monitor(self) -> StreamMonitor:
        return StreamMonitor(self.stream_url_fragments, self.ws_url_fragments)

    async def open_and_send(self, browser, prompt: str):
        """Open a fresh chat, type the prompt, submit. Returns (page, monitor).

        Generic across providers that use a contenteditable/textarea composer
        plus a send button. Override only if a site needs something special.
        """
        page = await browser.get(self.chat_url)
        monitor = self.new_monitor()
        monitor.attach(page)

        logger.info(f"[{self.name}] waiting for page to load...")
        await asyncio.sleep(self.load_wait)

        input_box = await page.select(self.input_selector, timeout=20)
        if not input_box:
            raise RuntimeError(f"[{self.name}] input box not found ({self.input_selector})")

        logger.info(f"[{self.name}] sending prompt ({len(prompt)} chars)...")
        await input_box.send_keys(prompt)
        # Nudge frameworks that only react to a real 'input' event.
        await page.evaluate(
            "const el = document.querySelector(%r); "
            "if (el) el.dispatchEvent(new Event('input', {bubbles: true}));"
            % self.input_selector
        )
        await asyncio.sleep(1.2)

        sent = False
        for sel in self.send_selectors:
            try:
                btn = await page.select(sel, timeout=3)
                if btn:
                    await btn.click()
                    sent = True
                    break
            except Exception:
                continue
        if not sent:
            await input_box.send_keys("\n")

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

    @abstractmethod
    async def logged_in(self, page) -> bool:
        """True if a usable, signed-in session is present (used by login.py)."""
