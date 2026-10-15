"""
OpenAI-compatible API server backed by browser automation.

Providers are selected per-request via the OpenAI ``model`` field:
  - "chatgpt-browser"  → chatgpt.com          (text + image generation; the default)
  - "gemini-browser"   → gemini.google.com    (text; images only where IMAGE_PROVIDERS allows)
An unknown/absent model falls back to DEFAULT_PROVIDER (env, default chatgpt-browser).
ChatGPT's model and power level come from CHATGPT_MODEL / CHATGPT_EFFORT (default
GPT-6 at High) or a request's ``model_variant``.

Endpoints:
  GET  /                      (mini web UI — chat, image gen, gallery, status)
  GET  /widget.js             (embeddable floating chat widget for any LAN page)
  GET  /v1/models
  POST /v1/chat/completions   (streaming + non-streaming; images in AND out)
  POST /v1/images/generations (OpenAI-style image generation; `image` = img2img)
  POST /v1/images/edits       (image + prompt -> image; JSON or multipart)
  GET  /images/<provider>/<file>  (saved images, per-provider subfolder of BROWSER_LLM_IMAGE_DIR)
  GET  /api/status            (per-provider busy/browser/recycle + live telemetry)
  GET  /api/gallery           (list saved images, newest first; ?provider=&limit=)

Usage:
  browser-llm serve           # listens on 127.0.0.1:8081 (BROWSER_LLM_HOST to share)

Example provider entry for an OpenAI-compatible client:
  {
    "models": {
      "providers": {
        "browser-llm": {
          "baseUrl": "http://localhost:8081/v1",
          "apiKey": "local",
          "api": "openai-completions",
          "models": [
            {"id": "chatgpt-browser", "name": "ChatGPT (Browser)"},
            {"id": "gemini-browser",  "name": "Gemini (Browser)"}
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
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional, Union

import nodriver as uc
import websockets.exceptions
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    import httpx  # http(s) attachments are downloaded with it when present
except ImportError:  # without it, urllib does the download
    httpx = None

from .providers import (
    PROVIDERS, DEFAULT_PROVIDER, IMAGE_PROVIDERS, get_provider, patch_cdp, CHROME_ARGS,
    CompletionTracker, RateLimited, UsageLimitReached,
)
from .providers.limits import refusal_from_message
from . import authz, config, design
from ._version import __version__

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
#   BROWSER_LLM_IMAGE_DIR  — base folder for saved images (created if needed).
#                            Before 2.0: GEMINI_IMAGE_DIR or IMAGE_DIR (still read).
#   BROWSER_LLM_PUBLIC_URL — base URL images are served under (for the returned
#                            links). Before 2.0: GEMINI_PUBLIC_URL (still read).
# ---------------------------------------------------------------------------
# expanduser: a .env file and a systemd Environment= line expand nothing, so
# "~/Pictures/browser-llm" would otherwise create a folder named "~".
IMAGE_DIR = os.path.expanduser(config.env("BROWSER_LLM_IMAGE_DIR", "GEMINI_IMAGE_DIR", "IMAGE_DIR",
                                          default="~/Pictures/browser-llm"))
PUBLIC_URL = config.env("BROWSER_LLM_PUBLIC_URL", "GEMINI_PUBLIC_URL",
                        default="http://localhost:8081").rstrip("/")
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
#                              able to upload arbitrary files off this machine)
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
# Access control (see authz.py for the full contract).
#   BROWSER_LLM_API_KEY — when set, non-loopback clients must send this key
#       (Bearer/X-Api-Key) on /v1/* and /api/*. Localhost stays open.
# Forwarding a model to another install (REMOTE_PROVIDERS) was removed in 2.0:
# a second machine calls this one's API directly, with the key.
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
if os.environ.get("REMOTE_PROVIDERS", "").strip():
    logging.getLogger("gemini_server").warning(
        "REMOTE_PROVIDERS is set but forwarding to another install was removed in 2.0; "
        "it is ignored. Call the other install's API directly instead.")

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
    # Not OpenAI: delete the conversation from the account once the answer has
    # been read. For callers whose prompts are tooling — a linter, a judge, an
    # agent's scratch question — and have no business in the user's history.
    # Unset means "whatever BROWSER_LLM_EPHEMERAL says", so an install can make
    # it the default for clients that predate the field.
    ephemeral: Optional[bool] = None
    # Not OpenAI: which of the site's own models and power levels to use for
    # this request, e.g. "instant", "GPT-6/medium" (ChatGPT; see
    # providers/chatgpt.py parse_variant). Unset = CHATGPT_MODEL/CHATGPT_EFFORT.
    model_variant: Optional[str] = None


class ConversationDeleteRequest(BaseModel):
    """Delete these conversations from the provider's account. Destructive on
    someone's real history, hence the same-origin gate on the endpoint."""
    model: Optional[str] = None
    ids: list[str] = []


class GalleryDeleteRequest(BaseModel):
    """Delete saved image files. Each entry is a served path
    ('/images/chatgpt/x.png'), a full URL, or 'provider/file'."""
    files: list[str] = []


