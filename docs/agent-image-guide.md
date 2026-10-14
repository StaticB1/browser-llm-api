# Generating image assets (instructions for the coding agent)

You have a **local image-generation API** (ChatGPT's image tool, driven via browser automation)
for creating raster image assets while building a website or app: hero images, section
backgrounds, textures, avatars and mascots, favicons and app icons, illustrations, og/social
images and product shots. **Use it to produce real assets instead of leaving placeholder
boxes, lorem-picsum links, or empty `src`s.**

The endpoint runs at `http://localhost:8081`.

> **Provider:** images are drawn by ChatGPT (`chatgpt-browser`). An install can bar a provider
> from images with `IMAGE_PROVIDERS`, and a barred one answers 403. The tools draw with ChatGPT
> unless you pass `--model`.

> **Commands:** `browser-llm` below is `<checkout>/venv/bin/browser-llm`. The same tool also
> runs under its pre-2.0 path, `<checkout>/venv/bin/python <checkout>/gen_asset.py`, with the
> same flags.

---

## 1. Hard constraints — read before using

- **ChatGPT picks the pixel size.** It draws a shape near the ratio you ask for: on 13 Oct 2026
  1254×1254 for square, 1672×941 for 16:9, 1731×909 for 1.91:1 and 1122×1402 for 4:5. So
  ask for the size you need and let `gen_asset.py` crop to the exact pixels. The untouched
  original is kept by the server (its `path`) and, with `--keep-original`, beside your file.
- **Transparency works.** Asked for a transparent background, ChatGPT returns a real RGBA PNG
  (63% of pixels fully clear in the 13 Oct 2026 test). `--knockout-bg` asks for it and only falls
  back to knocking out a flat background if no alpha came back.
- **Latency: ~50–90 s per image** on Plus (measured 13 Oct 2026). The API handles **one image
  request at a time** — generate assets **sequentially, never in parallel** (parallel calls just
  queue and can time out). Use a generous timeout: `gen_asset.py` waits `--timeout` (default
  440 s); if you call the raw API, set a **client timeout of at least 300 s, ideally ~450 s**.
  A short client timeout is the #1 cause of "it failed" when the image was made fine.
- **A timed-out call usually still produced the image.** The server writes the file to disk
  before the HTTP response, so on a timeout check the newest file in
  the server's image folder (`BROWSER_LLM_IMAGE_DIR`, default `~/Pictures/browser-llm`, in
  its `chatgpt/` subfolder) rather than retrying blindly.
