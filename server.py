"""
OpenAI-compatible API server backed by browser automation.

Providers are selected per-request via the OpenAI ``model`` field:
  - "gemini-browser"   → gemini.google.com   (text + image generation)
  - "chatgpt-browser"  → chatgpt.com          (text + image generation)
An unknown/absent model falls back to DEFAULT_PROVIDER (env, default gemini-browser).

Endpoints:
  GET  /                      (mini web UI — chat, image gen, gallery, status)
  GET  /widget.js             (embeddable floating chat widget for any LAN page)
  GET  /v1/models
  POST /v1/chat/completions   (streaming + non-streaming; images in AND out)
  POST /v1/images/generations (OpenAI-style image generation; `image` = img2img)
  POST /v1/images/edits       (image + prompt -> image; JSON or multipart)
  GET  /images/<provider>/<file>  (saved images, per-provider subfolder of GEMINI_IMAGE_DIR)
  GET  /api/status            (per-provider busy/browser/recycle + live telemetry)
  GET  /api/gallery           (list saved images, newest first; ?provider=&limit=)

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
import shutil
import tempfile
import time
import uuid
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional, Union

import nodriver as uc
import websockets.exceptions
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    import httpx  # only needed when REMOTE_PROVIDERS is configured
except ImportError:  # older venv without httpx: local providers still work
    httpx = None

from providers import (
    PROVIDERS, DEFAULT_PROVIDER, get_provider, patch_cdp, CHROME_ARGS,
    CompletionTracker,
)
import authz
from _version import __version__

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

# ---------------------------------------------------------------------------
# Image INPUT (attachments). A request can hand us an image as a data: URL,
# bare base64, an http(s) URL, a local file path, or multipart upload bytes;
# the browser's file picker needs a real file on disk, so specs are materialized
# into a temp dir for the duration of the drive and deleted afterwards.
#   MAX_ATTACHMENTS          — most files one request may attach
#   MAX_ATTACHMENT_MB        — per-file size ceiling
#   ALLOW_REMOTE_FILE_PATHS  — let NON-loopback clients attach server-side file
#                              paths (off by default: a LAN client shouldn't be
#                              able to upload arbitrary files off this box)
# ---------------------------------------------------------------------------
_MAX_ATTACHMENTS = max(1, int(os.environ.get("MAX_ATTACHMENTS", "6")))
_MAX_ATTACHMENT_BYTES = int(float(os.environ.get("MAX_ATTACHMENT_MB", "20")) * 1024 * 1024)
_ALLOW_REMOTE_FILE_PATHS = (os.environ.get("ALLOW_REMOTE_FILE_PATHS", "").strip().lower()
                            in ("1", "true", "yes", "on"))

_DATA_URL_RE = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?((?:;[\w.+-]+=?[\w.+-]*)*),(.*)$",
                          re.I | re.S)
_B64_RE = re.compile(r"^[A-Za-z0-9+/\s]+={0,2}$")
# Image signatures, so a spec that carries no mime/extension still gets a name
# the site's file input will accept.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
    (b"\x00\x00\x01\x00", "ico"),
)


def _sniff_ext(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data.lstrip()[:5] == b"<?xml" or data.lstrip()[:4] == b"<svg":
        return "svg"
    return ""


def _spec_kind(spec: str) -> str:
    """How to turn an attachment spec into bytes: 'data' | 'http' | 'path'."""
    s = (spec or "").strip()
    low = s[:8].lower()
    if low.startswith("data:"):
        return "data"
    if low.startswith(("http://", "https:/")):
        return "http"
    if low.startswith("file://"):
        return "path"
    # Long, base64-looking and not a plausible path → treat as raw base64.
    if len(s) > 256 and "/" not in s[:1] and "~" not in s[:1] and _B64_RE.match(s):
        return "data"
    return "path"


def _decode_data_spec(spec: str) -> tuple[bytes, str]:
    """(bytes, extension) for a data: URL or a bare base64 blob."""
    s = spec.strip()
    declared = ""
    if s[:5].lower() == "data:":
        m = _DATA_URL_RE.match(s)
        if not m:
            raise HTTPException(status_code=400, detail="malformed data: URL attachment")
        declared, params, payload = (m.group(1) or ""), (m.group(2) or ""), m.group(3)
        if "base64" not in params.lower():
            raise HTTPException(status_code=400,
                                detail="only base64 data: URLs are supported for attachments")
    else:
        payload = s
    try:
        data = base64.b64decode(re.sub(r"\s+", "", payload), validate=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"attachment is not valid base64: {e}")
    if not data:
        raise HTTPException(status_code=400, detail="attachment decoded to zero bytes")
    _check_attachment_size(len(data))
    return data, (_sniff_ext(data) or _EXT.get(declared.lower(), "")
                  or (declared.split("/")[-1] if "/" in declared else "") or "png")


def _check_attachment_size(n: int) -> None:
    if n > _MAX_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"attachment is {n / 1e6:.1f} MB; the limit is "
                   f"{_MAX_ATTACHMENT_BYTES / 1e6:.0f} MB (raise MAX_ATTACHMENT_MB)")


async def _download_attachment(url: str) -> bytes:
    """Fetch an http(s) attachment, capped at MAX_ATTACHMENT_MB."""
    if httpx is not None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10)) as client:
                async with client.stream("GET", url, follow_redirects=True) as r:
                    if r.status_code >= 400:
                        raise HTTPException(status_code=400,
                                            detail=f"attachment URL returned {r.status_code}: {url}")
                    buf = bytearray()
                    async for chunk in r.aiter_bytes():
                        buf += chunk
                        _check_attachment_size(len(buf))
                    return bytes(buf)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"could not fetch attachment {url}: {e}")

    def _get() -> bytes:
        import urllib.request
        with urllib.request.urlopen(url, timeout=60) as resp:
            return resp.read(_MAX_ATTACHMENT_BYTES + 1)
    try:
        data = await asyncio.to_thread(_get)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"could not fetch attachment {url}: {e}")
    _check_attachment_size(len(data))
    return data


def _resolve_local_attachment(spec: str, *, allow_local_paths: bool) -> Path:
    if not allow_local_paths:
        raise HTTPException(
            status_code=400,
            detail="file-path attachments are only accepted from a local, same-origin "
                   "caller; send the image as a data: URL (or set "
                   "ALLOW_REMOTE_FILE_PATHS=1 on the server)")
    raw = spec.strip()
    if raw[:7].lower() == "file://":
        from urllib.parse import unquote, urlparse
        raw = unquote(urlparse(raw).path)
    p = Path(raw).expanduser()
    if not p.is_file():
        raise HTTPException(status_code=400, detail=f"attachment file not found: {spec}")
    _check_attachment_size(p.stat().st_size)
    return p.resolve()


@asynccontextmanager
async def _attachment_files(specs: Optional[list], raw: Optional[list] = None, *,
                            allow_local_paths: bool = True):
    """Materialize attachment specs (and raw ``(filename, bytes)`` uploads) into
    real files for the browser's file input. Temp files are deleted on exit;
    caller-supplied local paths are used in place and never touched."""
    specs = [s for s in (specs or []) if isinstance(s, str) and s.strip()]
    raw = list(raw or [])
    if len(specs) + len(raw) > _MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"too many attachments ({len(specs) + len(raw)}); the limit is "
                   f"{_MAX_ATTACHMENTS} (raise MAX_ATTACHMENTS)")

    tmpdir: Optional[str] = None
    paths: list[str] = []

    def _write(data: bytes, ext: str, name: str = "") -> str:
        nonlocal tmpdir
        if tmpdir is None:
            tmpdir = tempfile.mkdtemp(prefix="browser-llm-input-")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "").stem)[:40] or "attachment"
        fname = f"{stem}_{len(paths) + 1}.{(ext or 'png').lstrip('.')}"
        fpath = Path(tmpdir) / fname
        fpath.write_bytes(data)
        return str(fpath)

    try:
        for name, data in raw:
            _check_attachment_size(len(data))
            if not data:
                raise HTTPException(status_code=400, detail=f"uploaded file {name!r} is empty")
            ext = _sniff_ext(data) or Path(name or "").suffix.lstrip(".") or "png"
            paths.append(_write(data, ext, name))
        for spec in specs:
            kind = _spec_kind(spec)
            if kind == "data":
                data, ext = _decode_data_spec(spec)
                paths.append(_write(data, ext))
            elif kind == "http":
                data = await _download_attachment(spec.strip())
                ext = _sniff_ext(data) or Path(spec.split("?")[0]).suffix.lstrip(".") or "png"
                paths.append(_write(data, ext, Path(spec.split("?")[0]).name))
            else:
                paths.append(str(_resolve_local_attachment(
                    spec, allow_local_paths=allow_local_paths)))
        if paths:
            logger.info(f"attachments ready: {[Path(p).name for p in paths]}")
        yield paths
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

# ---------------------------------------------------------------------------
# Access control + remote upstreams (see authz.py for the full contract).
#   BROWSER_LLM_API_KEY — when set, non-loopback clients must send this key
#       (Bearer/X-Api-Key) on /v1/* and /api/*. Localhost stays open.
#   REMOTE_PROVIDERS    — "model=url[,model=url…]": proxy those models to
#       another browser-llm-api instance instead of a local browser (e.g.
#       forward chatgpt-browser to the one machine with a ChatGPT login).
#   REMOTE_API_KEY      — Bearer key sent on proxied requests (the upstream's
#       BROWSER_LLM_API_KEY).
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
REMOTES = authz.parse_remote_providers(os.environ.get("REMOTE_PROVIDERS"))
REMOTE_API_KEY = os.environ.get("REMOTE_API_KEY", "").strip()
# Upstream drive can legitimately take up to _MAX_DEADLINE (900s); give the
# proxy a little headroom on top so we never cut off a still-working upstream.
_REMOTE_TIMEOUT = 940.0
if REMOTES and httpx is None:
    raise RuntimeError(
        "REMOTE_PROVIDERS is set but httpx is not installed — "
        "run: ./venv/bin/pip install -r requirements.txt"
    )

# Suppress KeyError from unknown CDP events (e.g. DOM.adoptedStyleSheetsModified).
patch_cdp()

# ---------------------------------------------------------------------------
# Pydantic models (OpenAI wire format)
# ---------------------------------------------------------------------------
class ImageURL(BaseModel):
    url: str
    detail: Optional[str] = None


class ContentPart(BaseModel):
    """One part of a multimodal message. Deliberately permissive so the common
    dialects all work: OpenAI (``{"type":"image_url","image_url":{"url":…}}``),
    the Responses-style plain-string ``image_url``, and Anthropic's
    ``{"type":"image","source":{"type":"base64","media_type":…,"data":…}}``."""
    model_config = {"extra": "allow"}

    type: Optional[str] = None
    text: Optional[str] = None
    image_url: Optional[Union[ImageURL, str, dict]] = None
    source: Optional[dict] = None
    image: Optional[str] = None


class Message(BaseModel):
    role: str
    # str for plain text, or a list of content parts for text+images (vision).
    content: Union[str, list[ContentPart], None] = None


class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_PROVIDER
    messages: list[Message]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # Convenience shorthand (not OpenAI): attach these images to the prompt
    # without building content parts. Each item is a data: URL, an http(s) URL,
    # bare base64, or a local file path (localhost callers only).
    images: Optional[list[str]] = None


class ImageGenRequest(BaseModel):
    prompt: str
    model: str = DEFAULT_PROVIDER
    n: Optional[int] = 1
    size: Optional[str] = None
    response_format: Optional[str] = "b64_json"  # "b64_json" | "url" (data: URL)
    # Reference image(s) -> image-to-image / edit. Same spec forms as
    # ChatCompletionRequest.images. `image` matches OpenAI's edits field name.
    image: Optional[Union[str, list[str]]] = None
    images: Optional[list[str]] = None

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


# Lightweight per-provider telemetry for the dashboard's live status panel.
# In-memory only (reset on restart). Latency is wall-clock for the actual browser
# drive (queue-wait excluded), so it reflects model/site speed, not our overhead.
_metrics: dict[str, dict] = {
    name: {
        "requests": 0,           # completed requests (chat + image)
        "errors": 0,             # of which failed
        "total_latency": 0.0,    # sum of durations, for the running average
        "last_latency": None,    # most recent duration (s)
        "last_error": None,      # most recent error text (truncated)
        "last_error_at": None,   # epoch seconds
        "last_request_at": None,  # epoch seconds
    }
    for name in {*PROVIDERS, *REMOTES}  # remote-only models get telemetry too
}


def _record_request(name: str, started: float, error: Optional[BaseException] = None) -> None:
    """Fold one finished request into the provider's telemetry (in-memory).

    ``started`` is a ``time.monotonic()`` stamp taken *after* the per-provider
    lock is acquired, so the recorded latency excludes time spent queued behind
    another in-flight request.
    """
    m = _metrics.get(name)
    if m is None:
        return
    dur = time.monotonic() - started
    m["requests"] += 1
    m["total_latency"] += dur
    m["last_latency"] = round(dur, 2)
    m["last_request_at"] = int(time.time())
    if error is not None:
        m["errors"] += 1
        m["last_error"] = str(error)[:200]
        m["last_error_at"] = int(time.time())


async def _browser_alive(b: uc.Browser) -> bool:
    """Cheap liveness probe for a cached browser: the Chrome process must still
    be running AND its CDP websocket must answer. Catches both an exited Chrome
    and the 2026-07-09 failure mode where the process lived on but the CDP
    connection had silently died — previously the first (or, before the
    eviction fix, every) request after that just failed."""
    try:
        if b.stopped:  # Chrome process has exited
            return False
        # Same call nodriver's own Browser._get_targets makes; Browser.send
        # re-attaches a dropped socket first, so this probes "usable", and
        # can even transparently heal a dropped-but-recoverable connection.
        await asyncio.wait_for(b.send(uc.cdp.target.get_targets()), timeout=5)
        return True
    except Exception:
        return False


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

    # Never hand out a browser whose process or CDP connection is dead —
    # replace it now so THIS request succeeds, instead of failing it once
    # and only recovering on the next one.
    if b is not None and not await _browser_alive(b):
        logger.warning(f"[{provider.name}] cached browser is dead (process or CDP "
                       f"connection); starting a fresh one")
        try:
            b.stop()
        except Exception:
            pass
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


def _is_dead_transport(exc: BaseException) -> bool:
    """True if `exc` indicates the CDP connection/browser process itself died
    (as opposed to an ordinary in-page failure like 'no image produced'). A
    provider that hits this must not be reused for the next request — it will
    just fail identically until the process is restarted (see 2026-07-09
    incident: one dead ChatGPT connection silently broke every request for the
    rest of the day)."""
    if isinstance(exc, (websockets.exceptions.ConnectionClosed, ConnectionError, OSError)):
        return True
    # nodriver's Connection.send raises RuntimeError("WebSocket is not connected")
    # once the socket is gone — same corpse, different wrapper.
    if isinstance(exc, RuntimeError) and "websocket" in str(exc).lower():
        return True
    return False


async def _evict_dead_browser(provider, exc: BaseException) -> None:
    """Drop a provider's cached browser after a transport-level failure so the
    *next* request starts a fresh one instead of retrying against a corpse."""
    if not _is_dead_transport(exc):
        return
    b = _browsers.pop(provider.name, None)
    _img_gen_count[provider.name] = 0  # count tracked THAT browser's bloat, not the next one's
    if b is not None:
        logger.warning(f"[{provider.name}] browser connection died ({exc!r}); "
                       f"evicting so the next request starts fresh")
        try:
            b.stop()
        except Exception:
            pass


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
def _message_pieces(m: Message) -> tuple[str, list[str]]:
    """Split one message into (text, image specs). Plain-string content has no
    images; a content-part list is walked for text and every image dialect we
    accept (see ContentPart)."""
    if m.content is None:
        return "", []
    if isinstance(m.content, str):
        return m.content, []

    texts: list[str] = []
    specs: list[str] = []
    for part in m.content:
        if isinstance(part, str):
            if part.strip():
                texts.append(part)
            continue
        if part.text:
            texts.append(part.text)

        spec = None
        iu = part.image_url
        if isinstance(iu, ImageURL):
            spec = iu.url
        elif isinstance(iu, str):
            spec = iu
        elif isinstance(iu, dict):
            spec = iu.get("url")
        if not spec and isinstance(part.image, str):
            spec = part.image
        if not spec and isinstance(part.source, dict):
            src = part.source
            data = src.get("data")
            if isinstance(data, str) and data:
                mt = src.get("media_type") or "image/png"
                spec = data if data[:5].lower() == "data:" else f"data:{mt};base64,{data}"
            elif isinstance(src.get("url"), str):
                spec = src["url"]

        if isinstance(spec, str) and spec.strip():
            specs.append(spec.strip())
        elif (part.type or "").lower() in ("image_url", "image", "input_image"):
            logger.warning(f"content part typed {part.type!r} carried no usable image")

    return "\n".join(t for t in texts if t).strip(), specs


def _build_prompt(messages: list[Message]) -> tuple[str, list[str]]:
    """
    Flatten the OpenAI messages list into a single prompt plus the list of image
    attachments found in it. System messages become a preamble; multi-turn
    history is included so agents get context.

    Every attached image goes into the one composer message we send, so a turn
    that carried images is annotated — otherwise a multi-turn transcript gives
    the model no way to tell which image belonged to which turn.
    """
    system: list[str] = []
    turns: list[tuple[Message, str, list[str]]] = []
    specs: list[str] = []
    for m in messages:
        text, imgs = _message_pieces(m)
        specs.extend(imgs)
        if m.role == "system":
            system.append(text)
        else:
            turns.append((m, text, imgs))

    def _annotate(text: str, imgs: list[str], multi: bool) -> str:
        if not imgs or not multi:
            return text
        note = f"[{len(imgs)} attached image{'s' if len(imgs) > 1 else ''}]"
        return f"{text} {note}".strip()

    parts = []
    if any(s for s in system):
        parts.append("[Context/Instructions: " + " ".join(s for s in system if s) + "]")

    multi = len(turns) > 1
    if len(turns) == 1:
        parts.append(turns[0][1])
    else:
        for m, text, imgs in turns:
            label = "User" if m.role == "user" else "Assistant"
            parts.append(f"{label}: {_annotate(text, imgs, multi)}")

    if len(specs) > _MAX_ATTACHMENTS:
        # Keep the most recent images — those belong to the live turn.
        logger.warning(f"{len(specs)} images in this conversation; sending the last "
                       f"{_MAX_ATTACHMENTS} (MAX_ATTACHMENTS)")
        specs = specs[-_MAX_ATTACHMENTS:]

    return "\n\n".join(p for p in parts if p), specs


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
    # Some providers' extracted text reshapes near the end (see
    # Provider.buffered_stream); for those we suppress incremental deltas and
    # emit the final authoritative text once, so append-only SSE stays correct.
    buffered = getattr(provider, "buffered_stream", False)

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
                if buffered and tracker.text:
                    yield tracker.text
                return

        await asyncio.sleep(0.8)
        now = time.monotonic()

        raw = await provider.get_response_text(page)
        img = await provider.image_status(page)
        is_gen = await provider.is_generating(page)
        cdp_done = monitor.stream_done.is_set()

        chunk, done = tracker.feed(now, raw, is_gen, img, cdp_done=cdp_done)
        if chunk and not buffered:
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
            if buffered and tracker.text:
                yield tracker.text
            return


async def run_chat(provider, prompt: str, attachments: Optional[list] = None, *,
                   raw_uploads: Optional[list] = None,
                   allow_local_paths: bool = True) -> AsyncGenerator[str, None]:
    """Open the provider's chat, attach any input images, send the prompt, stream
    text deltas, then append any generated images as markdown links."""
    browser = await get_browser(provider)
    try:
        async with _attachment_files(attachments, raw_uploads,
                                     allow_local_paths=allow_local_paths) as files:
            page, monitor = await provider.open_and_send(browser, prompt, attachments=files)

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
    except Exception as e:
        await _evict_dead_browser(provider, e)
        raise

    # Leave the tab open — closing or navigating away disrupts the browser.


async def drive_once(provider, prompt: str, attachments: Optional[list] = None, *,
                     raw_uploads: Optional[list] = None,
                     allow_local_paths: bool = True) -> tuple[str, list[dict]]:
    """Non-streaming drive used by non-streaming chat and the images endpoints:
    returns (text, images)."""
    browser = await get_browser(provider)
    try:
        async with _attachment_files(attachments, raw_uploads,
                                     allow_local_paths=allow_local_paths) as files:
            page, monitor = await provider.open_and_send(browser, prompt, attachments=files)
            text = ""
            async for delta in _stream_completion(provider, page, monitor):
                text += delta
            imgs = [_persist(im, provider) for im in await provider.get_images(page)]
            if imgs:
                _note_image_gen(provider)  # count toward browser recycle
            return text, imgs
    except Exception as e:
        await _evict_dead_browser(provider, e)
        raise


def _prevalidate_specs(specs: list, *, allow_local_paths: bool) -> None:
    """Catch the cheap attachment mistakes *before* a streaming response starts,
    so the client gets a real 4xx instead of an in-band error chunk. Only checks
    that cost nothing (count, and local paths: policy + existence + size);
    decoding/downloading still happens during the drive."""
    if len(specs) > _MAX_ATTACHMENTS:
        raise HTTPException(
            status_code=400,
            detail=f"too many attachments ({len(specs)}); the limit is "
                   f"{_MAX_ATTACHMENTS} (raise MAX_ATTACHMENTS)")
    for spec in specs:
        if _spec_kind(spec) == "path":
            _resolve_local_attachment(spec, allow_local_paths=allow_local_paths)


def _check_upload_support(provider, specs: list, raw: Optional[list] = None) -> None:
    """Fail fast (and clearly) when a request carries images but the target
    provider can't take them."""
    if not (specs or raw):
        return
    if not getattr(provider, "supports_upload", False):
        raise HTTPException(
            status_code=400,
            detail=f"{provider.name} does not support image input (attachments)")