class ImageGenRequest(BaseModel):
    prompt: str
    model: str = DEFAULT_PROVIDER
    n: Optional[int] = 1
    # "WxH", "square", "landscape" or "portrait": asked for in words, since the
    # sites' image tools make a few fixed sizes (crop to the exact pixels after).
    size: Optional[str] = None
    # "transparent" asks the image tool for an alpha background (ChatGPT's
    # tool supports it); "opaque"/"auto"/unset add nothing.
    background: Optional[str] = None
    model_variant: Optional[str] = None
    response_format: Optional[str] = "b64_json"  # "b64_json" | "url" (data: URL)
    # Reference image(s) -> image-to-image / edit. Same spec forms as
    # ChatCompletionRequest.images. `image` matches OpenAI's edits field name.
    image: Optional[Union[str, list[str]]] = None
    images: Optional[list[str]] = None
    # A design kind from design.py ("section", "mobile-screen", "icon", ...):
    # the prompt becomes the brief inside that kind's structural rules, and the
    # kind's size and background apply unless given. `direction` adds the art
    # direction for the kind's group; theme / platform / system / copy_space
    # are the same options as `design.py prompt`.
    kind: Optional[str] = None
    direction: Optional[bool] = None
    theme: Optional[str] = None
    platform: Optional[str] = None
    system: Optional[str] = None
    copy_space: Optional[str] = None


_DESIGN_FIELDS = ("kind", "direction", "theme", "platform", "system", "copy_space")


