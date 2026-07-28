# Generating image assets (instructions for the coding agent)

You have a **local image-generation API** (Google Gemini, driven via browser automation)
for creating raster image assets while building the website: hero images, section
background images, textures/patterns, avatars/mascots, favicons, app icons, illustrations,
og/social images, etc. **Use it to produce real assets instead of leaving placeholder
boxes, lorem-picsum links, or empty `src`s.**

The endpoint runs at `http://localhost:8081`.

> **Provider note:** this API is multi-provider (Gemini and ChatGPT). Image requests without a
> `model` field (as `gen_asset.py` sends) use the server's `DEFAULT_PROVIDER`. The Gemini-specific
> constraints below (fixed ~1024×559 output, ✦ watermark, Google-account quota) apply when the
> default is `gemini-browser`; ChatGPT returns a larger PNG with no watermark but needs the server
> on a GPU display. Post-processing via `gen_asset.py` is identical either way.

---

## 1. Hard constraints — read before using

- **Raw output is fixed:** every call returns **one JPEG, ~1024×559 px, landscape, fully
  opaque (no transparency).** The `n`, `size`, and aspect-ratio parameters are **ignored**.
- Therefore you **must post-process** for any other dimension, aspect ratio, square/portrait
  shape, transparency, or file format. Use `gen_asset.py` (below) — it does this for you.
- **Latency: ~30–40 s per image when warm, occasionally up to ~3 min** under load. The API handles
  **ONE request at a time** — generate assets **sequentially, never in parallel** (parallel calls
  just queue and can time out). Use a generous timeout: `gen_asset.py` waits `--timeout` (default
  440 s); if you call the raw API, set a **≥300 s (ideally ~450 s) client timeout** to match the
  server's 420 s ceiling. A short client timeout is the #1 cause of "it failed" when the image
  actually generated fine.
- **A timed-out/502 call usually still produced the image.** The server writes the file to disk
  (and returns its `path`) **before** the HTTP response, so on a timeout/502 check the newest file
  in `GEMINI_IMAGE_DIR` rather than blindly retrying. Long sequential runs stay healthy — the server
  **auto-recycles its browser every few image gens**, so you won't hit the old "slows down / times
  out after 4–5 images" wall and never need a manual restart.
- **Watermark:** Gemini stamps a small ✦ sparkle in the **bottom-right corner**. Square/center
  crops (`--square`) remove it; for full-bleed images either crop a little off the bottom-right
  or keep important content away from that corner.
- **Don't put text in images** — Gemini renders words unreliably. Add real text/logos in HTML/CSS
  on top of the image instead.
- Not suitable for **exact brand logos, precise typography, or pixel-accurate diagrams.**

---

## 2. Preferred path: `gen_asset.py`

A wrapper that does **generate → resize/crop/convert/favicon → write the file**. Call it once
per asset. (It lives at `gen_asset.py`; copy it into the project or call by
absolute path. Needs Python + Pillow.)

```
python3 gen_asset.py --prompt "<description>" --out <path> [shaping flags]
```

Flags:
| flag | meaning |
|------|---------|
| `--out PATH` | output file; **format inferred from extension** (`.webp`/`.png`/`.jpg`/`.ico`) |
| `--width W --height H` | resize to exactly WxH, **crop-to-fill** (cover) by default |
| `--fit contain` | pad instead of crop (letterbox on white) |
| `--square SIZE` | center-crop to a `SIZE×SIZE` square (avatars, icons) — also removes the corner watermark |
| `--favicon` | write a multi-size `.ico` (16/32/48/64) |
| `--knockout-bg` | make a flat background transparent (png/webp/ico only; prompt a "solid <color> background") |
| `--format` | override the inferred format |
| `--quality N` | JPEG/WebP quality (default 88) |
| `--model NAME` | pick the provider (`gemini-browser` / `chatgpt-browser`); default = server's `DEFAULT_PROVIDER` |
| `--timeout SEC` | how long to wait for generation (default 440; ChatGPT can be slow) |

It prints the output path on success. Verify the file (open it / check dimensions) and
**regenerate with a refined prompt if it's off** — output is non-deterministic.

---

## 3. Recipes by asset type

**Hero / full-width background** — WebP, sized to the layout, keep it uncluttered so text stays legible:
```
python3 gen_asset.py \
  --prompt "misty pine forest at dawn, cinematic wide shot, muted greens, soft light, lots of open sky, uncluttered" \
  --out src/assets/hero.webp --width 1920 --height 1080
```

**Section background** — subtle, low-contrast so foreground content reads:
```
python3 gen_asset.py \
  --prompt "abstract soft gradient mesh, pastel blue and lavender, very subtle, minimal, blurred" \
  --out public/bg/features.webp --width 1600 --height 1000
```

**Texture / pattern** — ask for "seamless / tileable / low contrast" (tiling is approximate):
```
python3 gen_asset.py \
  --prompt "seamless subtle light-grey concrete texture, uniform, low contrast, tileable" \
  --out public/textures/concrete.webp --width 1024 --height 1024
```