# ---------------------------------------------------------------------------
# Remote upstream proxying (REMOTE_PROVIDERS): requests for a mapped model are
# forwarded verbatim to another browser-llm-api instance — no local browser.
# A remote mapping OVERRIDES the local provider of the same name (that's the
# point: this install has the code for chatgpt-browser but no login).
# ---------------------------------------------------------------------------
def _resolve_model(model: Optional[str]) -> str:
    """The model name a request actually routes to: a known local provider or
    remote mapping wins; anything else falls back to DEFAULT_PROVIDER (same
    fallback rule as get_provider, but remote-aware)."""
    if model and (model in REMOTES or model in PROVIDERS):
        return model
    return DEFAULT_PROVIDER


def _remote_headers() -> dict:
    h = {"Content-Type": "application/json"}
    if REMOTE_API_KEY:
        h["Authorization"] = f"Bearer {REMOTE_API_KEY}"
    return h


def _remote_timeout():
    return httpx.Timeout(_REMOTE_TIMEOUT, connect=15)


_MIME_BY_EXT = {v: k for k, v in _EXT.items()}


async def _spec_to_data_url(spec: str, *, allow_local_paths: bool) -> str:
    """Turn a local-path attachment spec into a data: URL. A file path is
    meaningless on the upstream machine, so proxied requests must carry bytes.
    data:/http(s) specs are already portable and pass through untouched."""
    if _spec_kind(spec) != "path":
        return spec
    p = _resolve_local_attachment(spec, allow_local_paths=allow_local_paths)
    data = await asyncio.to_thread(p.read_bytes)
    _check_attachment_size(len(data))
    ext = (_sniff_ext(data) or p.suffix.lstrip(".")).lower()
    mime = _MIME_BY_EXT.get(ext, "image/png" if ext != "svg" else "image/svg+xml")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def _inline_paths_for_remote(payload: dict, *, allow_local_paths: bool) -> dict:
    """Rewrite every local-path attachment in an outgoing proxied payload into a
    data: URL, in the shapes this API accepts (top-level images/image, plus
    content parts inside messages)."""
    async def fix(v):
        return await _spec_to_data_url(v, allow_local_paths=allow_local_paths) \
            if isinstance(v, str) and v.strip() else v

    for key in ("images", "image"):
        v = payload.get(key)
        if isinstance(v, str):
            payload[key] = await fix(v)
        elif isinstance(v, list):
            payload[key] = [await fix(s) for s in v]

    for m in payload.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            iu = part.get("image_url")
            if isinstance(iu, str):
                part["image_url"] = await fix(iu)
            elif isinstance(iu, dict) and isinstance(iu.get("url"), str):
                iu["url"] = await fix(iu["url"])
            if isinstance(part.get("image"), str):
                part["image"] = await fix(part["image"])
            src = part.get("source")
            if isinstance(src, dict) and isinstance(src.get("url"), str):
                src["url"] = await fix(src["url"])
    return payload