def _design_prompt(prompt: str, fields: dict, n_refs: int, size: Optional[str],
                   background: Optional[str]) -> tuple[str, Optional[str], Optional[str]]:
    """(prompt, size, background) after applying a design ``kind``, or the
    inputs unchanged when there is none. 400 for an unknown kind."""
    kind = str(fields.get("kind") or "").strip().lower()
    if not kind:
        return prompt, size, background
    try:
        d = design.kind_defaults(kind)
        text = design.compose(
            kind, prompt, direction=str(fields.get("direction") or "").lower()
            in ("1", "true", "yes", "on"),
            theme=fields.get("theme"), platform=fields.get("platform"),
            system=fields.get("system"), copy_space=fields.get("copy_space"),
            ref_roles=["user"] * n_refs or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return text, size or d["size"], background or d["background"]

# ---------------------------------------------------------------------------
# Browser state — one persistent instance per provider, one request at a time
# per provider (so Gemini and ChatGPT can run concurrently).
# ---------------------------------------------------------------------------
_browsers: dict[str, uc.Browser] = {}
_locks: dict[str, asyncio.Lock] = {name: asyncio.Lock() for name in PROVIDERS}

# Recycle a provider's browser once it has done this many image gens. On the
# free plan the renderer bloated after 4-5 slow image generations and started
# timing out, hence the old default of 3. On ChatGPT Plus, measured 13 Oct 2026
# over 7 images in one browser (49-90 s each): the chatgpt.com renderer settled
# at 479-548 MB after each image and peaked at 754-813 MB while drawing, with
# no upward trend. 8 keeps a browser within that measured range and saves a
# Chrome restart on most runs.
_RECYCLE_AFTER_IMAGES = int(os.environ.get("BROWSER_RECYCLE_AFTER_IMAGES", "8"))

# Most conversations or image files one delete call may name. A multi-select in
# the UI is a handful of rows; a request for thousands is a mistake or a script
# with a bug, and either way it should not run to completion.
_MAX_BULK_DELETE = 200

# Whether a chat completion that says nothing about it should have its
# conversation deleted afterwards. Off by default: the sessions are a
# person's own account, and silently binning their history would be worse
# than leaving a thread behind. Turn it on for an install whose callers are
# all tooling; a request can still override it either way.
_EPHEMERAL_DEFAULT = os.environ.get("BROWSER_LLM_EPHEMERAL", "0").strip().lower() \
    in ("1", "true", "yes", "on")
_img_gen_count: dict[str, int] = {name: 0 for name in PROVIDERS}


def _note_image_gen(provider) -> None:
    _img_gen_count[provider.name] = _img_gen_count.get(provider.name, 0) + 1


# Lightweight per-provider telemetry for the dashboard's live status panel.
# In-memory only (reset on restart). Latency is wall-clock for the actual browser
# drive (queue-wait excluded), so it reflects model/site speed, not our overhead.
_RECENT_KEPT = 40
_metrics: dict[str, dict] = {
    name: {
        "requests": 0,           # completed requests (chat + image)
        "errors": 0,             # of which failed
        "total_latency": 0.0,    # sum of durations, for the running average
        "last_latency": None,    # most recent duration (s)
        "last_error": None,      # most recent error text (truncated)
        "last_error_at": None,   # epoch seconds
        "last_request_at": None,  # epoch seconds
        # the last requests, oldest first: {"at", "s", "ok"}, for the latency chart
        "recent": deque(maxlen=_RECENT_KEPT),
    }
    for name in PROVIDERS
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
    m["recent"].append({"at": int(time.time()), "s": round(dur, 2), "ok": error is None})
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


def _conv_stamp(conv_id: Optional[str]) -> str:
    """The conversation's fingerprint as it appears in a saved image's filename.

    Deleting a chat has to take that chat's pictures with it, and the client's
    copy of the conversation is not a reliable place to look for them: a chat
    imported from the account carries the site's own image URLs, so a delete
    driven off the message text finds nothing and the files stay behind for
    good. The filename is the one record that survives every route in, so the
    conversation is written into it. Twelve hex characters, which is short
    enough to read and long enough that two conversations here won't collide.
    """
    hexes = re.sub(r"[^0-9a-f]", "", (conv_id or "").lower())
    return hexes[:12]


def _persist(im: dict, provider, conv_id: Optional[str] = None) -> dict:
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
        stamp = _conv_stamp(conv_id)
        mark = f"c{stamp}_" if stamp else ""
        fname = f"{slug}_{int(time.time())}_{mark}{uuid.uuid4().hex[:8]}.{ext}"
        fpath = subdir / fname
        fpath.write_bytes(base64.b64decode(im["b64"]))
        im["path"] = str(fpath)
        im["url"] = f"{PUBLIC_URL}/images/{slug}/{fname}"
        logger.info(f"saved image -> {fpath}")
    except Exception as e:
        logger.warning(f"failed to save image: {e}")
    return im


def _sidecar(path: Path) -> Path:
    """Where the record of what made a saved image lives: next to it, same name
    plus .json. The gallery reads it; deleting the image deletes it too."""
    return path.with_name(path.name + ".json")


def _write_record(imgs: list[dict], record: dict) -> None:
    """Keep the brief, the words sent and who drew it beside each saved image,
    so the gallery can show them later. A failure here never fails the image."""
    for im in imgs:
        if not im.get("path"):
            continue
        try:
            entry = dict(record, created=int(time.time()))
            if im.get("b64"):
                wh = _pixel_size(base64.b64decode(im["b64"]))
                if wh:
                    entry["width"], entry["height"] = wh
            _sidecar(Path(im["path"])).write_text(json.dumps(entry, ensure_ascii=False))
        except Exception as e:
            logger.warning(f"could not write the image record: {e}")


def _read_record(path: Path) -> Optional[dict]:
    try:
        return json.loads(_sidecar(path).read_text())
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _unlink_saved(path: Path) -> None:
    """Delete a saved image and its record. Raises FileNotFoundError if the
    image is already gone, as Path.unlink does."""
    path.unlink()
    try:
        _sidecar(path).unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"could not delete the record of {path.name}: {e}")


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
        # Only needed until the answer shows any sign of life (see "nostart").
        submitted = None if tracker.started else await provider.has_user_turn(page)

        chunk, done = tracker.feed(now, raw, is_gen, img, cdp_done=cdp_done,
                                   submitted=submitted)
        if chunk and not buffered:
            yield chunk

        if done == "nostart":
            # Fail loudly instead of riding the deadline to an empty 200: the
            # request is then counted in /api/status errors and the caller is
            # told what the page looked like.
            state = await provider.page_visibility(page)
            raise RuntimeError(
                f"[{provider.name}] nothing happened on the page "
                f"{now - start:.0f}s after sending: no user turn, no reply, no "
                f"generation (page visibility: {state or 'unreadable'}). A hidden "
                f"page, a changed composer or send button, or a sign-in or "
                f"bot-check wall all look like this; an expired login is only one "
                f"of them."
            )

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


def _ephemeral(req: "ChatCompletionRequest") -> bool:
    """Whether to delete this request's conversation: what it asked for, or the
    install's default when it did not ask."""
    return _EPHEMERAL_DEFAULT if req.ephemeral is None else bool(req.ephemeral)


async def _discard(provider, page) -> None:
    """Delete the conversation an ephemeral request just created. Never raises:
    the answer has already been read, and losing the cleanup is a tidier failure
    than losing the response."""
    try:
        await provider.discard_conversation(page)
    except Exception as e:
        logger.warning(f"[{provider.name}] discard_conversation failed: {e}")


async def _read_conversation_id(provider, page) -> str:
    """Which account-side conversation this drive landed in, or "". Read before
    the images are saved, because the id goes into their filenames."""
    try:
        return await provider.conversation_id(page) or ""
    except Exception as e:
        logger.debug(f"[{provider.name}] conversation id unavailable: {e}")
        return ""


def _note_conversation(meta: Optional[dict], conv_id: str) -> None:
    """Hand the id to the client, so a client keeping its own copy of the chat
    can delete the real thread later. Best effort: a missing id costs the client
    a cascade, not the answer."""
    if meta is not None and conv_id:
        meta["conversation_id"] = conv_id


async def _read_model_used(provider, page, monitor, options: dict) -> dict:
    """Which model answered: the site's own record when the provider can read
    it, else the model the page asked its backend for. Never raises."""
    used: dict = {}
    sent = getattr(monitor, "sent_model", None) or {}
    try:
        record = await provider.answered_model(page) or {}
    except Exception as e:
        logger.debug(f"[{provider.name}] answered_model failed: {e}")
        record = {}
    if record.get("model_slug"):
        used = {"model": record["model_slug"], "effort": record.get("thinking_effort"),
                "resolved": record.get("resolved_model_slug"),
                "source": "conversation record"}
    elif sent.get("model"):
        used = {"model": sent["model"], "effort": sent.get("thinking_effort"),
                "source": "page request"}
    if sent.get("model"):
        used["requested_by_page"] = sent["model"]
    if options.get("picker"):
        used["picker"] = options["picker"]
    if used:
        if used.get("model"):
            effort = f" ({used['effort']})" if used.get("effort") else ""
            logger.info(f"[{provider.name}] answered by {used['model']}{effort} per the "
                        f"{used.get('source')}; picker {used.get('picker', '-')}")
        else:
            # ChatGPT's record of an image made at Instant names no model
            # (13 Oct 2026), and the page's request may not have been seen.
            logger.info(f"[{provider.name}] the page did not say which model answered; "
                        f"picker {used.get('picker', '-')}")
        note = getattr(provider, "note_model_used", None)
        if note:
            note(used)
    return used


async def run_chat(provider, prompt: str, attachments: Optional[list] = None, *,
                   raw_uploads: Optional[list] = None,
                   allow_local_paths: bool = True,
                   ephemeral: bool = False,
                   meta: Optional[dict] = None,
                   variant: Optional[str] = None) -> AsyncGenerator[str, None]:
    """Open the provider's chat, attach any input images, send the prompt, stream
    text deltas, then append any generated images as markdown links.

    ``ephemeral`` deletes the conversation from the account once the answer has
    been read, for callers whose prompts are tooling rather than conversation.
    ``variant`` is the request's model_variant (see Provider.prepare_page)."""
    browser = await get_browser(provider)
    try:
        async with _attachment_files(attachments, raw_uploads,
                                     allow_local_paths=allow_local_paths) as files:
            options = {"kind": "chat", "variant": variant}
            page, monitor = await provider.open_and_send(browser, prompt, attachments=files,
                                                         options=options)

            async for delta in _stream_completion(provider, page, monitor):
                yield delta

            used = await _read_model_used(provider, page, monitor, options)
            if meta is not None and used:
                meta["model_used"] = used
            # Before the images, not after: the id becomes part of their names.
            conv_id = "" if ephemeral else await _read_conversation_id(provider, page)
            n = 0
            for im in await provider.get_images(page):
                _persist(im, provider, conv_id)
                n += 1
                logger.info(f"[{provider.name}] attaching image ({im.get('mime')})")
                yield _img_markdown(im)
            if n:
                _note_image_gen(provider)  # count toward browser recycle
            if ephemeral:
                await _discard(provider, page)
            else:
                _note_conversation(meta, conv_id)
    except Exception as e:
        await _evict_dead_browser(provider, e)
        raise

    # Leave the tab open — closing or navigating away disrupts the browser.


async def drive_once(provider, prompt: str, attachments: Optional[list] = None, *,
                     raw_uploads: Optional[list] = None,
                     allow_local_paths: bool = True,
                     ephemeral: bool = False,
                     meta: Optional[dict] = None,
                     variant: Optional[str] = None,
                     kind: str = "chat") -> tuple[str, list[dict]]:
    """Non-streaming drive used by non-streaming chat and the images endpoints:
    returns (text, images). ``kind`` "image" makes the provider read the
    site's own image counter first (see Provider.prepare_page)."""
    browser = await get_browser(provider)
    try:
        async with _attachment_files(attachments, raw_uploads,
                                     allow_local_paths=allow_local_paths) as files:
            options = {"kind": kind, "variant": variant}
            page, monitor = await provider.open_and_send(browser, prompt, attachments=files,
                                                         options=options)
            if meta is not None and "image_tool" in options:
                meta["image_tool"] = options["image_tool"]
            text = ""
            async for delta in _stream_completion(provider, page, monitor):
                text += delta
            used = await _read_model_used(provider, page, monitor, options)
            if meta is not None and used:
                meta["model_used"] = used
            conv_id = "" if ephemeral else await _read_conversation_id(provider, page)
            imgs = [_persist(im, provider, conv_id)
                    for im in await provider.get_images(page)]
            if imgs:
                _note_image_gen(provider)  # count toward browser recycle
            if ephemeral:
                await _discard(provider, page)
            else:
                _note_conversation(meta, conv_id)
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
# FastAPI app
# ---------------------------------------------------------------------------
_PREWARM = os.environ.get("BROWSER_LLM_PREWARM", "1").strip().lower() \
    not in ("0", "false", "no", "off")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm only the default provider; others start lazily on first request
    # (so an un-logged-in provider never blocks startup). BROWSER_LLM_PREWARM=0
    # starts no browser at all: for tests, docs screenshots and a second
    # instance that must not open Chrome over the service's windows.
    if _PREWARM:
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

# Serve saved images so responses can return real links (BROWSER_LLM_IMAGE_DIR).
if _SAVE_ENABLED:
    app.mount("/images", StaticFiles(directory=str(_image_dir)), name="images")

_STARTED_AT = time.time()
_WEBUI_DIR = Path(__file__).resolve().parent / "webui"
_UI_FILE = _WEBUI_DIR / "index.html"
_WIDGET_FILE = _WEBUI_DIR / "widget.js"
_WIDGET_DEMO_FILE = _WEBUI_DIR / "widget-demo.html"
# Fonts the dashboard and widget ship with, so the UI looks the same on a box
# with no network and no fonts installed. Public, like the pages that use them.
_ASSETS_DIR = _WEBUI_DIR / "assets"
if _ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")


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


@app.get("/api/design/kinds")
async def api_design_kinds():
    """The design kinds (design.py) for the dashboard's presets row: id,
    label, group, default size and background, text policy, one-line about."""
    return {"kinds": design.kinds_catalog(), "direction_groups": sorted(design.DIRECTION)}


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
            # can its account-side conversation list be read and deleted?
            # (the dashboard's import button and cascading delete key off this)
            "supports_history": getattr(p, "supports_history", False),
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
            "recent": list(m["recent"]),
            # may this provider make images here (IMAGE_PROVIDERS)?
            "images_allowed": name in IMAGE_PROVIDERS,
        }
        try:
            providers[name].update(p.status_extra())
        except Exception as e:
            logger.debug(f"[{name}] status_extra failed: {e}")
    return {
        "ok": True,
        "version": __version__,
        "uptime_seconds": int(time.time() - _STARTED_AT),
        "default_provider": DEFAULT_PROVIDER,
        "image_providers": IMAGE_PROVIDERS,
        "image_saving": _SAVE_ENABLED,
        "image_dir": str(_image_dir),
        "display": os.environ.get("DISPLAY", ""),
        "providers": providers,
    }


