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
- **Latency ≈ 30 s per image, and the API handles ONE request at a time.** Generate assets
  **sequentially, never in parallel** (parallel calls just queue and can time out).
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
  "url":  "http://localhost:8081/images/gemini_....jpg",
  "path": "~/Pictures/gemini/gemini_....jpg"
}]}
```
You still have to resize/crop/convert yourself (the wrapper exists so you don't).

---

## 7. Limits

- Best-effort transparency (flat-background knockout); for clean cutouts always prompt a plain
  solid background.
- No exact text, logos, or precise layouts.
- Shared Google-account quota; if generation starts failing/returning empty, the login session
  may need re-auth (a human task — tell the user).
