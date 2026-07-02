"""
OpenAI-compatible API server backed by Gemini (browser automation).

Endpoints:
  GET  /v1/models
  POST /v1/chat/completions   (streaming + non-streaming; images inline)
  POST /v1/images/generations (OpenAI-style image generation)
  GET  /images/<file>         (saved images, see GEMINI_IMAGE_DIR)

Usage:
  python server.py            # listens on 0.0.0.0:8081

OpenClaw openclaw.json:
  {
    "models": {
      "providers": {
        "gemini-browser": {
          "baseUrl": "http://localhost:8081/v1",
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
import base64
import json
import os
import re
import time
import uuid
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

import nodriver as uc
import nodriver.cdp.util as _cdp_util
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
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
# Generated-image storage
#   GEMINI_IMAGE_DIR  — folder to save generated images into (created if needed)
#   GEMINI_PUBLIC_URL — base URL images are served under (for the returned links)
# ---------------------------------------------------------------------------
IMAGE_DIR = os.environ.get("GEMINI_IMAGE_DIR", "/home/b/Pictures/gemini")
PUBLIC_URL = os.environ.get("GEMINI_PUBLIC_URL", "http://localhost:8081").rstrip("/")
_image_dir = Path(IMAGE_DIR)
try:
    _image_dir.mkdir(parents=True, exist_ok=True)
    _SAVE_ENABLED = True
    logger.info(f"Saving generated images to {_image_dir}")
except Exception as e:
    _SAVE_ENABLED = False
    logger.warning(f"Image dir {_image_dir} unavailable ({e}); images will not be saved to disk")

_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
        "image/webp": "webp", "image/gif": "gif"}

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

class ImageGenRequest(BaseModel):
    prompt: str
    model: str = "gemini-browser"
    n: Optional[int] = 1
    size: Optional[str] = None
    response_format: Optional[str] = "b64_json"  # "b64_json" | "url" (data: URL)

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
                    'mat-icon','iron-icon','tp-yt-paper-tooltip',
                    'thinking-overlay','model-thoughts'  // Gemini's "thinking" summary
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
                            // skip screen-reader-only nodes (e.g. the "Gemini said" h2)
                            const cls = (node.getAttribute && node.getAttribute('class')) || '';
                            if (/cdk-visually-hidden|screen-reader/.test(cls)) continue;
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
# Generated-image helpers
# ---------------------------------------------------------------------------
# Status of AI-generated images in the last response: how many have rendered
# (loaded), how many are still blank (pending), and whether the "Creating your
# image…" placeholder text is showing.
_IMG_STATUS_JS = """
(function(){
  function deep(root,sel,out){if(!root)return;root.querySelectorAll(sel).forEach(e=>out.push(e));root.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)deep(e.shadowRoot,sel,out);});}
  const rs=[];deep(document,'model-response',rs);
  const last=rs[rs.length-1];
  if(!last) return JSON.stringify({loaded:0,pending:0,creating:false});
  const root=last.shadowRoot||last;
  const imgs=[];deep(root,'img',imgs);
  let loaded=0,pending=0;
  imgs.forEach(im=>{const ai=(im.alt||'').toLowerCase().includes('generated');if(!ai)return;if(im.naturalWidth>64)loaded++;else pending++;});
  const txt=(last.innerText||last.textContent||'');
  const creating=/creating your image|generating image|creating image/i.test(txt);
  return JSON.stringify({loaded:loaded,pending:pending,creating:creating});
})()
"""

# Read each rendered AI image (a blob: URL) into base64 from inside the page —
# blob: URLs can't be fetched over HTTP from outside the browser.
_GET_IMAGES_JS = """
(async function(){
  function deep(root,sel,out){if(!root)return;root.querySelectorAll(sel).forEach(e=>out.push(e));root.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)deep(e.shadowRoot,sel,out);});}
  const rs=[];deep(document,'model-response',rs);
  const last=rs[rs.length-1]; if(!last) return '[]';
  const root=last.shadowRoot||last;
  const imgs=[];deep(root,'img',imgs);
  const seen=new Set(); const out=[];
  for(const im of imgs){
    const ai=(im.alt||'').toLowerCase().includes('generated');
    if(!ai||im.naturalWidth<=64) continue;
    const src=im.currentSrc||im.src; if(!src||seen.has(src)) continue; seen.add(src);
    try{
      const r=await fetch(src); const b=await r.blob(); const buf=await b.arrayBuffer();
      const by=new Uint8Array(buf); let s=''; const CH=0x8000;
      for(let i=0;i<by.length;i+=CH){ s+=String.fromCharCode.apply(null, by.subarray(i,i+CH)); }
      out.push({mime:b.type||'image/jpeg', b64:btoa(s), alt:(im.alt||'').replace(/^[,\\s]+/,'').trim()});
    }catch(e){ /* skip unreadable image */ }
  }
  return JSON.stringify(out);
})()
"""

_PLACEHOLDER_RE = re.compile(r'^(creating|generating)\b', re.I)


def _is_placeholder(text: str) -> bool:
    """True for the transient 'Creating your image…' loading text."""
    return bool(text) and bool(_PLACEHOLDER_RE.match(text.strip()))


async def _image_status(page) -> dict:
    try:
        raw = await page.evaluate(_IMG_STATUS_JS)
        if isinstance(raw, str):
            return json.loads(raw)
    except Exception:
        pass
    return {"loaded": 0, "pending": 0, "creating": False}


async def _get_images(page) -> list[dict]:
    """Return [{mime, b64, alt}] for AI-generated images in the last response."""
    try:
        raw = await page.evaluate(_GET_IMAGES_JS, await_promise=True, return_by_value=True)
        if isinstance(raw, str):
            return [d for d in json.loads(raw) if d.get("b64")]
    except Exception as e:
        logger.warning(f"image extraction failed: {e}")
    return []


def _persist(im: dict) -> dict:
    """Write an extracted image to GEMINI_IMAGE_DIR, adding 'path' and 'url'."""
    if not _SAVE_ENABLED:
        return im
    try:
        ext = _EXT.get(im.get("mime", "image/jpeg"), "jpg")
        fname = f"gemini_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        fpath = _image_dir / fname
        fpath.write_bytes(base64.b64decode(im["b64"]))
        im["path"] = str(fpath)
        im["url"] = f"{PUBLIC_URL}/images/{fname}"
        logger.info(f"saved image -> {fpath}")
    except Exception as e:
        logger.warning(f"failed to save image: {e}")
    return im


def _img_markdown(im: dict) -> str:
    alt = im.get("alt") or "generated image"
    # Prefer the served URL (small); fall back to an inline data URL.
    src = im.get("url") or f"data:{im['mime']};base64,{im['b64']}"
    return f"\n\n![{alt}]({src})"


def _compose(text: str, imgs: list[dict]) -> str:
    """Build chat message content. When images were generated we return just the
    image markdown — Gemini's accompanying text for image prompts is internal
    'thinking' chrome, not a caption. Plain text otherwise."""
    if imgs:
        return "".join(_img_markdown(im) for im in imgs).strip()
    return text


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


async def _open_and_send(browser, prompt: str):
    """Open a fresh Gemini chat, type the prompt, and submit. Returns (page, monitor)."""
    page = await browser.get("https://gemini.google.com/app")
    monitor = StreamMonitor()
    monitor.attach(page)

    logger.info("Waiting for Gemini to load...")
    await asyncio.sleep(6)

    input_selector = 'div[contenteditable="true"]'
    input_box = await page.select(input_selector, timeout=20)
    if not input_box:
        raise RuntimeError("Gemini input box not found.")

    logger.info(f"Sending prompt ({len(prompt)} chars)...")
    await input_box.send_keys(prompt)
    await page.evaluate(f"""
        const el = document.querySelector('{input_selector}');
        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    """)
    await asyncio.sleep(1.2)

    try:
        btn = await page.select('button[aria-label="Send message"]', timeout=5)
        await btn.click()
    except Exception:
        await input_box.send_keys("\n")

    return page, monitor


async def _stream_completion(page, monitor) -> AsyncGenerator[str, None]:
    """
    Poll the response, yielding text deltas as they grow; return when complete.

    Image-aware: while Gemini is generating an image it shows a "Creating your
    image…" placeholder and the text stream closes early. We suppress that
    placeholder and keep waiting until the <img> actually renders, instead of
    returning the loading text as the answer.
    """
    last_len = 0
    last_change = time.monotonic()
    cdp_fired_at: float | None = None
    deadline = time.monotonic() + 300  # image generation can be slow

    while time.monotonic() < deadline:
        await asyncio.sleep(0.8)

        raw = await get_response_text(page)
        img = await _image_status(page)
        img_pending = img["creating"] or img["pending"] > 0
        # Never stream the "Creating your image…" placeholder or the thinking
        # text shown while an image is rendering.
        text = "" if (_is_placeholder(raw) or img_pending) else raw
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

        logger.debug(
            f"poll: text={last_len} silent={silent:.1f}s "
            f"cdp={'y' if cdp_fired_at else 'n'} gen={still_gen} img={img}"
        )

        if still_gen:
            continue

        # An image finished rendering.
        if img["loaded"] > 0 and not img_pending and silent >= 2.5:
            logger.info(f"Done (image). {img['loaded']} image(s), {last_len} text chars")
            return

        # Text-only completion — only when no image is in flight.
        if not img_pending and img["loaded"] == 0:
            if cdp_fired_at and silent >= 5.0 and last_len > 0:
                logger.info(f"Done (CDP). {last_len} chars")
                return
            if last_len > 50 and silent >= 10.0:
                logger.info(f"Done (DOM fallback). {last_len} chars")
                return
        # else: an image is still on its way — keep waiting until it renders.

    logger.warning("completion deadline reached")


async def run_gemini(messages: list[Message]) -> AsyncGenerator[str, None]:
    """Open Gemini, send the prompt, stream text deltas, then append any
    generated images as markdown data-URLs."""
    prompt = _build_prompt(messages)
    browser = await get_browser()
    page, monitor = await _open_and_send(browser, prompt)

    async for delta in _stream_completion(page, monitor):
        yield delta

    for im in await _get_images(page):
        _persist(im)
        logger.info(f"attaching image ({im['mime']}, {len(im['b64'])} b64 chars)")
        yield _img_markdown(im)

    # Leave the tab open — closing or navigating away disrupts the browser


async def drive_once(prompt: str) -> tuple[str, list[dict]]:
    """Non-streaming drive used by the images endpoint: returns (text, images)."""
    browser = await get_browser()
    page, monitor = await _open_and_send(browser, prompt)
    text = ""
    async for delta in _stream_completion(page, monitor):
        text += delta
    imgs = [_persist(im) for im in await _get_images(page)]
    return text, imgs


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

# Serve saved images so responses can return real links (GEMINI_IMAGE_DIR).
if _SAVE_ENABLED:
    app.mount("/images", StaticFiles(directory=str(_image_dir)), name="images")


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
            try:
                text, imgs = await drive_once(_build_prompt(req.messages))
            except Exception as e:
                logger.error(f"run_gemini failed: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))
            full_text = _compose(text, imgs)

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


@app.post("/v1/images/generations")
async def images_generations(req: ImageGenRequest):
    """OpenAI-compatible image generation, backed by Gemini's in-chat image tool."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    async with _request_lock:
        try:
            _text, imgs = await drive_once(req.prompt)
        except Exception as e:
            logger.error(f"image generation failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    if not imgs:
        raise HTTPException(
            status_code=502,
            detail="Gemini did not return an image for this prompt",
        )

    data = []
    for im in imgs:
        entry = {"b64_json": im["b64"]}
        if im.get("url"):
            entry["url"] = im["url"]
        elif req.response_format == "url":
            entry["url"] = f"data:{im['mime']};base64,{im['b64']}"  # not saved to disk
        if im.get("path"):
            entry["path"] = im["path"]
        data.append(entry)

    return {"created": int(time.time()), "data": data}


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