@app.get("/api/gallery")
async def api_gallery(provider: Optional[str] = None, limit: int = 60, offset: int = 0):
    """Saved generated images, newest first. ``provider`` accepts a slug
    ('gemini') or a model name ('gemini-browser'); absent = all providers.
    ``offset`` pages through them; each item carries ``record`` when the
    image endpoints kept one (the brief, kind, words sent, model, size)."""
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
    offset = max(0, int(offset or 0))
    page = items[offset:offset + limit]
    for it in page:
        rec = _read_record(_image_dir / it["provider"] / it["file"])
        if rec:
            it["record"] = rec
    return {"images": page, "total": len(items), "offset": offset,
            "has_more": offset + len(page) < len(items), "image_dir": str(_image_dir)}


def _trusted_caller(request: Request) -> bool:
    """True when this request did not come from a page on another site.

    CORS is wide open here (the widget embeds anywhere), so any site the
    operator visits can make their browser call this server from 127.0.0.1 and
    read the answer. That is survivable for "ask the model a question"; it is
    not survivable for reading the whole of someone's chat history or deleting
    it, so those endpoints require an Origin that matches the host they were
    addressed to — or no Origin at all (curl, the CLI, the desktop app; a
    browser cannot omit it cross-origin). A keyed LAN client still passes: the
    API-key middleware has already checked its key by this point.
    """
    return authz.origin_is_trusted(request.headers.get("origin"),
                                   request.headers.get("host"))


