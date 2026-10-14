# Architecture

The server is a FastAPI app that turns an OpenAI-shaped request into actions on a real chat page and reads the answer back out of the page. Everything specific to one site lives in a provider; everything else is shared.

## How a request flows

1. The `model` field picks a provider (`chatgpt-browser` or `gemini-browser`).
2. Each provider has one persistent Chrome, with its own profile, started on first use. A lock per provider serializes its requests, so ChatGPT and Gemini can run at the same time while two requests to one provider queue.
3. The `messages` array is flattened into one prompt, with labelled turns, and any images are collected. Attachments are checked before anything starts, so a bad one gets a real 400.
4. The images become temporary files. The provider opens a fresh chat, prepares the page (for ChatGPT: the model picker, the image counter, and Create image mode for an image request), uploads the files through the site's own file picker, types the prompt and sends it.
5. A polling loop reads the answer's text, whether the site is still generating, and the state of any image. A completion tracker decides when the answer is done.
6. Generated images are read out of the page as base64, saved under the image folder, and returned with a link. The page is left open on purpose: navigating away mid-session destabilizes the browser.

## Deciding when an answer is done

The completion tracker is fed one sample per poll and returns either more text or a reason to stop. An answer is done when an image has rendered and stayed stable for four seconds, or when the text has stopped changing for 2.5 seconds while the site is not generating. If generation happened but produced nothing readable, it gives up after 10 seconds, or 45 while a status line such as "Creating image" shows. If the page shows no sign of life at all for 60 seconds after sending, the request fails instead of waiting out the deadline. The deadline is 420 seconds, extended up to 900 while an answer is still growing.

Status lines that sites show in place of an answer ("Creating your image", "Analyzing image") are never returned as the answer. Only short text can be one of those, so a real answer that happens to begin with "Analyzing" is not swallowed.

## Providers

A provider is mostly declarative: its site's URL, its profile folder, the selectors for the composer and the send button, and the files it uploads through. It supplies a few reads of its own: the answer's text, whether it is still generating, the state of an image, and the images themselves. The generic machinery for opening, uploading, typing, sending and confirming the send lives in the base class. A new backend means a new provider, not changes to the server.

ChatGPT's selectors are lists that keep each page layout the site has used, so a page serving either one still works; a redesign in October 2026 is why. ChatGPT renders code as editors whose text reshapes when an answer finishes, so its answers are sent in one piece at the end rather than streamed. Gemini's answers sit in a shadow DOM and stream as they type.

Account history (listing, reading and deleting a ChatGPT account's conversations) runs as JavaScript inside the signed-in page, against the site's own backend API, so a redesign of the sidebar does not break it. It always reuses the one open tab: a second live ChatGPT tab in the same profile makes the next answer read empty.

## The package

| Module | Does |
|---|---|
| `server.py` | the FastAPI app, attachments, the completion loop, image saving, history endpoints |
| `providers/` | `base.py` (shared machinery, completion tracker), `chatgpt.py`, `gemini.py`, `limits.py` (usage limits to HTTP 429) |
| `design.py` | design kinds and their prompts, sets, tokens, review |
| `gen_asset.py` | single image assets cut to exact pixels |
| `client.py` | the standard-library client and `ask()` |
| `mcp_server.py` | the MCP stdio server, a thin client of the HTTP API |
| `authz.py` | who may call what: the API key, loopback and origin rules |
| `config.py` | the `.env` file and the 2.0 setting names |
| `cli.py` | the `browser-llm` command |
| `login.py` | signing in on a visible display |
| `webui/` | the dashboard, widget and demo page |

## What the tests cover

The unit tests need no browser and no network, so they run anywhere in a few seconds. They cover the completion tracker, the access rules, the attachment layer, the history and delete layer and its refusal of unsafe file paths, the MCP protocol, the ChatGPT selector lists, usage limits, the design kinds and set runner, the `.env` file and the command. A passing run says the logic is sound. It cannot say the sites still look the way the selectors expect; only a real request can.
