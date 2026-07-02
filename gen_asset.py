#!/usr/bin/env python3
"""
Generate a website image asset via the local browser-LLM image API, then
post-process it (resize / crop / convert / favicon / knockout) into exactly what
the site needs.

The API returns a landscape raster with no transparency — Gemini: ~1024x559 JPEG
(with a ✦ watermark bottom-right); ChatGPT: a larger PNG, no watermark. This wraps
it: generate -> Pillow post-process -> write the final asset file. Use --model to
pick the provider (default = the server's DEFAULT_PROVIDER).

Examples:
  # Hero background: 1600x900 WebP, cropped to fill
  python3 gen_asset.py --prompt "misty pine forest at dawn, cinematic, muted greens, lots of sky" \
      --out src/assets/hero.webp --width 1600 --height 900

  # Section background texture (square-ish)
  python3 gen_asset.py --prompt "subtle light-grey paper texture, minimal, low contrast" \
      --out public/textures/paper.webp --width 1200 --height 1200

  # Square avatar / mascot, PNG
  python3 gen_asset.py --prompt "friendly cartoon fox mascot, flat vector, centered, plain white background" \
      --out public/avatar.png --square 256

  # Favicon (.ico, 16/32/48/64) from a simple centered mark on a flat background
  python3 gen_asset.py --prompt "minimalist letter B monogram, bold, centered, solid white background" \
      --out public/favicon.ico --favicon --knockout-bg

Constraints to respect:
  * Generation is slow and serialized per provider — generate sequentially, never
    in parallel. Gemini ~30s; ChatGPT ~30s-4min (free tier), so --timeout defaults
    to 440s (above the server's completion deadline).
  * ChatGPT image generation needs the server on a real GPU display (it renders on
    a <canvas> that stalls under headless Xvfb); Gemini works headless.
  * --width/--height crop-to-fill (cover) by default; --fit contain pads instead.
  * --knockout-bg makes a flat background transparent (best-effort; only meaningful
    for png/webp/ico output). For clean results prompt "solid <color> background".
"""
import argparse
import base64
import collections
import io
import json
import os
import sys
import urllib.request

from PIL import Image

API = os.environ.get("GEMINI_API", "http://localhost:8081") + "/v1/images/generations"


def generate(prompt, model=None, timeout=440):
    payload = {"prompt": prompt}
    if model:
        payload["model"] = model  # else the server uses its DEFAULT_PROVIDER
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    data = d.get("data") or []
    if not data:
        raise SystemExit("API returned no image (prompt refused, or gen timed out server-side)")
    item = data[0]  # providers may return multiple variants; use the first
    if item.get("b64_json"):
        raw = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        # Fallback for providers that only return a URL (e.g. a CORS-blocked
        # remote image); handles http(s) and data: URLs.
        with urllib.request.urlopen(item["url"], timeout=60) as r2:
            raw = r2.read()
    else:
        raise SystemExit("API response had neither b64_json nor url")
    return Image.open(io.BytesIO(raw)).convert("RGB")


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
        dq.append((x, 0)); dq.append((x, h - 1))
    for y in range(h):
        dq.append((0, y)); dq.append((w - 1, y))
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


def main():
    ap = argparse.ArgumentParser(description="Generate a website image asset via the browser-LLM image API.")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--out", required=True, help="output file; format inferred from extension")
    ap.add_argument("--model", help="provider, e.g. gemini-browser | chatgpt-browser "
                    "(default = server's DEFAULT_PROVIDER)")
    ap.add_argument("--timeout", type=int, default=440,
                    help="seconds to wait for generation (default 440; ChatGPT can be slow)")
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--square", type=int, metavar="SIZE", help="center-crop to a SIZExSIZE square")
    ap.add_argument("--fit", choices=["cover", "contain"], default="cover")
    ap.add_argument("--format", help="override output format (png/webp/jpg/ico)")
    ap.add_argument("--favicon", action="store_true", help="write a multi-size .ico")
    ap.add_argument("--knockout-bg", action="store_true", dest="knockout",
                    help="make a flat background transparent (png/webp/ico only)")
    ap.add_argument("--quality", type=int, default=88)
    args = ap.parse_args()

    print(f"[gen_asset] generating ({args.model or 'default'}): {args.prompt[:70]}…", file=sys.stderr)
    img = generate(args.prompt, model=args.model, timeout=args.timeout)

    if args.square:
        img = square(img, args.square)
    elif args.width and args.height:
        img = resize_cover(img, args.width, args.height) if args.fit == "cover" \
            else resize_contain(img, args.width, args.height)

    if args.knockout:
        img = knockout_bg(img)

    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    fmt = (args.format or out.rsplit(".", 1)[-1]).lower()

    if args.favicon or fmt == "ico":
        icon = img if img.mode == "RGBA" else img.convert("RGBA")
        icon = square(icon, 256)
        icon.save(out, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])
    elif fmt in ("jpg", "jpeg"):
        img.convert("RGB").save(out, quality=args.quality)
    elif fmt == "webp":
        img.save(out, quality=args.quality, method=6)
    else:  # png and anything else
        img.save(out)

    print(f"[gen_asset] wrote {out}  ({img.size[0]}x{img.size[1]}, {fmt})", file=sys.stderr)
    print(out)


if __name__ == "__main__":
    main()