async def _proxy_chat(name: str, url: str, req: ChatCompletionRequest, *,
                      allow_local_paths: bool = True):
    """Forward a chat completion to the upstream instance. Streaming responses
    are relayed byte-for-byte (the upstream already speaks correct SSE);
    failures are surfaced in-band as a chunk, matching local behavior."""
    payload = req.model_dump(exclude_none=True)
    payload["model"] = name
    payload = await _inline_paths_for_remote(payload, allow_local_paths=allow_local_paths)

    if req.stream:
        async def relay():
            started = time.monotonic()
            err: Optional[BaseException] = None
            try:
                async with httpx.AsyncClient(timeout=_remote_timeout()) as client:
                    async with client.stream("POST", f"{url}/v1/chat/completions",
                                             json=payload, headers=_remote_headers()) as r:
                        if r.status_code != 200:
                            body = (await r.aread()).decode("utf-8", "ignore")[:300]
                            raise RuntimeError(f"upstream returned {r.status_code}: {body}")
                        async for chunk in r.aiter_bytes():
                            yield chunk
            except Exception as e:
                err = e
                logger.error(f"[{name}] remote proxy ({url}) failed: {e}")
                data = {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": name,
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant",
                                           "content": f"\n\n[browser-llm error: remote: {e}]"},
                                 "finish_reason": None}],
                }
                yield f"data: {json.dumps(data)}\n\n".encode()
                data["choices"][0]["delta"] = {}
                data["choices"][0]["finish_reason"] = "stop"
                yield f"data: {json.dumps(data)}\n\n".encode()
                yield b"data: [DONE]\n\n"
            finally:
                _record_request(name, started, err)
        return StreamingResponse(relay(), media_type="text/event-stream")

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_remote_timeout()) as client:
            r = await client.post(f"{url}/v1/chat/completions",
                                  json=payload, headers=_remote_headers())
    except Exception as e:
        _record_request(name, started, e)
        logger.error(f"[{name}] remote proxy ({url}) failed: {e}")
        raise HTTPException(status_code=502, detail=f"remote provider {url} unreachable: {e}")
    if r.status_code != 200:
        err = RuntimeError(f"upstream returned {r.status_code}")
        _record_request(name, started, err)
        raise HTTPException(status_code=502,
                            detail=f"remote provider returned {r.status_code}: {r.text[:300]}")
    _record_request(name, started)
    return JSONResponse(r.json())