**Avatar / mascot** — square PNG; prompt "centered … plain white background":
```
python3 gen_asset.py \
  --prompt "friendly cartoon fox mascot, flat vector, centered, plain white background" \
  --out public/avatar.png --square 256 --knockout-bg
```

**Favicon / app icon** — simple bold mark, centered, solid background:
```
python3 gen_asset.py \
  --prompt "minimalist geometric mountain icon, bold, centered, solid white background, simple" \
  --out public/favicon.ico --favicon --knockout-bg
# also emit a large PNG for apple-touch-icon / PWA:
python3 gen_asset.py \
  --prompt "minimalist geometric mountain icon, bold, centered, solid white background, simple" \
  --out public/icon-512.png --square 512 --knockout-bg
```

**Illustration / spot image** — transparent PNG on a flat background:
```
python3 gen_asset.py \
  --prompt "flat illustration of a rocket launching, bright colors, plain white background" \
  --out src/assets/rocket.png --square 512 --knockout-bg
```

**OG / social share image** — 1200×630:
```
python3 gen_asset.py \
  --prompt "modern abstract tech background, deep blue and cyan, subtle geometric shapes" \
  --out public/og-image.jpg --width 1200 --height 630
```

---

## 3b. Editing an existing image (image-to-image)

`--ref FILE` (repeatable) uploads a reference image with the prompt, so you can restyle or fix an
asset instead of regenerating it from a description. Same post-processing flags apply.

```bash
# re-colour an existing hero to a night palette, same composition
python3 gen_asset.py --ref public/hero.webp --out public/hero-night.webp \
    --prompt "identical composition and layout, night-time palette, deep blues, moonlit" \
    --width 1600 --height 900

# turn a screenshot mock into a clean flat-vector illustration
python3 gen_asset.py --ref mock.png --out public/feature.png \
    --prompt "same layout, flat vector illustration, solid white background"
```

Say what must stay the same ("identical layout", "keep the text") — the model otherwise drifts.
Raw endpoint: `POST /v1/images/edits` (multipart `image=@file` + `prompt`, or JSON with
`{"image": "<path|data-url>"}`).

## 4. Prompt guidance

- **Structure:** `subject + style + composition + palette + lighting/mood`.
  e.g. "*cozy reading nook by a rainy window, watercolor style, warm lamplight, muted palette*".
- **Style keywords steer it hard:** `flat vector`, `photographic`, `3d render`, `watercolor`,
  `line art`, `isometric`, `minimalist`, `cinematic`.
- **Backgrounds:** add "uncluttered / lots of negative space / subtle / low contrast" so overlaid
  text is readable.
- **Icons/logos/cutouts:** add "centered, simple, bold, solid <color> background" and use
  `--square` + `--knockout-bg`.
- **Consistency:** reuse the same style/palette phrasing across a set of assets so they match.

---

## 5. Rules / workflow

1. **Format:** WebP for photos & backgrounds (smallest), PNG for anything needing transparency,
   ICO for favicons, JPG only if a tool requires it.
2. **Size to the real rendered size** — don't ship a 1024px image where 400px is displayed.
3. **Commit generated assets into the repo.** Do **not** regenerate on every build: it's slow,
   non-deterministic, and consumes the account's quota. Generate once, review, commit.
4. **Name assets meaningfully** in the project (`hero-home.webp`, `favicon.ico`), not the raw
   `gemini_*.jpg` filenames.
5. **Generate sequentially** (one call at a time).
6. **Verify each asset** (dimensions/appearance) and refine the prompt + regenerate if needed.

---

## 6. Raw API (only if you can't use the wrapper)

```bash
curl -s http://localhost:8081/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red bicycle on a beach at sunset"}'
```
Response — use whichever field suits you (`path` is already a file on disk):
```json
{"created": 1782..., "data": [{
  "b64_json": "<base64 jpeg>",
  "url":  "http://localhost:8081/images/gemini/gemini_....jpg",
  "path": "~/Pictures/browser-llm/gemini/gemini_....jpg"
}]}
```
Image-to-image with the raw API — multipart, or JSON with an `image` field:
```bash
curl -s http://localhost:8081/v1/images/edits \
  -F image=@public/hero.webp -F 'prompt=identical layout, night-time palette'
```
You still have to resize/crop/convert yourself (the wrapper exists so you don't).

---

## 7. Limits

- Best-effort transparency (flat-background knockout); for clean cutouts always prompt a plain
  solid background.
- No exact text, logos, or precise layouts. `--ref` helps a lot here: editing an existing asset
  holds a layout far better than describing one, but still verify the output rather than trusting it.
- At most `MAX_ATTACHMENTS` (default 6) reference images per request, `MAX_ATTACHMENT_MB` (20) each.
- Shared Google-account quota; if generation starts failing/returning empty, the login session
  may need re-auth (a human task — tell the user).
