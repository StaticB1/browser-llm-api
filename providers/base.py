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
    """

    def __init__(self, url_fragments):
        self._fragments = list(url_fragments)
        self._tracked: set[str] = set()
        self.stream_done = asyncio.Event()

    def attach(self, tab):
        try:
            from nodriver import cdp
            tab.add_handler(cdp.network.RequestWillBeSent, self._on_request)
            tab.add_handler(cdp.network.LoadingFinished, self._on_finished)
            tab.add_handler(cdp.network.LoadingFailed, self._on_finished_err)
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


class Provider(ABC):
    # --- declarative config (override per provider) ---
    name: str = ""
    chat_url: str = ""
    profile_dir: str = ""
    stream_url_fragments: list = []
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

    def new_monitor(self) -> StreamMonitor:
        return StreamMonitor(self.stream_url_fragments)

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
