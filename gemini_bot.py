import nodriver as uc
import asyncio
import logging
import time
from datetime import datetime

# --- CONFIGURATION & LOGGING SETUP ---
LOG_FILE = "gemini_session.log"  # single file, overwritten each run

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CDP-level fix: suppress unknown events like DOM.adoptedStyleSheetsModified
# nodriver uses the `cdp` package which raises KeyError for unregistered
# events. Patch the parser once at import time.
# ---------------------------------------------------------------------------
try:
    # nodriver bundles cdp internally — import from there, not standalone 'cdp'
    import nodriver.cdp.util as _cdp_util
    _original_parse = _cdp_util.parse_json_event

    def _safe_parse(json: dict):
        try:
            return _original_parse(json)
        except KeyError:
            # Unknown event (e.g. DOM.adoptedStyleSheetsModified) — ignore.
            return None

    _cdp_util.parse_json_event = _safe_parse
    logger.info("CDP event parser patched (unknown events will be suppressed).")
except Exception as e:
    logger.warning(f"Could not patch CDP parser: {e}. KeyErrors may still appear.")


# ---------------------------------------------------------------------------
# Network-based completion detection
# ---------------------------------------------------------------------------
class StreamMonitor:
    """
    Watches CDP Network events for the Gemini streaming API request.
    When that specific request finishes (LoadingFinished / ResponseReceived
    with no body left), we know the LLM has sent its last token.
    """

    def __init__(self):
        self._gemini_request_ids: set[str] = set()
        self._completed_ids: set[str] = set()
        self.stream_done = asyncio.Event()

    def attach(self, tab):
        """Register CDP handlers on the tab."""
        try:
            from nodriver import cdp  # nodriver's internal cdp package
            tab.add_handler(cdp.network.RequestWillBeSent, self._on_request)
            tab.add_handler(cdp.network.LoadingFinished, self._on_loading_finished)
            tab.add_handler(cdp.network.LoadingFailed, self._on_loading_failed)
            logger.info("Network monitor attached via CDP.")
        except Exception as e:
            logger.warning(f"CDP network monitoring unavailable: {e}. Falling back to DOM polling.")

    def _on_request(self, event):
        url = event.request.url
        # Gemini's streaming endpoint contains one of these fragments
        if any(k in url for k in ("streamGenerateContent", "GenerateContent", "_/BardChatUi/data")):
            self._gemini_request_ids.add(event.request_id)
            logger.info(f"Tracking Gemini API request: {event.request_id} → {url[:80]}")

    def _on_loading_finished(self, event):
        if event.request_id in self._gemini_request_ids:
            self._completed_ids.add(event.request_id)
            logger.info(f"Gemini stream request {event.request_id} fully loaded.")
            self.stream_done.set()

    def _on_loading_failed(self, event):
        if event.request_id in self._gemini_request_ids:
            logger.warning(f"Gemini stream request {event.request_id} failed: {event.error_text}")
            self.stream_done.set()  # Unblock — let DOM check decide what happened


# ---------------------------------------------------------------------------
# DOM helpers
# ---------------------------------------------------------------------------
async def get_full_response_text(page) -> str:
    """
    Recursively walk shadow roots to collect all text from the last
    model-response element. page.evaluate() normally can't read Shadow DOM,
    but a manual shadow-piercing walker running inside the page context can.
    """
    try:
        result = await page.evaluate("""
            (function() {
                const SKIP = new Set([
                    'script','style','button','svg','path','img','picture',
                    'source','nav','header','footer','aside','dialog',
                    'mat-icon','iron-icon','tp-yt-paper-tooltip'
                ]);
                function collectText(root) {
                    if (!root) return '';
                    let out = '';
                    for (const node of root.childNodes) {
                        if (node.nodeType === 3) {
                            out += node.textContent;
                        } else if (node.nodeType === 1) {
                            const tag = node.tagName.toLowerCase();
                            if (SKIP.has(tag)) continue;
                            if (node.getAttribute && node.getAttribute('aria-hidden') === 'true') continue;
                            if (node.shadowRoot) {
                                out += collectText(node.shadowRoot);
                            } else {
                                out += collectText(node);
                            }
                        }
                    }
                    return out;
                }

                const responses = document.querySelectorAll('model-response');
                if (!responses.length) return '';
                const last = responses[responses.length - 1];
                const root = last.shadowRoot || last;
                return collectText(root).replace(/\\s+/g, ' ').trim();
            })()
        """)
        if not isinstance(result, str):
            return ""
        # Strip UI chrome injected by Gemini's shadow DOM
        for prefix in ("Show thinking Gemini said ", "Show thinking ", "Gemini said ", "Gemini said\n"):
            if result.startswith(prefix):
                result = result[len(prefix):]
                break
        for suffix in (" Sources", "\nSources", " Sources\n"):
            if result.endswith(suffix):
                result = result[:-len(suffix)].rstrip()
                break
        return result
    except Exception:
        return ""


async def is_generating(page) -> bool:
    """True if the 'Stop generating' button is present in the DOM."""
    try:
        btn = await page.select('button[aria-label="Stop generating"]', timeout=1)
        return btn is not None
    except Exception:
        return False