async def _proxy_images(name: str, url: str, req: ImageGenRequest, *,
                        path: str = "/v1/images/generations",
                        allow_local_paths: bool = True):
    """Forward an image generation/edit to the upstream instance. Returned image
    URLs point at the upstream (its GEMINI_PUBLIC_URL); /images/* is public
    there even with an API key set, so the links work in a plain browser."""
    payload = req.model_dump(exclude_none=True)
    payload["model"] = name
    payload = await _inline_paths_for_remote(payload, allow_local_paths=allow_local_paths)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_remote_timeout()) as client:
            r = await client.post(f"{url}{path}",
                                  json=payload, headers=_remote_headers())
    except Exception as e:
        _record_request(name, started, e)
        logger.error(f"[{name}] remote image proxy ({url}) failed: {e}")
        raise HTTPException(status_code=502, detail=f"remote provider {url} unreachable: {e}")
    if r.status_code != 200:
        err = RuntimeError(f"upstream returned {r.status_code}")
        _record_request(name, started, err)
        raise HTTPException(status_code=r.status_code if r.status_code in (501, 502) else 502,
                            detail=f"remote provider returned {r.status_code}: {r.text[:300]}")
    _record_request(name, started)
    return JSONResponse(r.json())


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm only the default provider; others start lazily on first request
    # (so an un-logged-in provider never blocks startup). A remote default has
    # no local browser to warm.
    if DEFAULT_PROVIDER not in REMOTES:
        await get_browser(get_provider(DEFAULT_PROVIDER))
    logger.info("Server ready.")
    yield
    for b in _browsers.values():
        try:
            b.stop()
        except Exception:
            pass


