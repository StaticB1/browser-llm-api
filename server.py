"""
OpenAI-compatible API server backed by Gemini (browser automation).

Endpoints:
  GET  /v1/models
  POST /v1/chat/completions  (streaming + non-streaming)

Usage:
  python server.py

OpenClaw openclaw.json:
  {
    "models": {
      "providers": {
        "gemini-browser": {
          "baseUrl": "http://localhost:8000/v1",
          "apiKey": "local",
          "api": "openai-completions",
          "models": [{"id": "gemini-browser", "name": "Gemini (Browser)",
                       "contextWindow": 32000, "maxTokens": 8192}]
        }
      }
    },
    "agents": {"defaults": {"model": {"primary": "gemini-browser"}}}
  }
"""

import asyncio
import json
import time
import uuid
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import nodriver as uc
import nodriver.cdp.util as _cdp_util
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("gemini_server")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = logging.FileHandler("server.log", mode="w")
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler()
_sh.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_sh)

# ---------------------------------------------------------------------------
# CDP patch — suppress DOM.adoptedStyleSheetsModified KeyError
# ---------------------------------------------------------------------------
_original_parse = _cdp_util.parse_json_event

def _safe_parse(json_data: dict):
    try:
        return _original_parse(json_data)
    except KeyError:
        return None

_cdp_util.parse_json_event = _safe_parse

# ---------------------------------------------------------------------------
# Pydantic models (OpenAI wire format)
# ---------------------------------------------------------------------------
class Message(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = "gemini-browser"
    messages: list[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

# ---------------------------------------------------------------------------
# Browser state — one persistent instance, one request at a time
# ---------------------------------------------------------------------------
_browser: uc.Browser | None = None
_request_lock = asyncio.Lock()


async def get_browser() -> uc.Browser:
    global _browser
    if _browser is None:
        logger.info("Starting browser...")
        _browser = await uc.start(user_data_dir="./gemini_profile")
    return _browser


# ---------------------------------------------------------------------------
# CDP stream monitor (same as gemini_bot.py)
# ---------------------------------------------------------------------------
class StreamMonitor:
    def __init__(self):
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
        # Only track the actual LLM streaming endpoints, not quick setup requests
        if any(k in url for k in ("streamGenerateContent", "GenerateContent", "BardFrontendService")):
            self._tracked.add(event.request_id)

    def _on_finished(self, event):
        if event.request_id in self._tracked:
            self.stream_done.set()

    def _on_finished_err(self, event):
        if event.request_id in self._tracked:
            self.stream_done.set()


# ---------------------------------------------------------------------------
# DOM helpers
# ---------------------------------------------------------------------------
async def get_response_text(page) -> str:
    """Shadow-DOM-piercing text extractor for the last model-response."""
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
                            // skip aria-hidden UI chrome
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
                return collectText(last.shadowRoot || last)
                    .replace(/\\s+/g, ' ').trim();
            })()
        """)
        if not isinstance(result, str):
            return ""
        import re
        result = re.sub(r'^(Show thinking\s+)?(Gemini said\s*)', '', result)
        result = re.sub(r'\s*Sources\s*$', '', result).strip()
        return result
    except Exception:
        return ""


async def is_generating(page) -> bool:
    try:
        btn = await page.select('button[aria-label="Stop generating"]', timeout=1)
        return btn is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core: send prompt → return full response text
# ---------------------------------------------------------------------------
def _build_prompt(messages: list[Message]) -> str:
    """
    Flatten the OpenAI messages list into a single Gemini prompt.
    System messages become a preamble; multi-turn history is included
    so agents get coherent context.
    """
    system = [m.content for m in messages if m.role == "system"]
    turns = [m for m in messages if m.role != "system"]

    parts = []
    if system:
        parts.append("[Context/Instructions: " + " ".join(system) + "]")

    if len(turns) == 1:
        parts.append(turns[0].content)
    else:
        for m in turns:
            label = "User" if m.role == "user" else "Assistant"
            parts.append(f"{label}: {m.content}")

    return "\n\n".join(parts)


async def run_gemini(messages: list[Message]) -> AsyncGenerator[str, None]:
    """
    Open a fresh Gemini chat, send the prompt, and yield text chunks
    as they arrive. Yields the full delta on each poll tick.
    """
    prompt = _build_prompt(messages)
    browser = await get_browser()

    page = await browser.get("https://gemini.google.com/app")
    monitor = StreamMonitor()
    monitor.attach(page)

    logger.info("Waiting for Gemini to load...")
    await asyncio.sleep(6)

    # Find input
    input_selector = 'div[contenteditable="true"]'
    input_box = await page.select(input_selector, timeout=20)
    if not input_box:
        raise RuntimeError("Gemini input box not found.")

    # Type prompt
    logger.info(f"Sending prompt ({len(prompt)} chars)...")
    await input_box.send_keys(prompt)
    await page.evaluate(f"""
        const el = document.querySelector('{input_selector}');
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    """)
    await asyncio.sleep(1.2)

    # Submit
    try:
        btn = await page.select('button[aria-label="Send message"]', timeout=5)
        await btn.click()
    except Exception:
        await input_box.send_keys("\n")

    # Stream chunks
    last_len = 0
    last_change = time.monotonic()
    cdp_fired_at: float | None = None
    deadline = time.monotonic() + 240

    while time.monotonic() < deadline:
        await asyncio.sleep(0.8)

        text = await get_response_text(page)
        now = time.monotonic()

        if len(text) > last_len:
            chunk = text[last_len:]
            last_len = len(text)
            last_change = now
            yield chunk

        if monitor.stream_done.is_set() and cdp_fired_at is None:
            cdp_fired_at = now
            logger.info(f"CDP: stream closed. text so far={last_len}")

        still_gen = await is_generating(page)
        silent = now - last_change

        logger.debug(f"poll: text={last_len} silent={silent:.1f}s cdp={'yes' if cdp_fired_at else 'no'} gen={still_gen}")

        if cdp_fired_at and silent >= 5.0 and not still_gen and last_len > 0:
            logger.info(f"Done (CDP). {last_len} chars")
            break
        if not still_gen and last_len > 50 and silent >= 10.0:
            logger.info(f"Done (DOM fallback). {last_len} chars")
            break

    # Leave the tab open — closing or navigating away disrupts the browser


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm the browser on startup
    await get_browser()
    logger.info("Server ready.")
    yield
    if _browser:
        _browser.stop()


app = FastAPI(title="Gemini Browser API", lifespan=lifespan)


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": "gemini-browser",
                "object": "model",
                "created": 1700000000,
                "owned_by": "google",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages is empty")

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    async with _request_lock:
        if req.stream:
            # --- Streaming (SSE) ---
            async def event_stream():
                async for chunk in run_gemini(req.messages):
                    data = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": chunk},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(data)}\n\n"

                # Final chunk
                done = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": req.model,
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                }
                yield f"data: {json.dumps(done)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        else:
            # --- Non-streaming ---
            full_text = ""
            try:
                async for chunk in run_gemini(req.messages):
                    full_text += chunk
            except Exception as e:
                logger.error(f"run_gemini failed: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))

            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": full_text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": sum(len(m.content.split()) for m in req.messages),
                    "completion_tokens": len(full_text.split()),
                    "total_tokens": sum(len(m.content.split()) for m in req.messages)
                                   + len(full_text.split()),
                },
            }


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