async def has_copy_button(page) -> bool:
    """Gemini renders a copy button once the response is finalized."""
    try:
        btn = await page.select('[aria-label="Copy"]', timeout=1)
        return btn is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core wait function — combines CDP signal + DOM stability fallback
# ---------------------------------------------------------------------------
async def wait_for_complete_response(
    page,
    monitor: StreamMonitor,
    *,
    poll_interval: float = 0.8,
    stable_duration: float = 5.0,   # seconds of unchanged text before we trust it
    max_wait: float = 240.0,        # hard ceiling (4 minutes)
) -> str:
    """
    Strategy (in priority order):
    1. If CDP fires LoadingFinished for the Gemini stream → wait one extra
       stable_duration window then capture.  This is the fastest, most reliable path.
    2. If the Copy button appears and 'Stop' is gone → same extra window then capture.
    3. Fallback: pure DOM stability — text unchanged for `stable_duration` seconds
       with no 'Stop' button visible.
    """
    deadline = time.monotonic() + max_wait
    last_text = ""
    last_change_at = time.monotonic()
    cdp_signal_at: float | None = None

    logger.info("Waiting for complete response…")

    while time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)

        current_text = await get_full_response_text(page)
        now = time.monotonic()

        # Track text changes
        if current_text != last_text:
            last_text = current_text
            last_change_at = now
            logger.info(f"Text growing… {len(current_text)} chars")

        still_generating = await is_generating(page)
        copy_present = await has_copy_button(page)
        silent_for = now - last_change_at

        # Record first time CDP says the stream is done
        if monitor.stream_done.is_set() and cdp_signal_at is None:
            cdp_signal_at = now
            logger.info("CDP: stream request completed.")

        # --- Completion checks ---

        # Path 1: CDP confirmed + text silent for stable_duration
        if cdp_signal_at is not None and silent_for >= stable_duration and not still_generating:
            logger.info(f"Done (CDP path). Stable for {silent_for:.1f}s after stream closed.")
            return last_text

        # Path 2: Copy button visible, Stop gone, text silent for longer window.
        # NOTE: Gemini 2026 shows the copy button mid-stream, so we require a
        # longer silence here to avoid capturing a partial response.
        if copy_present and not still_generating and silent_for >= stable_duration * 3:
            logger.info(f"Done (Copy-button path). Stable for {silent_for:.1f}s.")
            return last_text

        # Path 3: Pure DOM stability (long pause fallback)
        # Require a longer silence (2× stable_duration) to avoid false positives
        if (not still_generating
                and len(last_text) > 50
                and silent_for >= stable_duration * 2):
            logger.info(f"Done (DOM-stability fallback). Stable for {silent_for:.1f}s.")
            return last_text

        # Progress log every 10 seconds
        if int(now) % 10 == 0:
            logger.info(
                f"Waiting… chars={len(last_text)}, "
                f"silent={silent_for:.1f}s, generating={still_generating}, "
                f"copy={copy_present}, cdp={'yes' if cdp_signal_at else 'no'}"
            )

    logger.warning("max_wait reached — returning whatever was captured.")
    return last_text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    logger.info("Starting Gemini Automation (CDP-enhanced)…")

    try:
        browser = await uc.start(user_data_dir='./gemini_profile')
        page = await browser.get('https://gemini.google.com')

        # Attach network monitor BEFORE the page does anything
        monitor = StreamMonitor()
        monitor.attach(page)

        logger.info("Waiting for page to stabilize…")
        await asyncio.sleep(7)

        # --- Find input box ---
        input_selector = 'div[contenteditable="true"]'
        input_box = await page.select(input_selector, timeout=25)

        if not input_box:
            logger.error("Input box not found.")
            await page.save_screenshot('error_no_input.png')
            return
        

        # --- Type prompt ---
        prompt = (
            "how to ride a bike"
        )
        logger.info(f"Typing: {prompt[:60]}…")
        await input_box.send_keys(prompt)

        await page.evaluate(f"""
            const el = document.querySelector('{input_selector}');
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
        """)
        await asyncio.sleep(1.5)

        # --- Submit ---
        try:
            send_btn = await page.select('button[aria-label="Send message"]', timeout=5)
            await send_btn.click()
            logger.info("Send button clicked.")
        except Exception:
            logger.warning("Button click failed; using Enter fallback.")
            await input_box.send_keys('\n')

        # --- Wait for the full response ---
        final_text = await wait_for_complete_response(page, monitor)

        # --- Save results ---
        if len(final_text) > 50:
            print(f"\n--- CAPTURED RESPONSE ({len(final_text)} chars) ---\n{final_text}\n")
            with open("gemini_research_data.txt", "w") as f:
                f.write(f"--- SESSION: {datetime.now()} ---\n{final_text}\n\n")
            logger.info("Saved to gemini_research_data.txt")
        else:
            logger.warning(f"Captured text too short ({len(final_text)} chars).")
            await page.save_screenshot('short_response_debug.png')

    except Exception as e:
        logger.critical(f"Error: {e}", exc_info=True)
        if 'page' in locals():
            await page.save_screenshot('crash_report.png')

    finally:
        logger.info("Task finished. Keeping browser open for 10s…")
        await asyncio.sleep(10)
        # browser.stop()


if __name__ == '__main__':
    uc.loop().run_until_complete(main())
