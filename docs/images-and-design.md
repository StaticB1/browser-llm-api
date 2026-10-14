# Images and design

ChatGPT is the image provider by default. Every image request switches its composer into the site's own Create image mode, asks for the size and background in words, waits for the picture to finish rendering, reads it out of the page, and saves it. On top of that sit two tools: `browser-llm image` for single assets cut to exact pixels, and `browser-llm design` for designs made as images.

## How an image is made

Before an image request, the ChatGPT provider opens the composer's **+** menu and picks **Create image**. That makes the image tool certain instead of asked for in words, and the response says so with `image_tool: true`. If the menu has moved, the request still goes, in words, with `image_tool: false`, and that is the signal to look at the page. `CHATGPT_IMAGE_TOOL=0` turns the mode off. The mode is a pill inside the composer that survives a reload, so a text request clears any pill left behind.

The size and background are written into the prompt in plain words, because the site's tool takes them from the conversation. No style words are added. ChatGPT draws a few fixed shapes near the asked ratio, measured on a Plus account in October 2026:

| Asked for | Delivered |
|---|---|
| square | 1254 × 1254 |
| 16:9 | 1672 × 941 |
| 1.91:1 | 1731 × 909 |
| 4:5 | 1122 × 1402 |

So the asset tool crops to the exact pixels afterwards. Asked for a transparent background, ChatGPT returned a real PNG with an alpha channel.

An image takes about a minute; 48 to 96 seconds were measured. Requests to one provider run one at a time, so a batch queues. The image is written to disk before the response returns, so after a client timeout, look for the newest file in the image folder before retrying. The provider's browser restarts after `BROWSER_RECYCLE_AFTER_IMAGES` images (default 8) to keep its memory in check.

ChatGPT's image tool needs an X display, because the picture renders on a canvas. Chrome's headless mode does not work, but a plain Xvfb display does, and that is what the background service uses.

## Limits

On a ChatGPT Plus account the page's own counter allowed 120 images per 24 hours, measured in October 2026. The counter did not drop as images were made, so it shows the plan's ceiling rather than live use. The server handles limits in three ways:

- **Before an image** it reads the counter. When `remaining` is at or below `CHATGPT_IMAGE_RESERVE` (default 0), it answers 429 with `Retry-After` and the reset time, without touching the page. Set a reserve to keep some images for your own chats.
- **After a request that produced no image** it reads the page's text. ChatGPT's refusals, such as "You've hit the Plus plan limit for image generation" or "You're generating images too quickly", become 429 with the reset time worked out.
- **Pacing** comes from the one-at-a-time lock; no gap is added.

On a 429, stop and wait for the reset. The asset tool exits with code 3 and keeps what it already made.

## Single assets

`browser-llm image` (also `./gen_asset.py`) asks for the size and background, then crops, converts, writes favicons, or knocks out a flat background when the tool did not make it transparent.

```bash
browser-llm image --prompt "pine forest at dawn, open sky across the top third for a headline" \
  --out src/assets/hero.webp --width 1920 --height 1080
browser-llm image --prompt "a calendar with a check mark" --kind icon --out public/icon-cal.png --square 256
browser-llm image --ref public/hero.webp --prompt "the same scene at night" --out public/hero-night.webp \
  --width 1920 --height 1080
browser-llm image --prompt "..." --out public/hero.webp --width 1600 --height 900 --variants 3
```

`--variants N` makes N alternatives and a sheet showing them side by side. `--keep-original` keeps ChatGPT's untouched file beside the output, and `--upscale 2` enlarges a result with Lanczos and a light sharpen. The [agent image guide](agent-image-guide.md) lists every flag, with recipes for each asset type.

## Design kinds

A kind wraps your brief in the rules that make an image buildable, and brings a default size and background. The prompt carries structure only: what the image is, text readable at half size, a column grid, the real words from the brief, no boxes nested inside boxes. It adds no style words, so ChatGPT chooses the look. `--direction` adds an art-direction block for the kind's group, for when you want a stronger look.