app = FastAPI(title="Browser LLM API", version=__version__, lifespan=lifespan)

# CORS-open so the web UI (and any local tool) can call it from other
# origins/ports. Auth story: localhost is always open; non-loopback clients
# need BROWSER_LLM_API_KEY (if set) on /v1/* and /api/* — see authz.py.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    """When BROWSER_LLM_API_KEY is set, gate API paths for non-loopback
    clients. Loopback (local web UI / desktop app / CLI) stays open, as do
    page/asset paths (needed for served image links and the UI shell).
    Registered after CORSMiddleware → runs before it, so OPTIONS preflights
    (which can't carry auth headers) are exempted in needs_key."""
    client = request.client.host if request.client else None
    if (API_KEY and not authz.is_loopback(client)
            and authz.needs_key(request.url.path, request.method)):
        supplied = authz.extract_key(request.headers.get("authorization"),
                                     request.headers.get("x-api-key"))
        if not authz.key_matches(supplied, API_KEY):
            return JSONResponse(
                {"error": {"message": "missing or invalid API key "
                           "(Authorization: Bearer <key> or X-Api-Key header)",
                           "type": "invalid_request_error", "code": "invalid_api_key"}},
                status_code=401,
            )
    return await call_next(request)

# Serve saved images so responses can return real links (GEMINI_IMAGE_DIR).
if _SAVE_ENABLED:
    app.mount("/images", StaticFiles(directory=str(_image_dir)), name="images")

_STARTED_AT = time.time()
_WEBUI_DIR = Path(__file__).resolve().parent / "webui"
_UI_FILE = _WEBUI_DIR / "index.html"
_WIDGET_FILE = _WEBUI_DIR / "widget.js"
_WIDGET_DEMO_FILE = _WEBUI_DIR / "widget-demo.html"


# ---------------------------------------------------------------------------
# Mini web UI + status/gallery API (used by the UI, handy for scripts too)
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
@app.get("/ui", include_in_schema=False)
async def web_ui():
    if _UI_FILE.exists():
        return FileResponse(_UI_FILE, media_type="text/html")
    return HTMLResponse(
        "<h1>Browser LLM API</h1><p>web UI file missing (webui/index.html) — "
        "the JSON API at <code>/v1/...</code> still works.</p>", status_code=200)


@app.get("/version")
async def version():
    """Package version (also in /api/status)."""
    return {"name": "browser-llm-api", "version": __version__}


@app.get("/demo", include_in_schema=False)
@app.get("/widget-demo", include_in_schema=False)
async def widget_demo():
    """Standalone page demonstrating the embeddable chat widget on a non-dashboard
    page. Handy for testing the widget in isolation."""
    if _WIDGET_DEMO_FILE.exists():
        return FileResponse(_WIDGET_DEMO_FILE, media_type="text/html")
    raise HTTPException(status_code=404, detail="widget-demo.html not found")


@app.get("/widget.js", include_in_schema=False)
async def widget_js():
    """Self-contained embeddable chat widget. Drop into any page on the LAN with
    ``<script src="http://<host>:8081/widget.js"></script>`` — it discovers this
    server as its API base from its own script URL. CORS is already open."""
    if _WIDGET_FILE.exists():
        # no-cache so edits to the widget show up without a hard refresh
        return FileResponse(_WIDGET_FILE, media_type="application/javascript",
                            headers={"Cache-Control": "no-cache"})
    raise HTTPException(status_code=404, detail="widget.js not found")


@app.get("/api/status")
async def api_status():
    """Lightweight server/provider state for the UI's status bar (no browser I/O)."""
    providers = {}
    for name, p in PROVIDERS.items():
        m = _metrics[name]
        b = _browsers.get(name)
        providers[name] = {
            # not just cached — the Chrome process must actually still be alive
            # (b.stopped is a free returncode check, no browser I/O; the deeper
            # CDP-connection probe happens lazily in get_browser per request)
            "browser_running": b is not None and not b.stopped,
            "busy": _locks[name].locked(),
            "supports_images": p.supports_images,
            "supports_upload": getattr(p, "supports_upload", False),
            "images_since_recycle": _img_gen_count.get(name, 0),
            "recycle_after_images": _RECYCLE_AFTER_IMAGES,
            "default": name == DEFAULT_PROVIDER,
            # live telemetry (in-memory, reset on restart)
            "requests": m["requests"],
            "errors": m["errors"],
            "avg_latency": round(m["total_latency"] / m["requests"], 2) if m["requests"] else None,
            "last_latency": m["last_latency"],
            "last_error": m["last_error"],
            "last_error_at": m["last_error_at"],
            "last_request_at": m["last_request_at"],
            # non-None ⇒ requests for this model are proxied upstream, the
            # local browser fields above don't apply
            "remote_upstream": REMOTES.get(name),
        }
    # Remote-only models (mapped in REMOTE_PROVIDERS but not a local provider)
    # still get a status entry so the UI can show them.
    for name, url in REMOTES.items():
        if name in providers:
            continue
        m = _metrics[name]
        providers[name] = {
            "browser_running": False,
            "busy": False,
            "supports_images": True,
            "supports_upload": True,  # upstream decides; assume it can
            "images_since_recycle": 0,
            "recycle_after_images": _RECYCLE_AFTER_IMAGES,
            "default": name == DEFAULT_PROVIDER,
            "requests": m["requests"],
            "errors": m["errors"],
            "avg_latency": round(m["total_latency"] / m["requests"], 2) if m["requests"] else None,
            "last_latency": m["last_latency"],
            "last_error": m["last_error"],
            "last_error_at": m["last_error_at"],
            "last_request_at": m["last_request_at"],
            "remote_upstream": url,
        }
    return {
        "ok": True,
        "version": __version__,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "default_provider": DEFAULT_PROVIDER,
        "image_saving": _SAVE_ENABLED,
        "image_dir": str(_image_dir),
        "display": os.environ.get("DISPLAY", ""),
        "providers": providers,
    }


@app.get("/api/gallery")
async def api_gallery(provider: Optional[str] = None, limit: int = 60):
    """Saved generated images, newest first. ``provider`` accepts a slug
    ('gemini') or a model name ('gemini-browser'); absent = all providers."""
    if not _SAVE_ENABLED:
        return {"images": [], "image_dir": None}
    want = None
    if provider:
        want = provider.replace("-browser", "").strip().lower()
    exts = set(_EXT.values())
    items = []
    try:
        for sub in sorted(_image_dir.iterdir()):
            if not sub.is_dir():
                continue
            slug = sub.name
            if want and slug != want:
                continue
            for f in sub.iterdir():
                if not f.is_file() or f.suffix.lstrip(".").lower() not in exts:
                    continue
                st = f.stat()
                items.append({
                    "provider": slug,
                    "file": f.name,
                    "url": f"/images/{slug}/{f.name}",
                    "bytes": st.st_size,
                    "mtime": int(st.st_mtime),
                })
    except Exception as e:
        logger.warning(f"gallery listing failed: {e}")
    items.sort(key=lambda x: x["mtime"], reverse=True)
    limit = max(1, min(int(limit or 60), 500))
    return {"images": items[:limit], "total": len(items), "image_dir": str(_image_dir)}


@app.get("/v1/models")
async def list_models():
    names = list(PROVIDERS) + [n for n in REMOTES if n not in PROVIDERS]
    return {
        "object": "list",
        "data": [
            {
                "id": name,
                "object": "model",
                "created": 1700000000,
                "owned_by": "google" if name.startswith("gemini") else "openai",
            }
            for name in names
        ],
    }


def _client_may_send_paths(request: Request) -> bool:
    """Local-path attachments are a file-read primitive, so only loopback callers
    get them by default (the web UI, desktop app and CLI on this box). A LAN
    client with the API key must send bytes instead — unless the operator opts in
    with ALLOW_REMOTE_FILE_PATHS=1.

    Loopback is necessary but not sufficient: CORS is open so the widget can be
    embedded anywhere, which means a page on ANY site can make the operator's
    browser POST here, arriving from 127.0.0.1. Without the origin check that
    drive-by could name any path on this box and read back what the model saw."""
    if _ALLOW_REMOTE_FILE_PATHS:
        return True
    client = request.client.host if request.client else None
    if not authz.is_loopback(client):
        return False
    return authz.origin_is_trusted(request.headers.get("origin"),
                                   request.headers.get("host"))


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages is empty")

    allow_paths = _client_may_send_paths(request)
    name = _resolve_model(req.model)
    if name in REMOTES:
        return await _proxy_chat(name, REMOTES[name], req, allow_local_paths=allow_paths)

    provider = get_provider(req.model)
    prompt, specs = _build_prompt(req.messages)
    specs = specs + [s for s in (req.images or []) if isinstance(s, str) and s.strip()]
    _check_upload_support(provider, specs)
    _prevalidate_specs(specs, allow_local_paths=allow_paths)
    if not prompt.strip() and specs:
        prompt = "Describe this image."  # a bare image with no text still needs a prompt
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if req.stream:
        # --- Streaming (SSE) ---
        # The provider lock is taken INSIDE the generator: FastAPI runs the
        # generator after this handler returns, so a lock around the
        # `return StreamingResponse(...)` would be released before the first
        # poll and let concurrent requests fight over one browser tab.
        def _chunk(content: Optional[str], finish: Optional[str]) -> str:
            data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": provider.name,
                "choices": [
                    {
                        "index": 0,
                        "delta": {} if content is None
                                 else {"role": "assistant", "content": content},
                        "finish_reason": finish,
                    }
                ],
            }
            return f"data: {json.dumps(data)}\n\n"

        async def event_stream():
            started = time.monotonic()
            err: Optional[BaseException] = None
            try:
                async with _locks[provider.name]:
                    started = time.monotonic()  # reset: exclude queue-wait
                    async for chunk in run_chat(provider, prompt, specs,
                                                allow_local_paths=allow_paths):
                        yield _chunk(chunk, None)
            except Exception as e:
                # Surface the failure in-band; a raised exception here would
                # just cut the SSE dead with no explanation for the client.
                err = e
                logger.error(f"[{provider.name}] streaming run failed: {e}", exc_info=True)
                yield _chunk(f"\n\n[browser-llm error: {e}]", None)
            finally:
                _record_request(provider.name, started, err)
            yield _chunk(None, "stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # --- Non-streaming ---
    async with _locks[provider.name]:
        started = time.monotonic()
        try:
            text, imgs = await drive_once(provider, prompt, specs,
                                          allow_local_paths=allow_paths)
        except HTTPException:
            _record_request(provider.name, started, RuntimeError("bad request"))
            raise
        except Exception as e:
            _record_request(provider.name, started, e)
            logger.error(f"[{provider.name}] run failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
        _record_request(provider.name, started)
    full_text = _compose(text, imgs, provider)

    prompt_tokens = sum(len(_message_pieces(m)[0].split()) for m in req.messages)
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
            "prompt_tokens": prompt_tokens,
            "completion_tokens": len(full_text.split()),
            "total_tokens": prompt_tokens + len(full_text.split()),
        },
    }