- **Limits.** Plus allows 120 images per 24 hours (the page's own counter, 13 Oct 2026), and an
  install can keep some for the account owner (`CHATGPT_IMAGE_RESERVE`). When the limit is reached
  the API answers **HTTP 429** with `Retry-After`, and the tool exits with **code 3** and prints the reset
  time. **Stop, tell the user the reset time, and do not retry before it.** With `--variants`
  the alternatives already made are kept.
- **Text in images:** ChatGPT writes short words fairly well, but check every letter. Put real
  copy, prices and logos in HTML/CSS on top of the image.
- Not suitable for **exact brand logos or pixel-accurate diagrams**; use `--ref` with the real
  asset when something must match.

---

## 2. Preferred path: `browser-llm image`

A wrapper that does **generate → crop/resize/convert/favicon → write the file**. Call it once
per asset. It needs the checkout's venv (Python with Pillow). A copy of `browser_llm/gen_asset.py`
also runs on its own in another project, without the design kinds.

```
browser-llm image --prompt "<description>" --out <path> [shaping flags]
```

Flags:
| flag | meaning |
|------|---------|
| `--out PATH` | output file; **format inferred from extension** (`.webp`/`.png`/`.jpg`/`.ico`) |
| `--width W --height H` | ask for that size, then resize to exactly WxH, **crop-to-fill** (cover) by default |
| `--fit contain` | pad instead of crop (letterbox on white, or transparent if the image has alpha) |
| `--square SIZE` | ask for a square, then centre-crop to `SIZE×SIZE` (avatars, icons) |
| `--size SIZE` | ask the image tool for `WxH`, `square`, `landscape` or `portrait` without reshaping |
| `--favicon` | write a multi-size `.ico` (16/32/48/64) |
| `--knockout-bg` | ask for a transparent background (png/webp/ico only) |
| `--keep-original` | also write ChatGPT's untouched file as `<name>.original.<ext>` |
| `--variants N` | make N alternatives: `<name>-v1 … -vN` plus `<name>-variants.png` showing them side by side |
| `--ref PATH` | reference image for image-to-image (repeatable) |
| `--variant MODEL` | ChatGPT model/power for this image, e.g. `instant` (default: the server's GPT-6 at High) |
| `--format` | override the inferred format |
| `--quality N` | JPEG/WebP quality (default 88) |
| `--model NAME` | provider; default `chatgpt-browser` |
| `--timeout SEC` | how long to wait for generation (default 440) |
| `--kind KIND` | wrap the prompt in a design kind's rules, size and background (see 2b) |
| `--direction` | with `--kind`: add the art direction for the kind's group |
| `--theme light\|dark`, `--platform ios\|android\|neutral`, `--copy-space left\|right\|center\|top\|bottom` | kind options |
| `--system TEXT`, `--system-file FILE` | design-system text the image must follow exactly (a `DESIGN.md` works) |
| `--upscale F` | enlarge the result by F with Lanczos and a light sharpen |

It prints the output path, the original's size and the model that drew it. Open the file and
**regenerate with a refined prompt if it's off** — output is non-deterministic. Exit codes: 0
done, 1 failed, 3 image limit reached.

From an MCP client, the `generate_image` tool takes the same options (`width`, `height`,
`square`, `knockout_bg`, `keep_original`, `variants`, `refs`, `model_variant`, `kind`,
`direction`, `theme`, `platform`, `system`, `copy_space`, `upscale`).

---

## 2b. Designing a page or an app flow: `browser-llm design`

When the job is a **design** (a landing page, an app flow, a component, a brand board) rather
than one asset, generate the design as images first and build from them:

1. **One image per section or screen.** Never squeeze a page into one image: the text gets too
   small to read back. `browser-llm design set` makes one large image per item.
2. **One design system.** Each item after the first is made with the first item and the one
   before it attached, so palette, type and spacing hold. Pass the user's logo or brand assets
   with `--ref`, and an approved design with `--lock`.
3. **Read it back.** `browser-llm design tokens` writes `tokens.json`, `tokens.css` and `DESIGN.md`.
   Build with those values and the images open side by side.
4. **Check the build.** Screenshot it at the design's width (1440 for web sections) and run
   `browser-llm design review`; fix the high-severity differences and review again.

```bash
browser-llm design set --kind section \
  --brief "<product, audience, the action the page must get, facts and real copy>" \
  --section "Hero: <headline, button text>" --section "<name>: <content>" ... \
  --out designs/home --dry-run            # read the prompts first; spends nothing
browser-llm design set ... --out designs/home     # about a minute per image
browser-llm design tokens designs/home/0*.png --out designs/home
browser-llm design review --design designs/home/01-hero.png \
  --build shot-1440.png --width 1440 --out designs/home/review-hero.md
```

Kinds (`browser-llm design kinds`): `section`, `page`, `hero-image`, `og-image`, `mobile-screen`,
`component`, `brand-board`, `icon`, `logo-concept`, `illustration`, `diagram`, `texture`,
`avatar`. Put the real words in each section's content: the image tool renders text well when it
is given exact words, and invents filler when it is not. `--direction` adds an art-direction
block; leave it off unless the person asked for that look or the first round came back generic.
`--directions 3` makes three candidates for the first item and stops so you (or the person) can
choose; continue with `--pick K`.

A set refuses to start when it does not fit in what is left of the day's images (exit code 3,
with the numbers); `--partial` overrides. A set stopped by the limit keeps what it made: run the
same command after the reset and it continues. The MCP tools `design_set`, `design_tokens` and
`design_review` do the same; prefer the CLI in the background for sets over six images, because
an MCP client may time the call out (the set keeps going and resumes).

---

## 3. Recipes by asset type

Describe the subject, what the image is for and where it sits on the page. Add style words
only when the person asked for that style; don't add `minimal`, `clean`, `subtle`, `muted`,
`elegant`, `modern`, `professional`, `sleek` or `editorial` on your own (they pull every image
towards the same look).

**Hero / full-width background** — WebP, sized to the layout, with room for the headline:
```
browser-llm image \
  --prompt "pine forest at dawn, wide shot, open sky across the top third for a headline" \
  --out src/assets/hero.webp --width 1920 --height 1080
```

**Section background** — behind body text, so it must stay readable:
```
browser-llm image \
  --prompt "soft gradient in pale blue and lavender, used behind paragraphs of dark text" \
  --out public/bg/features.webp --width 1600 --height 1000
```

**Texture / pattern** — ask for "seamless / tileable" (tiling is approximate):
```
browser-llm image \
  --prompt "seamless light-grey concrete texture, tileable, low contrast" \
  --out public/textures/concrete.webp --width 1024 --height 1024
```

**Avatar / mascot** — square PNG with a transparent background:
```
browser-llm image \
  --prompt "friendly cartoon fox mascot, centred, full body" \
  --out public/avatar.png --square 256 --knockout-bg
```

**Favicon / app icon** — one bold mark, centred:
```
browser-llm image \
  --prompt "geometric mountain icon, one bold shape, centred, readable at 16 px" \
  --out public/favicon.ico --favicon --knockout-bg
# also emit a large PNG for apple-touch-icon / PWA:
browser-llm image \
  --prompt "geometric mountain icon, one bold shape, centred, readable at 16 px" \
  --out public/icon-512.png --square 512 --knockout-bg
```

**Illustration / spot image** — transparent PNG:
```
browser-llm image \
  --prompt "illustration of a rocket launching, bright colours" \
  --out src/assets/rocket.png --square 512 --knockout-bg
```

**OG / social share image** — 1200×630; try a few and pick one:
```
browser-llm image \
  --prompt "share image for a cloud backup product: a laptop on a desk with files flowing up into the sky" \
  --out public/og-image.jpg --width 1200 --height 630 --variants 3
```

---

## 3b. Editing an existing image (image-to-image)

`--ref FILE` (repeatable) uploads a reference image with the prompt, so you can restyle or fix an
asset instead of regenerating it from a description. Same post-processing flags apply.

```bash
# the same hero at night, same composition
browser-llm image --ref public/hero.webp --out public/hero-night.webp \
    --prompt "identical composition and layout, at night under moonlight" \
    --width 1600 --height 900

# put a product cutout into a scene
browser-llm image --ref teapot.png --out public/teapot-kitchen.png \
    --prompt "place this exact teapot on a marble kitchen counter next to a bowl of lemons" \
    --width 1080 --height 1350
```

Say what must stay the same ("identical layout", "this exact teapot") — the model otherwise
drifts. Raw endpoint: `POST /v1/images/edits` (multipart `image=@file` + `prompt`, or JSON with
`{"image": "<path|data-url>"}`).

## 4. Prompt guidance

- **Structure:** `subject + setting + composition + what the image is for`, e.g. "*a reading
  nook by a rainy window, lamp on, room on the left for a quote*".
- **Purpose and placement beat adjectives:** "behind white headline text", "card thumbnail at
  320 px", "full-bleed on a phone" tell the tool more than a list of moods.
- **Style words only from the brief:** if the person said "watercolour" or "flat vector", pass
  it on; don't invent a style for them.
- **Consistency:** reuse the same subject and setting phrasing across a set of assets so they
  match, or pass the first asset as `--ref` for the next.

---

## 5. Rules / workflow

1. **Format:** WebP for photos and backgrounds (smallest), PNG for anything needing
   transparency, ICO for favicons, JPG only if a tool requires it.
2. **Size to the real rendered size** — don't ship a 1600 px image where 400 px is displayed.
3. **Commit generated assets into the repo.** Do **not** regenerate on every build: it's slow,
   non-deterministic, and uses the account's daily image allowance. Generate once, review, commit.
4. **Name assets meaningfully** in the project (`hero-home.webp`, `favicon.ico`), not the raw
   `chatgpt_*.png` filenames.
5. **Generate sequentially** (one call at a time).
6. **Verify each asset** (dimensions and appearance) and refine the prompt and regenerate if
   needed. On exit code 3, stop and report the reset time.

---

## 6. Raw API (only if you can't use the wrapper)

```bash
curl -s http://localhost:8081/v1/images/generations \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"a red bicycle on a beach at sunset","size":"1600x900"}'
```
Response — use whichever field suits you (`path` is already a file on disk):
```json
{"created": 1791..., "data": [{
  "b64_json": "<base64 png>", "width": 1672, "height": 941, "mime": "image/png",
  "url":  "http://localhost:8081/images/chatgpt/chatgpt_....png",
  "path": "~/Pictures/browser-llm/chatgpt/chatgpt_....png"
}], "model_used": {"model": "gpt-5-4-thinking", "picker": "GPT-6 / High"}}
```
`size` (`WxH`, `square`, `landscape`, `portrait`), `background` (`transparent`) and
`model_variant` are accepted. A 429 means the image limit is reached; its `detail` and
`Retry-After` give the reset time.

Image-to-image with the raw API — multipart, or JSON with an `image` field:
```bash
curl -s http://localhost:8081/v1/images/edits \
  -F image=@public/hero.webp -F 'prompt=identical layout, at night'
```
You still have to crop and convert yourself (the wrapper exists so you don't).

---

## 7. Limits

- No exact logos or precise layouts. `--ref` helps a lot: editing an existing asset holds a
  layout far better than describing one, but still verify the output.
- At most `MAX_ATTACHMENTS` (default 6) reference images per request, `MAX_ATTACHMENT_MB` (20) each.
- One ChatGPT account is shared by everything using this server. If generation starts failing or
  returning nothing, the login may need renewing (a human task — tell the user).