| Group | Kind | Makes |
|---|---|---|
| Web | `section` | one section of a website at desktop width, as a flat mockup |
| | `page` | a short page in one tall image; text gets small, so prefer sections |
| | `hero-image` | a background photo or illustration with room for an HTML headline |
| | `og-image` | a 1200 × 630 link preview, cropped to exact pixels |
| App | `mobile-screen` | one app screen in a phone frame (iOS, Android or neutral) |
| | `component` | one component with its states side by side |
| Brand | `brand-board` | a 3 × 3 identity board with colours, type and applications |
| | `icon` | one centred icon on a transparent background |
| | `logo-concept` | four different logo concepts on one sheet |
| Content | `illustration` | one spot illustration on a transparent background |
| | `diagram` | a diagram that uses exactly the labels given |
| | `texture` | an even texture for backgrounds |
| | `avatar` | one character, head and shoulders, on a transparent background |

`browser-llm design kinds` lists them, and `browser-llm design prompt --kind section --brief "..."` prints the exact words a kind sends, without spending an image. The image API takes the same options as fields; see [API](api.md#image-generation).

## Design sets

A set makes one image per section or screen, in order. The first image becomes the style source. Every later image is made with the first image and the one before it attached, together with your own references, so the palette, type and spacing hold across the set. The prompt also tells the image tool not to repeat the references' components, photographs or text.

```bash
browser-llm design set --kind section --brief "<product, audience, the action the page must get>" \
  --section "Hero: <headline, button text>" --section "<name>: <the real content>" \
  --out designs/home --dry-run          # read every prompt; spends nothing
browser-llm design set ... --out designs/home
browser-llm design set ... --out designs/home --directions 3          # three looks for item 1, then stops
browser-llm design set ... --out designs/home --directions 3 --pick 2
```

The folder gets `01-hero.png`, `02-...png` and so on, a `manifest.json` written after every image, and `preview.png` with the set stacked like a page (or screens side by side). Running the same command again resumes: finished items are skipped, changed ones are made again, and `--redo 3` forces one. `--lock approved.png` makes an image you already approved the style source for every item, and `--ref logo.png` attaches your own references to every image. Six attachments fit in one request, references first. A JSON spec file (`--spec`) can hold the brief, sections, design system and references instead of flags.

Before a set starts, it compares the images it will make with ChatGPT's counter minus the reserve, and refuses with the numbers unless you pass `--partial`. The counter can overstate what is left, so the site's own refusal stays the real stop. A set stopped either way keeps what it made and resumes later. The exit code is 3.

## Tokens

`browser-llm design tokens` sends up to six finished images back to ChatGPT and asks for the design system as JSON. That covers colours, typefaces, the type scale, spacing, radii, shadows, components, and the layout of each section. It also measures the most common colours from the pixels and hands them over, so the colour list matches what is really there. It keeps photographs' colours out of the list and describes them under imagery instead.

```bash
browser-llm design tokens designs/home/0*.png --out designs/home
```

The output is `tokens.json`, `tokens.css` with CSS custom properties, and `DESIGN.md` for the coding step. Sizes are ChatGPT's estimates from a picture, so a 64-pixel heading may come back as 60 or 72. Treat the first image as the authority and the tokens as a strong first draft.

## Review

`browser-llm design review` compares a screenshot of your build with its design and lists every visible difference, with a severity, what each side shows, and a fix in CSS terms. It also gives a match score from 0 to 100. Take the screenshot at the design's width, 1440 pixels for web sections.

```bash
browser-llm design review --design designs/home/01-hero.png --build shot-1440.png --width 1440 \
  --out designs/home/review-hero.md
browser-llm design review --critique shot-1440.png --out shot-critique.md
```

In the measured run, one round of a review's fixes raised a build from 77 to 82 and cut the findings from 20 to 14. Its pixel estimates swing by about ten percent between rounds, so trust its direction more than its exact numbers. The [design upgrade write-up](design-upgrade.md) has the full run.

## In the dashboard and for agents

The dashboard's Create image page has a presets row: kind, shape, theme, platform, headline space, art direction, and a design-system box. Each result shows the prompt that was sent. For agents, the MCP tools `generate_image`, `design_set`, `design_tokens` and `design_review` take the same options ([MCP](mcp.md)). A set of more than six images is better run from the command line, because an agent client may time out a call that long; the set keeps going and resumes.
