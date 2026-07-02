"""
OpenAI-compatible API server backed by browser automation.

Providers are selected per-request via the OpenAI ``model`` field:
  - "gemini-browser"   → gemini.google.com   (text + image generation)
  - "chatgpt-browser"  → chatgpt.com          (text + image generation)
An unknown/absent model falls back to DEFAULT_PROVIDER (env, default gemini-browser).

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
          "models": [
            {"id": "gemini-browser",  "name": "Gemini (Browser)"},
            {"id": "chatgpt-browser", "name": "ChatGPT (Browser)"}
          ]
        }
      }
    }
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
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from providers import PROVIDERS, DEFAULT_PROVIDER, get_provider, patch_cdp, CHROME_ARGS

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
# Generated-image storage (shared across providers)
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

# Suppress KeyError from unknown CDP events (e.g. DOM.adoptedStyleSheetsModified).
patch_cdp()

# ---------------------------------------------------------------------------
# Pydantic models (OpenAI wire format)
# ---------------------------------------------------------------------------
class Message(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_PROVIDER
    messages: list[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

class ImageGenRequest(BaseModel):
    prompt: str
    model: str = DEFAULT_PROVIDER
    n: Optional[int] = 1
    size: Optional[str] = None
    response_format: Optional[str] = "b64_json"  # "b64_json" | "url" (data: URL)

# ---------------------------------------------------------------------------
# Browser state — one persistent instance per provider, one request at a time
# per provider (so Gemini and ChatGPT can run concurrently).
# ---------------------------------------------------------------------------
_browsers: dict[str, uc.Browser] = {}
_locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in PROVIDERS}


async def get_browser(provider) -> uc.Browser:
    b = _browsers.get(provider.name)
    if b is None:
        logger.info(f"[{provider.name}] Starting browser (profile {provider.profile_dir})...")
        b = await uc.start(user_data_dir=provider.profile_dir, browser_args=list(CHROME_ARGS))
        _browsers[provider.name] = b
    return b


# ---------------------------------------------------------------------------
# Generated-image helpers (generic)
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(r'^(creating|generating)\b', re.I)


def _is_placeholder(text: str) -> bool:
    """True for transient 'Creating your image…' loading text."""
    return bool(text) and bool(_PLACEHOLDER_RE.match(text.strip()))


def _persist(im: dict) -> dict:
    """Write an extracted image (if it has inline base64) to GEMINI_IMAGE_DIR,
    adding 'path' and 'url'. Images that are remote-only (e.g. CORS-blocked)
    keep their 'src' and are left untouched."""
    if not im.get("b64") or not _SAVE_ENABLED:
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
    # Prefer the served URL (small), then a remote src, then inline data URL.
    src = im.get("url") or im.get("src")
    if not src and im.get("b64"):
        src = f"data:{im['mime']};base64,{im['b64']}"
    return f"\n\n![{alt}]({src or ''})"


def _compose(text: str, imgs: list[dict], provider) -> str:
    """Build chat message content. For providers whose image-prompt prose is
    just internal 'thinking' (Gemini), return image markdown only. For providers
    where it's a real caption (ChatGPT), keep the text and append the images."""
    if not imgs:
        return text
    md = "".join(_img_markdown(im) for im in imgs).strip()
    if provider.image_text_is_caption and text.strip():
        return (text.strip() + "\n\n" + md).strip()
    return md


