# API

The server speaks OpenAI's HTTP API at `http://localhost:8081/v1`, plus a few endpoints of its own for the dashboard. Requests from the same machine need no key. A request from another machine needs the key set in `BROWSER_LLM_API_KEY`, sent as `Authorization: Bearer <key>` or `X-Api-Key: <key>` (see [Configuration](configuration.md#who-can-call-it)).

## Choosing a provider and a model

The OpenAI `model` field picks the provider: `chatgpt-browser` or `gemini-browser`. An unknown or missing `model` falls back to `DEFAULT_PROVIDER` (default `chatgpt-browser`), and `GET /v1/models` lists both.

ChatGPT requests set the composer's model picker before sending. `CHATGPT_MODEL` (default `GPT-6`) names the picker row, and `CHATGPT_EFFORT` (`instant`, `medium` or `high`, default `high`) the power slider. One request can choose differently with `model_variant`, for example `"instant"`, `"medium"` or `"GPT-6/medium"`; the next request without it goes back to the setting.

Every ChatGPT answer reports `model_used`, read from the conversation's own record on chatgpt.com:

```json
"model_used": {"model": "gpt-6-thinking", "effort": "extended", "resolved": "gpt-6-thinking",
               "source": "conversation record", "requested_by_page": "gpt-6-thinking",
               "picker": "GPT-6 / High"}
```

The site routes image turns itself. Measured on 13 and 14 October 2026, images made at High came from `gpt-5-4-thinking` whatever the picker said.

## Chat completions

`POST /v1/chat/completions` takes OpenAI's body: `model`, `messages` (system, user and assistant turns) and `stream`. A streamed answer arrives as server-sent events. ChatGPT answers arrive in one chunk at the end, because its code blocks change shape as they finish; Gemini streams as it types.

```bash
curl http://localhost:8081/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "chatgpt-browser",
  "messages": [{"role": "user", "content": "Explain CORS in two sentences"}]
}'
```

Extra fields this server understands:

| Field | Meaning |
|---|---|
| `model_variant` | ChatGPT model and power for this request (see above) |
| `ephemeral` | `true` deletes the conversation from the account once the answer is read |
| `images` | a shorthand list of image specs, attached alongside any in the messages |

**Ephemeral requests.** The sessions are signed in as a person, so every call lands in that person's chat history. `"ephemeral": true` deletes the conversation as soon as the answer has been read, with the same soft delete the site's own Delete performs. A failed delete never fails the request. Without the field, `BROWSER_LLM_EPHEMERAL` decides (default off). Image requests are never deleted, because an asset usually comes with a thread worth keeping. The MCP `ask` tool sets it by default.

A multi-turn `messages` array is flattened into one prompt, with labelled turns, and sent to a fresh chat. `usage` token counts are word counts, not a tokenizer's.

## Sending images in

Images go in as OpenAI vision content parts, as `image_url` strings, as Anthropic-style base64 blocks, or through the `images` shorthand. The server uploads them through the site's own file picker before it types the prompt.

```bash
curl http://localhost:8081/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "chatgpt-browser",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "What is wrong with this layout?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGg..."}}
  ]}]}'
```

An image spec can be a `data:` URL, bare base64, an `http(s)` URL (downloaded by the server), a `file://` URL or a local path. Limits are `MAX_ATTACHMENTS` (default 6) and `MAX_ATTACHMENT_MB` (default 20) each. Temporary files are deleted after the request; your own files are never touched.

Local paths are accepted only from a caller on the same machine and the same origin. A path makes the browser upload that file, so a caller on the network, or a web page open in your browser, could otherwise read any file on this machine. `ALLOW_REMOTE_FILE_PATHS=1` turns that check off.

## Image generation

`POST /v1/images/generations` makes one image with the provider's own image tool.

```bash
curl http://localhost:8081/v1/images/generations -H 'Content-Type: application/json' \
  -d '{"model":"chatgpt-browser","prompt":"a red bicycle on a beach at sunset","size":"1600x900"}'
```

| Field | Meaning |
|---|---|
| `prompt` | what to draw; with `kind`, the brief |
| `size` | `WxH`, `square`, `landscape` or `portrait`, asked for in words |
| `background` | `transparent` asks for a PNG with an alpha channel |
| `model_variant` | ChatGPT model and power for this image |
| `image`, `images` | reference images, which make it image-to-image |
| `kind` | a design kind such as `section`, `mobile-screen` or `icon` ([Images and design](images-and-design.md)) |
| `direction`, `theme`, `platform`, `system`, `copy_space` | design options for a `kind` |
| `response_format` | `b64_json` (default) or `url` |

The answer:

```json
{"created": 1791960645,
 "data": [{"b64_json": "...", "width": 1672, "height": 941, "mime": "image/png",
           "url": "http://localhost:8081/images/chatgpt/chatgpt_1791960645_c6acf2605eda0_050a6670.png",
           "path": "/home/you/Pictures/browser-llm/chatgpt/chatgpt_1791960645_c6acf2605eda0_050a6670.png",
           "revised_prompt": "..."}],
 "model_used": {"model": "gpt-5-4-thinking", "picker": "GPT-6 / High"},
 "prompt_sent": "Use the image generation tool to create this image. ...",
 "image_tool": true}
```

`width` and `height` are the image as the site delivered it. `prompt_sent` is the exact text typed into the site, and `image_tool` says whether the composer was in Create image mode. `n` is accepted and ignored: one request makes one image.

`POST /v1/images/edits` takes OpenAI's multipart upload, or the same JSON body as generations with `image` or `images`:

```bash
curl http://localhost:8081/v1/images/edits -F model=chatgpt-browser -F image=@logo.png \
  -F 'prompt=the same logo as a flat vector on a deep navy background'
```

Generated images are saved under `BROWSER_LLM_IMAGE_DIR` (default `~/Pictures/browser-llm`), one subfolder per provider, and served at `/images/<provider>/<file>`. In a chat, an image the model makes comes back as Markdown pointing at the saved file.

## Other endpoints

| Endpoint | What it returns |
|---|---|
| `GET /v1/models` | the provider ids |
| `GET /version` | name and version |
| `GET /api/status` | uptime, settings, and per provider: browser state, busy, telemetry, model setting, image counter |
| `GET /api/gallery?provider=&limit=` | saved images, newest first |
| `GET /api/design/kinds` | the design kinds with their default size and background |
| `GET /api/history`, `GET /api/conversation/{id}` | the account's conversation list, and one conversation |
| `POST /api/conversations/delete`, `POST /api/gallery/delete` | delete conversations from the account, and saved images from disk |
| `GET /`, `/widget.js`, `/demo`, `/images/...` | the dashboard, the widget, its demo page, saved images |

The four history and delete endpoints reach into a signed-in account, so they answer only callers on the server's own origin. A page on another site gets 403, even from this machine. Only ChatGPT exposes a conversation list this server can read; Gemini answers 501.

## Errors

| Status | Meaning |
|---|---|
| 400 | a malformed request, an unknown `kind`, or a file path from a caller that may not send one |
| 401 | a request from another machine without the right key |
| 403 | images are not allowed from this provider (`IMAGE_PROVIDERS`), or a cross-origin call to a history endpoint |
| 429 | the site's usage limit; `Retry-After` and the detail give the reset time. Stop and wait. |
| 501 | the provider cannot do this (no image tool, no readable history) |
| 502 | the site returned no image, or the drive failed; the detail quotes what the page said |

A streamed chat cannot change its status once it has started, so a failure mid-stream arrives as a final chunk reading `[browser-llm error: ...]`.
