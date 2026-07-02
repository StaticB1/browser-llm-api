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
  GET  /images/<provider>/<file>  (saved images, per-provider subfolder of GEMINI_IMAGE_DIR)

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

from providers import (
    PROVIDERS, DEFAULT_PROVIDER, get_provider, patch_cdp, CHROME_ARGS,
    CompletionTracker,
)

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
# Generated-image storage. Each provider's images go in its own subfolder with
# its own filename prefix (see _persist), so e.g. ChatGPT images are no longer
# mislabeled/saved as "gemini". IMAGE_DIR is the shared base.
#   GEMINI_IMAGE_DIR  — base folder for saved images (created if needed).
#                       (env name kept for back-compat; IMAGE_DIR also accepted)
#   GEMINI_PUBLIC_URL — base URL images are served under (for the returned links)
# ---------------------------------------------------------------------------
IMAGE_DIR = (os.environ.get("GEMINI_IMAGE_DIR") or os.environ.get("IMAGE_DIR")
             or os.path.expanduser("~/Pictures/browser-llm"))
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

# The persistent browser's renderer bloats after a handful of image generations
# (heavy canvas/blobs + growing SPA DOM) and starts timing out; a fresh browser
# resets it. Recycle a provider's browser once it has done this many image gens.
_RECYCLE_AFTER_IMAGES = int(os.environ.get("BROWSER_RECYCLE_AFTER_IMAGES", "3"))
_img_gen_count: dict[str, int] = {name: 0 for name in PROVIDERS}


def _note_image_gen(provider) -> None:
    _img_gen_count[provider.name] = _img_gen_count.get(provider.name, 0) + 1


async def get_browser(provider) -> uc.Browser:
    b = _browsers.get(provider.name)

    # Proactively recycle a browser that has generated enough images to bloat.
    # Called at the start of each (per-provider serialized) request, so the
    # browser is never recycled mid-response.
    if b is not None and _img_gen_count.get(provider.name, 0) >= _RECYCLE_AFTER_IMAGES:
        logger.info(f"[{provider.name}] recycling browser after "
                    f"{_img_gen_count[provider.name]} image gens")
        try:
            b.stop()
        except Exception as e:
            logger.warning(f"[{provider.name}] browser stop during recycle failed: {e}")
        _browsers.pop(provider.name, None)
        _img_gen_count[provider.name] = 0
        b = None
        await asyncio.sleep(2)  # let Chrome release the profile before restart

    if b is None:
        # Clear a stale singleton lock so the fresh start isn't blocked.
        try:
            for f in Path(provider.profile_dir).glob("Singleton*"):
                f.unlink()
        except Exception:
            pass
        logger.info(f"[{provider.name}] Starting browser (profile {provider.profile_dir})...")
        b = await uc.start(user_data_dir=provider.profile_dir, browser_args=list(CHROME_ARGS))
        _browsers[provider.name] = b
    return b


# ---------------------------------------------------------------------------
# Generated-image helpers (generic)
# ---------------------------------------------------------------------------
def _provider_slug(provider) -> str:
    """Short filesystem-safe label for a provider ('chatgpt' / 'gemini'), used to
    name and folder saved images so one provider's images aren't mislabeled under
    another's (the bug: everything was foldered + prefixed 'gemini')."""
    name = (getattr(provider, "name", "") or "provider").replace("-browser", "")
    slug = "".join(c if (c.isalnum() or c in "-_") else "_" for c in name).strip("_-")
    return slug or "provider"


def _persist(im: dict, provider) -> dict:
    """Write an extracted image (if it has inline base64) into a per-provider
    subfolder of IMAGE_DIR, adding 'path' and 'url'. Images that are remote-only
    (e.g. CORS-blocked) keep their 'src' and are left untouched."""
    if not im.get("b64") or not _SAVE_ENABLED:
        return im
    try:
        slug = _provider_slug(provider)
        ext = _EXT.get(im.get("mime", "image/jpeg"), "jpg")
        subdir = _image_dir / slug
        subdir.mkdir(parents=True, exist_ok=True)
        fname = f"{slug}_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        fpath = subdir / fname
        fpath.write_bytes(base64.b64decode(im["b64"]))
        im["path"] = str(fpath)
        im["url"] = f"{PUBLIC_URL}/images/{slug}/{fname}"
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