def _require_trusted(request: Request, what: str) -> None:
    if not _trusted_caller(request):
        origin = request.headers.get("origin") or "an unknown origin"
        logger.warning(f"refused {what} from cross-origin caller {origin}")
        raise HTTPException(
            status_code=403,
            detail=f"{what} is refused for a cross-origin caller ({origin}). "
                   f"Open the dashboard on this server's own address instead.")


def _history_provider(model: Optional[str]):
    """The provider whose account history we are about to touch, or a 4xx/501
    explaining why we can't."""
    provider = get_provider(model)
    if not getattr(provider, "supports_history", False):
        raise HTTPException(
            status_code=501,
            detail=f"{provider.name} exposes no conversation list this server can read")
    return provider


@app.get("/api/history")
async def api_history(request: Request, model: Optional[str] = None,
                      limit: int = 200, offset: int = 0):
    """The provider account's conversation titles, newest first — what the
    dashboard imports so a chat that predates it, or was held in another
    browser, is visible here too.

    Titles only. The bodies come one at a time from /api/conversation/{id},
    because reading every thread up front is exactly what makes the site start
    answering 429. Reads the signed-in session, so it is same-origin only.
    """
    _require_trusted(request, "reading the account's chat history")
    provider = _history_provider(model)
    limit = max(1, min(int(limit or 200), 1000))
    offset = max(0, int(offset or 0))
    async with _locks[provider.name]:
        try:
            browser = await get_browser(provider)
            async with provider.session_tab(browser) as page:
                data = await provider.list_conversations(page, limit=limit,
                                                        offset=offset)
        except Exception as e:
            await _evict_dead_browser(provider, e)
            logger.error(f"[{provider.name}] history read failed: {e}", exc_info=True)
            raise HTTPException(status_code=502, detail=str(e))
    return {"model": provider.name, "limit": limit, "offset": offset, **data}