def _ref_specs(req: ImageGenRequest) -> list[str]:
    """Reference images on an image request (`image` and/or `images`)."""
    specs: list[str] = []
    if isinstance(req.image, str):
        specs.append(req.image)
    elif isinstance(req.image, list):
        specs.extend(s for s in req.image if isinstance(s, str))
    specs.extend(s for s in (req.images or []) if isinstance(s, str))
    return [s for s in specs if s.strip()]


def _image_payload(imgs: list[dict], response_format: Optional[str]) -> dict:
    data = []
    for im in imgs:
        entry = {}
        if im.get("b64"):
            entry["b64_json"] = im["b64"]
        if im.get("url"):
            entry["url"] = im["url"]
        elif im.get("src"):
            entry["url"] = im["src"]
        elif response_format == "url" and im.get("b64"):
            entry["url"] = f"data:{im['mime']};base64,{im['b64']}"  # not saved to disk
        if im.get("path"):
            entry["path"] = im["path"]
        data.append(entry)
    return {"created": int(time.time()), "data": data}


async def _run_image_request(provider, prompt: str, specs: list, raw: Optional[list],
                             response_format: Optional[str], *, allow_local_paths: bool,
                             what: str = "image generation"):
    """Shared body of /v1/images/generations and /v1/images/edits."""
    if not provider.supports_images:
        raise HTTPException(status_code=501,
                            detail=f"{provider.name} does not support image generation")
    _check_upload_support(provider, specs, raw)
    # Fail before queueing behind another request's (possibly minutes-long) drive.
    _prevalidate_specs(specs, allow_local_paths=allow_local_paths)

    async with _locks[provider.name]:
        started = time.monotonic()
        try:
            _text, imgs = await drive_once(provider, prompt, specs, raw_uploads=raw,
                                           allow_local_paths=allow_local_paths)
        except HTTPException:
            _record_request(provider.name, started, RuntimeError("bad request"))
            raise
        except Exception as e:
            _record_request(provider.name, started, e)
            logger.error(f"[{provider.name}] {what} failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
        _record_request(provider.name, started)

    if not imgs:
        raise HTTPException(
            status_code=502,
            detail=f"{provider.name} did not return an image for this prompt",
        )
    return _image_payload(imgs, response_format)


@app.post("/v1/images/generations")
async def images_generations(req: ImageGenRequest, request: Request):
    """OpenAI-compatible image generation, backed by the provider's in-chat image
    tool. Passing ``image``/``images`` makes it image-to-image (the reference is
    uploaded into the chat with the prompt)."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    allow_paths = _client_may_send_paths(request)
    name = _resolve_model(req.model)
    if name in REMOTES:
        return await _proxy_images(name, REMOTES[name], req, allow_local_paths=allow_paths)

    return await _run_image_request(get_provider(req.model), req.prompt, _ref_specs(req),
                                    None, req.response_format,
                                    allow_local_paths=allow_paths)


@app.post("/v1/images/edits")
async def images_edits(request: Request):
    """Image + prompt → image (OpenAI's ``images.edits``). Accepts the official
    ``multipart/form-data`` upload (fields ``image`` — repeatable — plus
    ``prompt``/``model``/``response_format``) or the same JSON body as
    /v1/images/generations. ``mask``/``n``/``size`` are accepted and ignored."""
    ctype = (request.headers.get("content-type") or "").lower()
    allow_paths = _client_may_send_paths(request)
    raw: list = []
    specs: list[str] = []

    if ctype.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as e:  # python-multipart missing / malformed body
            hint = ("; install it with: ./venv/bin/python -m pip install python-multipart"
                    if "multipart" in str(e).lower() else
                    " (or send the same fields as JSON)")
            raise HTTPException(status_code=400,
                                detail=f"could not parse multipart body: {e}{hint}")
        prompt = str(form.get("prompt") or "").strip()
        model = str(form.get("model") or DEFAULT_PROVIDER)
        response_format = str(form.get("response_format") or "b64_json")
        for field in ("image", "image[]", "images", "images[]"):
            for item in form.getlist(field):
                if hasattr(item, "read"):  # UploadFile
                    raw.append((getattr(item, "filename", "") or "image.png", await item.read()))
                elif isinstance(item, str) and item.strip():
                    specs.append(item.strip())
    else:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="body must be JSON or multipart/form-data")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="JSON body must be an object")
        try:
            req = ImageGenRequest(**{k: v for k, v in body.items()
                                     if k in ImageGenRequest.model_fields})
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"invalid request: {e}")
        prompt, model, response_format = req.prompt, req.model, req.response_format
        specs = _ref_specs(req)

    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")
    if not (specs or raw):
        raise HTTPException(status_code=400,
                            detail="no image supplied — send `image` (multipart file, "
                                   "data: URL, http(s) URL, or local path)")

    name = _resolve_model(model)
    if name in REMOTES:
        if raw:  # multipart bytes → portable data: URLs for the upstream
            specs = specs + [
                f"data:{_MIME_BY_EXT.get(_sniff_ext(data) or 'png', 'image/png')};base64,"
                f"{base64.b64encode(data).decode()}" for _fname, data in raw]
        proxied = ImageGenRequest(prompt=prompt, model=name, images=specs,
                                  response_format=response_format)
        return await _proxy_images(name, REMOTES[name], proxied,
                                   path="/v1/images/edits", allow_local_paths=allow_paths)

    return await _run_image_request(get_provider(model), prompt, specs, raw, response_format,
                                    allow_local_paths=allow_paths, what="image edit")


# ---------------------------------------------------------------------------
def main():
    """Console entry point (``browser-llm``). Host/port via ``BROWSER_LLM_HOST``
    / ``BROWSER_LLM_PORT`` (defaults 127.0.0.1:8081 — this server drives your
    logged-in accounts, so it stays off the network until you ask for it; set
    ``BROWSER_LLM_HOST=0.0.0.0`` plus ``BROWSER_LLM_API_KEY`` to share it).
    Note: the sites need a non-headless Chrome — on a headless box launch via
    ``serve.sh`` (Xvfb) rather than calling this directly."""
    import uvicorn
    host = os.environ.get("BROWSER_LLM_HOST", "127.0.0.1")
    port = int(os.environ.get("BROWSER_LLM_PORT", "8081"))
    logger.info(f"Browser LLM API v{__version__} → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
