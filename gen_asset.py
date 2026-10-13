#!/usr/bin/env python3
"""
Generate a website image asset via the local browser-LLM image API, then
post-process it (resize / crop / convert / favicon / knockout) into exactly what
the site needs.

ChatGPT is the image provider (default model chatgpt-browser; since 13 Oct 2026
on a Plus account, Eben's order: no images from Gemini). Its image tool makes a
few fixed sizes (1254x1254 square, measured 13 Oct 2026), so this script asks
for the orientation and aspect ratio you need in words, then crops to the exact
pixels. The untouched original stays where the server saved it and its path is
printed; --keep-original also copies it beside your output.

Examples:
  # Hero background: 1600x900 WebP, cropped to fill
  python3 gen_asset.py --prompt "misty pine forest at dawn, lots of sky" \
      --out src/assets/hero.webp --width 1600 --height 900

  # Three finished alternatives side by side, plus a contact sheet to compare
  python3 gen_asset.py --prompt "..." --out public/hero.webp --width 1600 --height 900 \
      --variants 3

  # Square avatar / mascot, PNG
  python3 gen_asset.py --prompt "friendly cartoon fox mascot, flat vector, centered" \
      --out public/avatar.png --square 256

  # Icon on a transparent background (asked of ChatGPT's tool; a flat-colour
  # background is knocked out locally only if the tool returned none)
  python3 gen_asset.py --prompt "letter B monogram, bold, centered" \
      --out public/favicon.ico --favicon --knockout-bg

  # Image-to-image: restyle an existing asset (the reference is uploaded to the chat)
  python3 gen_asset.py --ref public/hero.webp --out public/hero-night.webp \
      --prompt "same composition at night, keep the layout identical" --width 1600 --height 900

Constraints to respect:
  * One image at a time per provider: the server queues requests. An image
    takes 30 s to 2 min on Plus (53 s measured 13 Oct 2026).
  * ChatGPT counts images: its own counter read 120 per rolling day on Plus
    (13 Oct 2026). When it is spent the server answers 429 with the reset time
    and this script exits 3. Stop and report it; do not retry.
  * --width/--height crop-to-fill (cover) by default; --fit contain pads instead.

Exit codes: 0 done, 1 an error, 3 ChatGPT's image limit is reached.
"""
import argparse
import base64
import collections
import io
import json
import os
import sys
import urllib.error
import urllib.request

from PIL import Image, ImageDraw, ImageFont

_BASE = (os.environ.get("BROWSER_LLM_API") or os.environ.get("GEMINI_API")
         or "http://localhost:8081").rstrip("/")
API = _BASE + "/v1/images/generations"
EDIT_API = _BASE + "/v1/images/edits"      # used when --ref is given (image-to-image)
API_KEY = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
# Images go to ChatGPT unless told otherwise. Not the server's DEFAULT_PROVIDER:
# an install whose default is a text-only choice must still draw with ChatGPT.
DEFAULT_MODEL = os.environ.get("BROWSER_LLM_IMAGE_MODEL", "chatgpt-browser")

_EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                "image/gif": "gif"}