@app.get("/api/conversation/{conv_id}")
async def api_conversation(conv_id: str, request: Request,
                           model: Optional[str] = None):
    """One conversation's messages, read from the account. The dashboard calls
    this when an imported chat is opened, so a whole account's history is never
    pulled at once."""
    _require_trusted(request, "reading a conversation from the account")
    provider = _history_provider(model)
    async with _locks[provider.name]:
        try:
            browser = await get_browser(provider)
            async with provider.session_tab(browser) as page:
                conv = await provider.fetch_conversation(page, conv_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RateLimited as e:
            # Not a fault: the same call works after a pause, and the client
            # should say so rather than reporting the history as broken.
            raise HTTPException(status_code=429, detail=str(e))
        except Exception as e:
            await _evict_dead_browser(provider, e)
            logger.error(f"[{provider.name}] conversation read failed: {e}", exc_info=True)
            raise HTTPException(status_code=502, detail=str(e))
    return {"model": provider.name, "conversation": conv}


@app.post("/api/conversations/delete")
async def api_conversations_delete(req: ConversationDeleteRequest, request: Request):
    """Delete conversations from the provider's account by id. This is the
    site's own Delete, on someone's real history: same-origin only."""
    _require_trusted(request, "deleting conversations from the account")
    ids = [i.strip() for i in (req.ids or []) if isinstance(i, str) and i.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="ids is empty")
    if len(ids) > _MAX_BULK_DELETE:
        raise HTTPException(
            status_code=400,
            detail=f"too many ids ({len(ids)}); delete at most {_MAX_BULK_DELETE} at a time")
    provider = _history_provider(req.model)
    deleted: list[str] = []
    why: dict = {}
    async with _locks[provider.name]:
        try:
            browser = await get_browser(provider)
            async with provider.session_tab(browser) as page:
                # The whole list in one page call. Per-id calls measured ~4s
                # each, so a 70-chat clear-out held this request open for five
                # minutes and looked to the operator like nothing happened.
                deleted, why = await provider.delete_conversations(page, ids)
        except Exception as e:
            await _evict_dead_browser(provider, e)
            logger.error(f"[{provider.name}] conversation delete failed: {e}", exc_info=True)
            raise HTTPException(status_code=502, detail=str(e))
    # The pictures go with the chat. The client can only name the files it saw
    # itself, which for an imported conversation is none of them, so the sweep
    # happens here where the filenames are.
    images: list[str] = []
    for path in _conversation_images(ids):
        try:
            _unlink_saved(path)
            images.append(path.name)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"could not delete {path}: {e}")
    logger.info(f"[{provider.name}] deleted {len(deleted)}/{len(ids)} conversation(s) "
                f"and {len(images)} of their image(s)")
    return {"model": provider.name, "deleted": deleted,
            "failed": list(why), "why": why, "images_deleted": images}


def _conversation_images(ids: list) -> list[Path]:
    """The saved image files that belong to these conversations.

    Matched on the stamp in the filename (see _conv_stamp), so it works for a
    chat this server never had a local copy of — an imported one, or one from a
    client that kept no record. Images saved before stamping existed carry no
    conversation and are never matched: nothing links them, and guessing from
    timestamps would delete the wrong picture.
    """
    stamps = {s for s in (_conv_stamp(i) for i in ids) if s}
    if not stamps or not _image_dir.exists():
        return []
    exts = {"." + e for e in set(_EXT.values())}
    out = []
    for path in _image_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        if any(f"_c{s}_" in path.name for s in stamps):
            out.append(path)
    return out


