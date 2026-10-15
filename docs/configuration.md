# Configuration

Every setting is an environment variable. Put them in a `.env` file in the checkout, export them in a shell, or set them in a systemd drop-in for the service. When the same setting is in more than one place, the environment wins over the file.

## The .env file

On start, the package reads the first `.env` it finds: the path in `BROWSER_LLM_ENV_FILE`, then `.env` in the current directory, then `.env` in the checkout. Lines are `KEY=VALUE`; `export` in front, quotes around a value and `#` comments are all fine, and nothing is expanded. `BROWSER_LLM_NO_DOTENV=1` skips the file. `.env.example` lists every setting with its default, and `.env` is ignored by git, so a key in it stays on your machine.

```bash
cp .env.example .env      # then edit
```

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `DEFAULT_PROVIDER` | `chatgpt-browser` | provider for a request whose `model` is unknown or missing |
| `IMAGE_PROVIDERS` | every provider that can draw | comma list of providers allowed to make images; others answer 403 |
| `CHATGPT_MODEL` | `GPT-6` | ChatGPT picker row selected before each request |
| `CHATGPT_EFFORT` | `high` | ChatGPT power: `instant`, `medium` or `high` |
| `CHATGPT_IMAGE_TOOL` | `1` | put the composer in Create image mode for image requests; `0` asks in words only |
| `CHATGPT_IMAGE_RESERVE` | `0` | images on ChatGPT's counter that requests here must leave for the account owner |
| `CHATGPT_READ_MODEL` | `1` | read the conversation record after each answer to report `model_used` |
| `BROWSER_LLM_HOST` | `127.0.0.1` | interface to bind; use `0.0.0.0` only together with an API key |
| `BROWSER_LLM_PORT` | `8081` | port to bind |
| `BROWSER_LLM_API_KEY` | unset | key required from callers on other machines on `/v1/*` and `/api/*` |
| `BROWSER_LLM_IMAGE_DIR` | `~/Pictures/browser-llm` | folder for saved images, one subfolder per provider |
| `BROWSER_LLM_PUBLIC_URL` | `http://localhost:8081` | base of the image links the API returns; set it to this machine's address when others use the links |
| `BROWSER_LLM_EPHEMERAL` | `0` | delete every chat's conversation afterwards unless the request says otherwise |
| `BROWSER_LLM_PREWARM` | `1` | start the default provider's browser at startup; `0` starts none until a request needs one |
| `BROWSER_RECYCLE_AFTER_IMAGES` | `8` | restart a provider's browser after this many images |
| `MAX_ATTACHMENTS` | `6` | most images one request may attach |
| `MAX_ATTACHMENT_MB` | `20` | size limit per attachment |
| `ALLOW_REMOTE_FILE_PATHS` | unset | let callers on other machines or other origins attach files by server path |
| `BROWSER_LLM_ENV_FILE` | unset | read this `.env` file instead of looking for one |
| `BROWSER_LLM_NO_DOTENV` | unset | `1` reads no `.env` file |

Settings for the clients (the CLI, the asset and design tools, the MCP server, the desktop app):

| Setting | Default | Meaning |
|---|---|---|
| `BROWSER_LLM_API` | `http://localhost:8081` | the server to talk to |
| `BROWSER_LLM_MODEL` | `chatgpt-browser` | model for `browser-llm ask` |
| `BROWSER_LLM_IMAGE_MODEL` | `chatgpt-browser` | model for `browser-llm image` and design sets |
| `BROWSER_LLM_MCP_TIMEOUT` | `440` | seconds an MCP tool call waits |

Settings for the watchdog: `BLM_ALERT_DIR` writes each alert as a Markdown file into a folder, and `BLM_ALERT_CMD` runs a command per alert with `BLM_ALERT_SEVERITY`, `BLM_ALERT_TITLE`, `BLM_ALERT_BODY` and `BLM_ALERT_KIND` set. See [Running it](running.md#watchdog).

### Names that changed in 2.0

| Before 2.0 | Now |
|---|---|
| `GEMINI_IMAGE_DIR`, `IMAGE_DIR` | `BROWSER_LLM_IMAGE_DIR` |
| `GEMINI_PUBLIC_URL` | `BROWSER_LLM_PUBLIC_URL` |
| `GEMINI_API` | `BROWSER_LLM_API` |
| `REMOTE_PROVIDERS`, `REMOTE_API_KEY` | removed; see below |

The old names still work, and the server logs once which one it found, so nothing breaks on upgrade.

## Who can call it

CORS is open on purpose, because the embeddable widget has to work from any page on your network. That has a consequence: while the server runs, any website open in your browser can send it requests, and they arrive from `127.0.0.1` like any local client. So:

- Treat the port as local trust. Keep the default `127.0.0.1` bind unless you mean to share it, and set `BROWSER_LLM_API_KEY` when you do.
- Callers on the same machine need no key; callers on other machines do. Pages, images and the UI's fonts (`/`, `/widget.js`, `/demo`, `/images/...`, `/assets/...`) stay public so links work in a plain browser, and saved image names are unguessable.
- Attaching a file by its path on the server needs a caller on the same machine and the same origin. A path makes the browser upload that file, so a page on another site naming a private file would otherwise read it. A request with no `Origin` header (curl, the CLI, the desktop app) counts as local. `ALLOW_REMOTE_FILE_PATHS=1` turns the check off.
- The history and delete endpoints answer only the server's own origin, because they read and delete a real account's conversations.
- Stop the service when you are not using it, if you would rather not have the endpoint live at all.

## Using it from another machine

Version 2.0 removed forwarding a model from one install to another. Another machine now calls this server's API directly:

```bash
# On the machine with the signed-in sessions (in .env, or a systemd drop-in):
BROWSER_LLM_HOST=0.0.0.0
BROWSER_LLM_API_KEY=<a long random key, e.g. from: openssl rand -hex 24>
BROWSER_LLM_PUBLIC_URL=http://<this machine's address>:8081

# On the other machine:
export BROWSER_LLM_API=http://<that address>:8081
export BROWSER_LLM_API_KEY=<the same key>
browser-llm ask "hello"            # or any OpenAI client with base_url and api_key set
```

Allow the port only from your own network in the firewall, and change the key if it may have leaked. Across networks, a private overlay such as Tailscale works the same way with its addresses.