class GenAssetError(RuntimeError):
    """A failed generation. ``code`` is the CLI exit code (3 = usage limit).

    An exception, not SystemExit: the MCP server calls render() on a worker
    thread, where a SystemExit killed the thread and the caller never got a
    reply."""

    def __init__(self, message, code=1, retry_after=None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def _ref_spec(path_or_url):
    """Reference image → data: URL (works even when the server is on another
    machine and can't read this disk). http(s)/data: specs pass through."""
    s = str(path_or_url)
    if s[:5].lower() in ("http:", "https", "data:"):
        return s
    if not os.path.isfile(s):
        raise GenAssetError(f"[gen_asset] reference image not found: {s}")
    import mimetypes
    mime = mimetypes.guess_type(s)[0] or "image/png"
    with open(s, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def size_request(width=None, height=None, square_size=None):
    """The size to ask the image tool for: 'WxH', or None for its own choice."""
    if square_size:
        return f"{square_size}x{square_size}"
    if width and height:
        return f"{width}x{height}"
    return None


def generate_raw(prompt, model=None, timeout=440, refs=None, size=None,
                 background=None, variant=None):
    """Ask the server for one image. Returns (bytes, mime, entry) where entry
    is the response's data item plus its model_used. Raises GenAssetError."""
    payload = {"prompt": prompt, "model": model or DEFAULT_MODEL}
    if size:
        payload["size"] = size
    if background:
        payload["background"] = background
    if variant:
        payload["model_variant"] = variant
    url = API
    if refs:
        # Reference image(s) → image-to-image via the edits endpoint.
        payload["images"] = [_ref_spec(r) for r in refs]
        url = EDIT_API
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            detail = json.loads(body).get("detail") or body
        except Exception:
            detail = body
        if e.code == 429:
            raise GenAssetError(f"[gen_asset] {detail}", code=3,
                                retry_after=e.headers.get("Retry-After")) from None
        raise GenAssetError(f"[gen_asset] HTTP {e.code}: {str(detail)[:500]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GenAssetError(
            f"[gen_asset] no answer from {url} ({e}). An image may still have been "
            f"made: look in ~/Pictures/browser-llm/chatgpt/ before retrying.") from None
    data = d.get("data") or []
    if not data:
        raise GenAssetError("[gen_asset] API returned no image (prompt refused, or the "
                            "generation timed out server-side)")
    item = dict(data[0])  # providers may return several; the first is used
    item["model_used"] = d.get("model_used")
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("path") and os.path.isfile(item["path"]):
        with open(item["path"], "rb") as f:
            raw = f.read()
    elif item.get("url"):
        # Fallback for providers that only return a URL (e.g. a CORS-blocked
        # remote image); handles http(s) and data: URLs.
        with urllib.request.urlopen(item["url"], timeout=60) as r2:
            raw = r2.read()
    else:
        raise GenAssetError("[gen_asset] API response had neither b64_json nor url")
    mime = item.get("mime") or Image.open(io.BytesIO(raw)).get_format_mimetype() or "image/png"
    item.pop("b64_json", None)
    return raw, mime, item


def generate(prompt, model=None, timeout=440, refs=None, **kw):
    """One image as a Pillow image (RGBA when it has transparency, else RGB)."""
    raw, _mime, _item = generate_raw(prompt, model=model, timeout=timeout, refs=refs, **kw)
    return _open(raw)


def _open(raw):
    img = Image.open(io.BytesIO(raw))
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        return img.convert("RGBA")
    return img.convert("RGB")


def has_transparency(img):
    if img.mode != "RGBA":
        return False
    lo, _hi = img.getchannel("A").getextrema()
    return lo < 250


def resize_cover(img, w, h):
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    img = img.resize((round(sw * scale), round(sh * scale)), Image.LANCZOS)
    x = (img.width - w) // 2
    y = (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def resize_contain(img, w, h, bg=(255, 255, 255)):
    sw, sh = img.size
    scale = min(w / sw, h / sh)
    img = img.resize((round(sw * scale), round(sh * scale)), Image.LANCZOS)
    if img.mode == "RGBA":
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2), img)
        return canvas
    canvas = Image.new("RGB", (w, h), bg)
    canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    return canvas


def square(img, size):
    s = min(img.size)
    x = (img.width - s) // 2
    y = (img.height - s) // 2
    return img.crop((x, y, x + s, y + s)).resize((size, size), Image.LANCZOS)


def knockout_bg(img, tol=28):
    """Flood-fill from the borders, turning a flat background transparent.
    Leaves interior pixels of the same colour intact (unlike a global threshold)."""
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
    r0 = sum(c[0] for c in corners) // 4
    g0 = sum(c[1] for c in corners) // 4
    b0 = sum(c[2] for c in corners) // 4

    def close(c):
        return abs(c[0] - r0) <= tol and abs(c[1] - g0) <= tol and abs(c[2] - b0) <= tol

    seen = bytearray(w * h)
    dq = collections.deque()
    for x in range(w):
        dq.append((x, 0))
        dq.append((x, h - 1))
    for y in range(h):
        dq.append((0, y))
        dq.append((w - 1, y))
    while dq:
        x, y = dq.popleft()
        if x < 0 or y < 0 or x >= w or y >= h or seen[y * w + x]:
            continue
        seen[y * w + x] = 1
        c = px[x, y]
        if not close(c):
            continue
        px[x, y] = (c[0], c[1], c[2], 0)
        dq.extend([(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)])
    return img


def _write(img, out, fmt, favicon, quality):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if favicon or fmt == "ico":
        icon = img if img.mode == "RGBA" else img.convert("RGBA")
        icon = square(icon, 256)
        icon.save(out, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    elif fmt in ("jpg", "jpeg"):
        img.convert("RGB").save(out, quality=quality)
    elif fmt == "webp":
        img.save(out, quality=quality, method=6)
    else:  # png and anything else
        img.save(out)


def render_full(prompt, out, model=None, refs=None, timeout=440, width=None, height=None,
                square_size=None, fit="cover", fmt=None, favicon=False, knockout=False,
                quality=88, keep_original=False, variant=None, size=None):
    """Generate → shape → write. Returns a dict: ``out`` (the shaped asset),
    ``original`` (the untouched file as ChatGPT delivered it: the server's
    saved copy, or a copy beside ``out`` with keep_original), ``original_size``
    (its pixels), ``size`` (the asset's pixels) and ``model_used``.

    Crop/resize first, knockout after, format last."""
    what = f"editing {len(refs)} ref(s)" if refs else "generating"
    print(f"[gen_asset] {what} ({model or DEFAULT_MODEL}): {prompt[:70]}…", file=sys.stderr)
    raw, mime, item = generate_raw(
        prompt, model=model, timeout=timeout, refs=refs,
        size=size or size_request(width, height, square_size),
        background="transparent" if knockout else None, variant=variant)
    img = _open(raw)
    original_size = img.size

    original = item.get("path") if item.get("path") and os.path.isfile(item["path"]) else None
    if keep_original:
        stem = os.path.splitext(out)[0]
        copy = f"{stem}.original.{_EXT_BY_MIME.get(mime, 'png')}"
        os.makedirs(os.path.dirname(os.path.abspath(copy)), exist_ok=True)
        with open(copy, "wb") as f:
            f.write(raw)
        original = copy

    if square_size:
        img = square(img, square_size)
    elif width and height:
        img = resize_cover(img, width, height) if fit == "cover" \
            else resize_contain(img, width, height)

    # ChatGPT's tool can draw on a transparent background itself; knock out a
    # flat one locally only when it did not.
    if knockout and not has_transparency(img):
        img = knockout_bg(img)

    fmt = (fmt or out.rsplit(".", 1)[-1]).lower()
    _write(img, out, fmt, favicon, quality)

    used = item.get("model_used") or {}
    print(f"[gen_asset] wrote {out}  ({img.size[0]}x{img.size[1]}, {fmt}); original "
          f"{original_size[0]}x{original_size[1]} at {original or '(not saved)'}"
          f"{'; drawn via ' + str(used.get('model')) if used.get('model') else ''}",
          file=sys.stderr)
    return {"out": out, "original": original, "original_size": list(original_size),
            "size": list(img.size), "model_used": used or None}


def render(prompt, out, **kw):
    """render_full, returning only the output path (the MCP tool's old shape)."""
    return render_full(prompt, out, **kw)["out"]


def contact_sheet(paths, out, height=480, gap=24):
    """The variants side by side on a white sheet, labelled v1, v2, ..."""
    tiles = []
    for p in paths:
        im = Image.open(p).convert("RGBA")
        w = round(im.width * height / im.height)
        tiles.append(im.resize((w, height), Image.LANCZOS))
    # Labels at a size readable when the sheet is shown scaled to a screen.
    try:
        font = ImageFont.load_default(size=max(20, height // 14))
    except Exception:  # Pillow before 10.1 has no sized default font
        font = ImageFont.load_default()
    label_h = max(40, height // 14 + 20)
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + gap * (len(tiles) + 1),
                              height + label_h + gap * 2), (255, 255, 255))
    draw = ImageDraw.Draw(sheet)
    x = gap
    for i, t in enumerate(tiles, 1):
        sheet.paste(t, (x, gap), t)
        draw.text((x, gap + height + 10), f"v{i}  {os.path.basename(paths[i - 1])}",
                  fill=(20, 20, 20), font=font)
        x += t.width + gap
    sheet.save(out)
    return out


def render_variants(prompt, out, n, **kw):
    """N finished alternatives of one brief, written side by side as
    <stem>-v1.<ext> ... <stem>-vN.<ext>, plus <stem>-variants.png showing
    them together. The images are made one after another (the server runs one
    at a time). If ChatGPT's limit is reached part-way, the ones already made
    are kept and the GenAssetError is raised after the sheet is written."""
    stem, ext = os.path.splitext(out)
    made, stop = [], None
    for k in range(1, n + 1):
        brief = prompt if n == 1 else (
            f"{prompt}\n\nThis is alternative {k} of {n}: choose your own composition "
            f"for it.")
        try:
            made.append(render_full(brief, f"{stem}-v{k}{ext}", **kw))
        except GenAssetError as e:
            stop = e
            break
    sheet = None
    if len(made) > 1:
        sheet = contact_sheet([m["out"] for m in made], f"{stem}-variants.png")
        print(f"[gen_asset] contact sheet {sheet}", file=sys.stderr)
    if stop:
        if made:
            print(f"[gen_asset] made {len(made)} of {n} before stopping", file=sys.stderr)
        stop.made = made
        stop.sheet = sheet
        raise stop
    return {"variants": made, "sheet": sheet}


def main():
    ap = argparse.ArgumentParser(description="Generate a website image asset via the browser-LLM image API.")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True, help="output file; format inferred from extension")
    ap.add_argument("--model", help=f"provider (default {DEFAULT_MODEL}, or "
                    "$BROWSER_LLM_IMAGE_MODEL). Images come from ChatGPT; Gemini is "
                    "barred from images on Eben's machine since 13 Oct 2026")
    ap.add_argument("--variant", dest="model_variant",
                    help="ChatGPT model/power for this request, e.g. 'instant' or "
                         "'GPT-6/high' (default: the server's CHATGPT_MODEL/CHATGPT_EFFORT)")
    ap.add_argument("--ref", action="append", metavar="PATH|URL", dest="refs",
                    help="reference image for image-to-image (repeatable): the image is "
                         "uploaded into the chat with the prompt, so you can restyle, "
                         "extend or clean up an existing asset")
    ap.add_argument("--timeout", type=int, default=440,
                    help="seconds to wait for generation (default 440)")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--square", type=int, metavar="SIZE", help="center-crop to a SIZExSIZE square")
    ap.add_argument("--size", help="ask the image tool for this size or orientation "
                    "('1600x900', 'landscape') without cropping; default comes from "
                    "--width/--height or --square")
    ap.add_argument("--fit", choices=["cover", "contain"], default="cover")
    ap.add_argument("--format", help="override output format (png/webp/jpg/ico)")
    ap.add_argument("--favicon", action="store_true", help="write a multi-size .ico")
    ap.add_argument("--knockout-bg", action="store_true", dest="knockout",
                    help="ask for a transparent background, and knock a flat one out "
                         "locally if none came back (png/webp/ico only)")
    ap.add_argument("--keep-original", action="store_true",
                    help="also copy ChatGPT's untouched image beside the output as "
                         "<name>.original.<ext> (it is always kept in the server's folder)")
    ap.add_argument("--variants", type=int, default=1, metavar="N",
                    help="make N finished alternatives: <name>-v1 ... -vN plus "
                         "<name>-variants.png to compare them")
    ap.add_argument("--quality", type=int, default=88)
    args = ap.parse_args()

    kw = dict(model=args.model, refs=args.refs, timeout=args.timeout, width=args.width,
              height=args.height, square_size=args.square, fit=args.fit, fmt=args.format,
              favicon=args.favicon, knockout=args.knockout, quality=args.quality,
              keep_original=args.keep_original, variant=args.model_variant, size=args.size)
    try:
        if args.variants > 1:
            res = render_variants(args.prompt, args.out, args.variants, **kw)
            for m in res["variants"]:
                print(m["out"])
            if res["sheet"]:
                print(res["sheet"])
        else:
            print(render_full(args.prompt, args.out, **kw)["out"])
    except GenAssetError as e:
        print(str(e), file=sys.stderr)
        for m in getattr(e, "made", []) or []:
            print(m["out"])
        sys.exit(e.code)


if __name__ == "__main__":
    main()