def _gallery_target(ref: str) -> Optional[Path]:
    """Resolve a gallery reference to a real file inside IMAGE_DIR, or None.

    This deletes files, so the reference is not trusted: symlinks are resolved
    first, the result must still sit inside the image dir, and it must carry a
    saved-image extension. '..', absolute paths and anything outside get None.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    from urllib.parse import unquote, urlsplit
    if "://" in ref:
        ref = urlsplit(ref).path
    ref = ref.split("?", 1)[0].split("#", 1)[0]
    # Decode before the '..' check, not after: a reference lifted from a URL
    # can spell the traversal as %2E%2E%2F, and saved filenames are provider
    # slug + timestamp + hex, so there is never a literal '%' to protect.
    ref = unquote(ref)
    if ref.startswith("/images/"):
        ref = ref[len("/images/"):]
    ref = ref.lstrip("/")
    if not ref or ".." in ref.split("/"):
        return None
    base = _image_dir.resolve()
    try:
        target = (base / ref).resolve()
    except Exception:
        return None
    if target == base or not target.is_relative_to(base):
        return None
    if target.suffix.lstrip(".").lower() not in set(_EXT.values()):
        return None
    return target


@app.post("/api/gallery/delete")
async def api_gallery_delete(req: GalleryDeleteRequest, request: Request):
    """Delete saved images from disk — what the gallery's own delete calls, and
    what a chat delete calls for the images that chat generated."""
    _require_trusted(request, "deleting saved images")
    refs = [f for f in (req.files or []) if isinstance(f, str) and f.strip()]
    if not refs:
        raise HTTPException(status_code=400, detail="files is empty")
    if len(refs) > _MAX_BULK_DELETE:
        raise HTTPException(
            status_code=400,
            detail=f"too many files ({len(refs)}); delete at most {_MAX_BULK_DELETE} at a time")
    deleted: list[str] = []
    missing: list[str] = []
    refused: list[str] = []
    for ref in refs:
        target = _gallery_target(ref)
        if target is None:
            refused.append(ref)
            continue
        try:
            _unlink_saved(target)
            deleted.append(ref)
        except FileNotFoundError:
            missing.append(ref)
        except Exception as e:
            logger.warning(f"could not delete {target}: {e}")
            refused.append(ref)
    if refused:
        logger.warning(f"gallery delete refused {len(refused)} reference(s)")
    logger.info(f"gallery delete: {len(deleted)} removed, {len(missing)} already gone")
    return {"deleted": deleted, "missing": missing, "refused": refused}


@app.get("/v1/models")
async def list_models():
    names = list(PROVIDERS)
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
    get them by default (the web UI, desktop app and CLI on this machine). A LAN
    client with the API key must send bytes instead — unless the operator opts in
    with ALLOW_REMOTE_FILE_PATHS=1.

    Loopback is necessary but not sufficient: CORS is open so the widget can be
    embedded anywhere, which means a page on ANY site can make the operator's
    browser POST here, arriving from 127.0.0.1. Without the origin check that
    drive-by could name any path on this machine and read back what the model saw."""
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
        def _chunk(content: Optional[str], finish: Optional[str],
                   extra: Optional[dict] = None) -> str:
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
            if extra:
                data.update(extra)
            return f"data: {json.dumps(data)}\n\n"

        async def event_stream():
            started = time.monotonic()
            err: Optional[BaseException] = None
            # Filled by the drive with the account-side conversation id, which
            # rides out on the closing chunk (see _note_conversation).
            meta: dict = {}
            try:
                async with _locks[provider.name]:
                    started = time.monotonic()  # reset: exclude queue-wait
                    async for chunk in run_chat(provider, prompt, specs,
                                                allow_local_paths=allow_paths,
                                                ephemeral=_ephemeral(req),
                                                meta=meta, variant=req.model_variant):
                        yield _chunk(chunk, None)
            except Exception as e:
                # Surface the failure in-band; a raised exception here would
                # just cut the SSE dead with no explanation for the client.
                err = e
                logger.error(f"[{provider.name}] streaming run failed: {e}", exc_info=True)
                yield _chunk(f"\n\n[browser-llm error: {e}]", None)
            finally:
                _record_request(provider.name, started, err)
            extra = {k: meta[k] for k in ("conversation_id", "model_used") if meta.get(k)}
            yield _chunk(None, "stop", extra or None)
            yield "data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # --- Non-streaming ---
    meta: dict = {}
    async with _locks[provider.name]:
        started = time.monotonic()
        try:
            text, imgs = await drive_once(provider, prompt, specs,
                                          allow_local_paths=allow_paths,
                                          ephemeral=_ephemeral(req),
                                          meta=meta, variant=req.model_variant)
        except HTTPException:
            _record_request(provider.name, started, RuntimeError("bad request"))
            raise
        except UsageLimitReached as e:
            _record_request(provider.name, started, e)
            raise _limit_http(e)
        except Exception as e:
            _record_request(provider.name, started, e)
            logger.error(f"[{provider.name}] run failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
        _record_request(provider.name, started)
    full_text = _compose(text, imgs, provider)

    prompt_tokens = sum(len(_message_pieces(m)[0].split()) for m in req.messages)
    body = {
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
    # Not OpenAI: which conversation in the account this answer lives in, so a
    # client that keeps its own copy can delete the real thread with it.
    if meta.get("conversation_id"):
        body["conversation_id"] = meta["conversation_id"]
    # Not OpenAI: which of the site's models answered, from its own record.
    if meta.get("model_used"):
        body["model_used"] = meta["model_used"]
    return body


def _ref_specs(req: ImageGenRequest) -> list[str]:
    """Reference images on an image request (`image` and/or `images`)."""
    specs: list[str] = []
    if isinstance(req.image, str):
        specs.append(req.image)
    elif isinstance(req.image, list):
        specs.extend(s for s in req.image if isinstance(s, str))
    specs.extend(s for s in (req.images or []) if isinstance(s, str))
    return [s for s in specs if s.strip()]


def _pixel_size(data: bytes) -> Optional[tuple[int, int]]:
    """(width, height) read from a PNG, JPEG or WebP header; None if unknown."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            kind = data[12:16]
            if kind == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if kind == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
            if kind == b"VP8L":
                b = int.from_bytes(data[21:25], "little")
                return (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if data[:3] == b"\xff\xd8\xff":
            i = 2
            while i < len(data) - 9:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                              0xCD, 0xCE, 0xCF):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    except Exception:
        return None
    return None


def _image_payload(imgs: list[dict], response_format: Optional[str],
                   model_used: Optional[dict] = None, extra: Optional[dict] = None) -> dict:
    data = []
    for im in imgs:
        entry = {}
        if im.get("b64"):
            entry["b64_json"] = im["b64"]
            # The original as the site delivered it, never resized here.
            try:
                wh = _pixel_size(base64.b64decode(im["b64"]))
            except Exception:
                wh = None
            if wh:
                entry["width"], entry["height"] = wh
            entry["mime"] = im.get("mime")
        if im.get("url"):
            entry["url"] = im["url"]
        elif im.get("src"):
            entry["url"] = im["src"]
        elif response_format == "url" and im.get("b64"):
            entry["url"] = f"data:{im['mime']};base64,{im['b64']}"  # not saved to disk
        if im.get("path"):
            entry["path"] = im["path"]
        if im.get("alt"):
            entry["revised_prompt"] = im["alt"]
        data.append(entry)
    out = {"created": int(time.time()), "data": data}
    if model_used:
        out["model_used"] = model_used
    for k, v in (extra or {}).items():
        if v is not None:
            out[k] = v
    return out


def _limit_http(e: UsageLimitReached) -> HTTPException:
    """HTTP 429 for a site's usage limit, with Retry-After when it said when."""
    secs = e.retry_after()
    logger.warning(f"usage limit ({e.source}): {e}")
    return HTTPException(status_code=429, detail=str(e),
                         headers={"Retry-After": str(secs)} if secs is not None else None)


async def _run_image_request(provider, prompt: str, specs: list, raw: Optional[list],
                             response_format: Optional[str], *, allow_local_paths: bool,
                             what: str = "image generation", size: Optional[str] = None,
                             background: Optional[str] = None,
                             variant: Optional[str] = None,
                             record: Optional[dict] = None):
    """Shared body of /v1/images/generations and /v1/images/edits. ``record``
    is kept beside each saved image (see _write_record)."""
    if not provider.supports_images:
        raise HTTPException(status_code=501,
                            detail=f"{provider.name} does not support image generation")
    if provider.name not in IMAGE_PROVIDERS:
        raise HTTPException(
            status_code=403,
            detail=f"{provider.name} may not make images on this install (IMAGE_PROVIDERS="
                   f"{','.join(IMAGE_PROVIDERS) or 'none'}); use model "
                   f"{IMAGE_PROVIDERS[0] if IMAGE_PROVIDERS else '(none configured)'}")
    _check_upload_support(provider, specs, raw)
    # Fail before queueing behind another request's (possibly minutes-long) drive.
    _prevalidate_specs(specs, allow_local_paths=allow_local_paths)
    typed = provider.image_prompt(prompt, size=size, background=background)

    meta: dict = {}
    async with _locks[provider.name]:
        started = time.monotonic()
        try:
            text, imgs = await drive_once(provider, typed, specs, raw_uploads=raw,
                                          allow_local_paths=allow_local_paths,
                                          meta=meta, variant=variant, kind="image")
        except HTTPException:
            _record_request(provider.name, started, RuntimeError("bad request"))
            raise
        except UsageLimitReached as e:
            _record_request(provider.name, started, e)
            raise _limit_http(e)
        except Exception as e:
            _record_request(provider.name, started, e)
            logger.error(f"[{provider.name}] {what} failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
        _record_request(provider.name, started)

    if not imgs:
        # The site's own refusal ("You've hit the Plus plan limit for image
        # generation ... resets in 23 hours") is a 429 with the reset time, so
        # an agent stops and says so instead of retrying into the same wall.
        refusal = refusal_from_message(text)
        if refusal:
            raise _limit_http(refusal)
        said = f" It said: {text.strip()[:300]!r}" if text and text.strip() else ""
        raise HTTPException(
            status_code=502,
            detail=f"{provider.name} did not return an image for this prompt.{said}",
        )
    _write_record(imgs, dict(record or {}, prompt_sent=typed, size=size,
                             model=provider.name, model_used=meta.get("model_used"),
                             image_tool=meta.get("image_tool")))
    # prompt_sent: the exact words typed into the site, so a caller can see
    # (and a design manifest can keep) what the image was made from.
    # image_tool: whether the composer was in the site's own image mode.
    return _image_payload(imgs, response_format, meta.get("model_used"),
                          {"prompt_sent": typed, "image_tool": meta.get("image_tool")})


@app.post("/v1/images/generations")
async def images_generations(req: ImageGenRequest, request: Request):
    """OpenAI-compatible image generation, backed by the provider's in-chat image
    tool. Passing ``image``/``images`` makes it image-to-image (the reference is
    uploaded into the chat with the prompt)."""
    if not req.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    allow_paths = _client_may_send_paths(request)
    specs = _ref_specs(req)
    dfields = req.model_dump(include=set(_DESIGN_FIELDS))
    prompt, size, background = _design_prompt(req.prompt, dfields, len(specs),
                                              req.size, req.background)
    return await _run_image_request(get_provider(req.model), prompt, specs,
                                    None, req.response_format,
                                    allow_local_paths=allow_paths, size=size,
                                    background=background, variant=req.model_variant,
                                    record={"brief": req.prompt, "kind": dfields.get("kind"),
                                            "references": len(specs)})


@app.post("/v1/images/edits")
async def images_edits(request: Request):
    """Image + prompt → image (OpenAI's ``images.edits``). Accepts the official
    ``multipart/form-data`` upload (fields ``image`` — repeatable — plus
    ``prompt``/``model``/``response_format``/``size``/``background``/
    ``model_variant``) or the same JSON body as /v1/images/generations.
    ``mask`` and ``n`` are accepted and ignored."""
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
        size = str(form.get("size") or "") or None
        background = str(form.get("background") or "") or None
        variant = str(form.get("model_variant") or "") or None
        dfields = {f: (str(form.get(f) or "") or None) for f in _DESIGN_FIELDS}
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
        size, background, variant = req.size, req.background, req.model_variant
        dfields = req.model_dump(include=set(_DESIGN_FIELDS))
        specs = _ref_specs(req)

    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is empty")
    if not (specs or raw):
        raise HTTPException(status_code=400,
                            detail="no image supplied — send `image` (multipart file, "
                                   "data: URL, http(s) URL, or local path)")
    brief = prompt
    prompt, size, background = _design_prompt(prompt, dfields, len(specs) + len(raw),
                                              size, background)

    return await _run_image_request(get_provider(model), prompt, specs, raw, response_format,
                                    allow_local_paths=allow_paths, what="image edit",
                                    size=size, background=background, variant=variant,
                                    record={"brief": brief, "kind": dfields.get("kind"),
                                            "references": len(specs) + len(raw)})


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
