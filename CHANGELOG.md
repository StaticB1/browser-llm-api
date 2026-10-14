# Changelog

All notable changes, newest first. Versions follow [semantic versioning](https://semver.org/): a major version can break how you install or call the project, and the notes say what to change.

## 2.0.0 — 2026-10-14

A reorganized, installable package with one command, and a tighter scope.

### Upgrading from 0.x

- Files moved: the code is now the `browser_llm` package; scripts are in `scripts/`, service templates in `deploy/systemd/`, and the guides in `docs/`. The top-level `gen_asset.py`, `design.py`, `client.py`, `serve.sh`, `mcp.sh` and `healthcheck.sh` stay as forwarding files, so a service, MCP config or script that calls them by path keeps working. New installs should use the new paths.
- One command: `browser-llm serve | login | ask | image | design | mcp | health | version` replaces running the scripts. `python -m browser_llm` is the same. The `browser-llm` console script used to start the server directly; it now takes a subcommand, so `browser-llm serve` is the old behaviour.
- Renamed settings: `BROWSER_LLM_IMAGE_DIR` replaces `GEMINI_IMAGE_DIR` and `IMAGE_DIR`, `BROWSER_LLM_PUBLIC_URL` replaces `GEMINI_PUBLIC_URL`, and `BROWSER_LLM_API` replaces `GEMINI_API`. The old names still work and the server logs which one it found.
- Forwarding between installs is removed: `REMOTE_PROVIDERS` and `REMOTE_API_KEY` are gone, and a server that still sets `REMOTE_PROVIDERS` logs that it is ignored. Another machine now calls this API directly with `BROWSER_LLM_API_KEY` (see [Configuration](docs/configuration.md#using-it-from-another-machine)).
- The original single-prompt prototype, `gemini_bot.py`, is removed.
- Imports: `from client import ask` still works from the checkout; new code should use `from browser_llm.client import ask`.

### Added

- A `.env` file beside the checkout, read on start; the real environment wins over it. `.env.example` lists every setting.
- `BROWSER_LLM_PREWARM=0`, which starts no browser until a request needs one.
- `browser-llm health`, a one-screen summary of the running server.
- A wheel and source archive that install the package with the dashboard and widget inside it.
- Docs reorganized into task pages, plus this changelog, a contributor guide, a release checklist, and issue and pull-request templates.

### Changed

- Pillow is a core dependency, since design sets and measured colours need it.
- `~` in the image folder setting is expanded wherever it comes from.

## 0.5.0 — 2026-10-14

Design with ChatGPT's image tool, and everything since 0.4.0.

- Design tools: thirteen design kinds (website sections, pages, hero and share images, app screens, components, brand boards, icons, logo concepts, illustrations, diagrams, textures, avatars). Sets make one image per section in one design system and resume after a stop. Tokens read the design system back out as CSS; review compares a build screenshot with its design. Available as commands, API fields, MCP tools and a dashboard presets row.
- Create image mode: ChatGPT image requests switch the composer into the site's own image mode first; responses report `prompt_sent` and `image_tool`.
- ChatGPT Plus: the default provider and the image provider; model and power from the picker (`CHATGPT_MODEL`, `CHATGPT_EFFORT`, `model_variant`); the model that answered reported as `model_used`; usage limits turned into HTTP 429 with the reset time; an image reserve for the account owner.
- ChatGPT's October 2026 page: Selectors keep both layouts; prompts keep their line breaks; code answers are fenced whole.
- Fixes: several attachments no longer go to a single-file camera input first, which had sent two references as three images. A hidden or covered browser window no longer empties every answer. A deleted venv is rebuilt instead of crash-looping. A failed answer read is logged instead of turning silently into an empty answer.
- History: the dashboard imports the account's own conversations, and deleting a chat deletes its saved images and the conversation on the site. Tooling requests can delete their conversation afterwards (`ephemeral`).

## 0.4.0 — 2026-08-18

- An MCP server, so agent clients use the sessions as native tools.
- Continuous integration, a security policy and a lint gate.

## 0.3.0 — 2026-08-12

- Security: a request from another site's page, which arrives over loopback, may no longer attach local files by path.
- A redesigned web UI.

## 0.2.0 — 2026-07-28

- Image input on every surface: vision questions and image-to-image editing.
- A native Linux tray app that makes image assets for the focused VS Code project.
- API-key access for callers on other machines, and forwarding a model to another install (removed in 2.0).
- A browser that dies is replaced instead of failing every request until a restart.

## 0.1.0 — 2026-07-08

- First release: an OpenAI-compatible API over Gemini and ChatGPT web sessions, with image generation, a web dashboard with telemetry, an embeddable chat widget, and packaging.