# ---------------------------------------------------------------------------
# Core: send prompt → stream response text, then generated images
# ---------------------------------------------------------------------------
def _build_prompt(messages: list[Message]) -> str:
    """
    Flatten the OpenAI messages list into a single prompt. System messages
    become a preamble; multi-turn history is included so agents get context.
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


async def _stream_completion(provider, page, monitor) -> AsyncGenerator[str, None]:
    """
    Poll the response, yielding text deltas as they grow; return when complete.

    Image-aware: while a provider is generating an image it shows a loading
    placeholder and the text stream closes early. We suppress that placeholder
    and keep waiting until the <img> actually renders, instead of returning the
    loading text as the answer.
    """
    last_len = 0
    last_change = time.monotonic()
    cdp_fired_at: float | None = None
    saw_generation = False  # True once we've seen the model actively generating
    saw_creating = False    # True once an image was rendering / rendered
    last_loaded = 0
    loaded_since: float | None = None  # when the rendered-image count last changed
    deadline = time.monotonic() + 420  # image generation can be very slow (esp. free tier)

    while time.monotonic() < deadline:
        await asyncio.sleep(0.8)

        raw = await provider.get_response_text(page)
        img = await provider.image_status(page)
        img_pending = img["creating"] or img["pending"] > 0
        # Never stream the loading placeholder / thinking text shown while an
        # image is rendering.
        text = "" if (_is_placeholder(raw) or img_pending) else raw
        now = time.monotonic()

        if len(text) > last_len:
            chunk = text[last_len:]
            last_len = len(text)
            last_change = now
            yield chunk

        if monitor.stream_done.is_set() and cdp_fired_at is None:
            cdp_fired_at = now
            logger.info(f"[{provider.name}] CDP: stream closed. text so far={last_len}")

        still_gen = await provider.is_generating(page)
        silent = now - last_change

        # Track how long the rendered-image count has been stable.
        if img["loaded"] != last_loaded:
            last_loaded = img["loaded"]
            loaded_since = now
        if img["creating"] or img["loaded"] > 0:
            saw_creating = True  # an image was requested/is rendering

        logger.debug(
            f"[{provider.name}] poll: text={last_len} silent={silent:.1f}s "
            f"cdp={'y' if cdp_fired_at else 'n'} gen={still_gen} img={img}"
        )

        # Image completion: an image has rendered, it's no longer "creating"
        # (canvas/placeholder gone), and the image set has been stable a few
        # seconds. This fires even while the stop button lingers, because
        # ChatGPT keeps it visible while finalizing image variants.
        if (img["loaded"] > 0 and not img["creating"]
                and loaded_since is not None and (now - loaded_since) >= 4.0):
            logger.info(f"[{provider.name}] Done (image). {img['loaded']} image(s), {last_len} text chars")
            return

        if still_gen:
            saw_generation = True
            continue

        # Generation stopped. If an image is still rendering, keep waiting.
        if img_pending:
            continue

        # Text completion: not generating + text settled. The "not generating"
        # signal (stop button gone) is the reliable one — don't require a CDP
        # network signal or a length threshold, since short replies and
        # WebSocket-streamed providers (ChatGPT) would otherwise never complete.
        if last_len > 0 and silent >= 2.5:
            logger.info(f"[{provider.name}] Done. {last_len} chars{' (CDP)' if cdp_fired_at else ''}")
            return
        # Guard: a plain text generation happened but produced no extractable
        # text — return rather than hang. Suppressed once an image is/was in
        # flight, so we never bail during the canvas→<img> commit gap (the image
        # is captured by the stability check above, or by get_images after the
        # deadline as a backstop).
        if last_len == 0 and saw_generation and not saw_creating and silent >= 10.0:
            logger.warning(f"[{provider.name}] Done but no text extracted")
            return

    logger.warning(f"[{provider.name}] completion deadline reached")


async def run_chat(provider, messages: list[Message]) -> AsyncGenerator[str, None]:
    """Open the provider's chat, send the prompt, stream text deltas, then
    append any generated images as markdown links."""
    prompt = _build_prompt(messages)
    browser = await get_browser(provider)
    page, monitor = await provider.open_and_send(browser, prompt)

    async for delta in _stream_completion(provider, page, monitor):
        yield delta

    for im in await provider.get_images(page):
        _persist(im)
        logger.info(f"[{provider.name}] attaching image ({im.get('mime')})")
        yield _img_markdown(im)

    # Leave the tab open — closing or navigating away disrupts the browser.


async def drive_once(provider, prompt: str) -> tuple[str, list[dict]]:
    """Non-streaming drive used by non-streaming chat and the images endpoint:
    returns (text, images)."""
    browser = await get_browser(provider)
    page, monitor = await provider.open_and_send(browser, prompt)
    text = ""
    async for delta in _stream_completion(provider, page, monitor):
        text += delta
    imgs = [_persist(im) for im in await provider.get_images(page)]
    return text, imgs


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm only the default provider; others start lazily on first request
    # (so an un-logged-in provider never blocks startup).
    await get_browser(get_provider(DEFAULT_PROVIDER))
    logger.info("Server ready.")
    yield
    for b in _browsers.values():
        try:
            b.stop()
        except Exception:
            pass


app = FastAPI(title="Browser LLM API", lifespan=lifespan)

# Serve saved images so responses can return real links (GEMINI_IMAGE_DIR).
if _SAVE_ENABLED:
    app.mount("/images", StaticFiles(directory=str(_image_dir)), name="images")


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "google" if name.startswith("gemini") else "openai",
            }
            for name in PROVIDERS
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages is empty")

    provider = get_provider(req.model)
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    async with _locks[provider.name]:
        if req.stream:
            # --- Streaming (SSE) ---
            async def event_stream():
                async for chunk in run_chat(provider, req.messages):
                    data = {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": provider.name,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": chunk},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(data)}\n\n"

                done = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": provider.name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(done)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        else:
            # --- Non-streaming ---
            try:
                text, imgs = await drive_once(provider, _build_prompt(req.messages))
            except Exception as e:
                logger.error(f"[{provider.name}] run failed: {e}", exc_info=True)
                raise HTTPException(status_code=500, detail=str(e))
            full_text = _compose(text, imgs, provider)

            return {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": provider.name,
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
    """OpenAI-compatible image generation, backed by the provider's in-chat image tool."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    provider = get_provider(req.model)
    if not provider.supports_images:
        raise HTTPException(status_code=501, detail=f"{provider.name} does not support image generation")

    async with _locks[provider.name]:
        try:
            _text, imgs = await drive_once(provider, req.prompt)
        except Exception as e:
            logger.error(f"[{provider.name}] image generation failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    if not imgs:
        raise HTTPException(
            status_code=502,
            detail=f"{provider.name} did not return an image for this prompt",
        )

    data = []
    for im in imgs:
        entry = {}
        if im.get("b64"):
            entry["b64_json"] = im["b64"]
        if im.get("url"):
            entry["url"] = im["url"]
        elif im.get("src"):
            entry["url"] = im["src"]
        elif req.response_format == "url" and im.get("b64"):
            entry["url"] = f"data:{im['mime']};base64,{im['b64']}"  # not saved to disk
        if im.get("path"):
            entry["path"] = im["path"]
        data.append(entry)

    return {"created": int(time.time()), "data": data}


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