_BASE_DEADLINE = 420.0   # base ceiling; long image gen on the free tier is slow
_MAX_DEADLINE = 900.0    # hard cap even for an answer that keeps actively streaming


async def _stream_completion(provider, page, monitor) -> AsyncGenerator[str, None]:
    """
    Poll the response, yielding text deltas as they grow; return when complete.

    The completion decision lives in ``CompletionTracker`` (unit-tested). This
    loop only polls the page, feeds samples in, and yields chunks. Long answers
    (e.g. a whole HTML page) that are *still actively streaming* extend the
    deadline up to ``_MAX_DEADLINE`` so they aren't truncated mid-generation,
    while a stalled request still gives up near ``_BASE_DEADLINE``.
    """
    tracker = CompletionTracker()
    start = time.monotonic()
    deadline = start + _BASE_DEADLINE

    while True:
        now = time.monotonic()
        if now >= deadline:
            # Extend only while the answer is plainly still in flight — text
            # still growing, or WebSocket frames still arriving (ChatGPT).
            ws_idle = monitor.seconds_since_ws_frame(now)
            active = (tracker.silent_for(now) < 5.0
                      or (ws_idle is not None and ws_idle < CompletionTracker.WS_ACTIVE_WINDOW))
            if active and deadline < start + _MAX_DEADLINE:
                deadline = min(start + _MAX_DEADLINE, deadline + 120.0)
            else:
                logger.warning(
                    f"[{provider.name}] completion deadline reached "
                    f"({now - start:.0f}s, {tracker.text_len} chars)"
                )
                return

        await asyncio.sleep(0.8)
        now = time.monotonic()

        raw = await provider.get_response_text(page)
        img = await provider.image_status(page)
        is_gen = await provider.is_generating(page)
        cdp_done = monitor.stream_done.is_set()

        chunk, done = tracker.feed(now, raw, is_gen, img, cdp_done=cdp_done)
        if chunk:
            yield chunk

        logger.debug(
            f"[{provider.name}] poll: text={tracker.text_len} "
            f"silent={tracker.silent_for(now):.1f}s cdp={'y' if tracker.cdp_fired else 'n'} "
            f"gen={is_gen} img={img}"
        )

        if done:
            logger.info(
                f"[{provider.name}] Done ({done}). {tracker.text_len} chars"
                f"{' (CDP)' if tracker.cdp_fired else ''}"
            )
            return


async def run_chat(provider, messages: list[Message]) -> AsyncGenerator[str, None]:
    """Open the provider's chat, send the prompt, stream text deltas, then
    append any generated images as markdown links."""
    prompt = _build_prompt(messages)
    browser = await get_browser(provider)
    page, monitor = await provider.open_and_send(browser, prompt)

    async for delta in _stream_completion(provider, page, monitor):
        yield delta

    n = 0
    for im in await provider.get_images(page):
        _persist(im, provider)
        n += 1
        logger.info(f"[{provider.name}] attaching image ({im.get('mime')})")
        yield _img_markdown(im)
    if n:
        _note_image_gen(provider)  # count toward browser recycle

    # Leave the tab open — closing or navigating away disrupts the browser.


async def drive_once(provider, prompt: str) -> tuple[str, list[dict]]:
    """Non-streaming drive used by non-streaming chat and the images endpoint:
    returns (text, images)."""
    browser = await get_browser(provider)
    page, monitor = await provider.open_and_send(browser, prompt)
    text = ""
    async for delta in _stream_completion(provider, page, monitor):
        text += delta
    imgs = [_persist(im, provider) for im in await provider.get_images(page)]
    if imgs:
        _note_image_gen(provider)  # count toward browser recycle
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
