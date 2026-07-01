# Gemini Browser API

Drives the **Gemini web UI** through an automated Chrome browser ([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and exposes it as an **OpenAI-compatible API**. No official Google API key — it uses a logged-in Gemini web session.

- **`server.py`** — FastAPI server on port **8081** (`/v1/chat/completions` streaming + non-streaming, `/v1/models`).
- **`gemini_bot.py`** — standalone single-prompt prototype (hardcoded prompt → saves the answer).
- **`login_gemini.py`** — interactive re-auth helper (see below).
- **`gemini-api.service`** — systemd `--user` unit to run the server in the background.

## How it works

The server keeps one persistent Chrome instance (profile in `gemini_profile/`, gitignored). On each request it opens Gemini, types the prompt, and reads the streamed answer out of the response element by walking the shadow DOM. Completion is detected via a CDP network signal (the `BardFrontendService` stream finishing) with a DOM-stability fallback. Requests are serialized (one at a time) by a global lock.

## Requirements

- Google Chrome, Python 3.12
- `pip install nodriver fastapi uvicorn pydantic`
- Chrome runs **non-headless** on purpose (Gemini blocks true headless). The background service renders to a virtual **Xvfb** display (via `xvfb-run`), so no real monitor is needed. Re-auth uses a real display (e.g. `DISPLAY=:1`). Requires the `xvfb` package.

## Run once (foreground)

```bash
python3 server.py            # serves http://localhost:8081/v1
# or the standalone prototype:
python3 gemini_bot.py
```

## Run in the background (systemd --user)

The unit runs the server under `xvfb-run`, so Chrome renders to an invisible virtual display — no window appears and no login session is required (with linger enabled it survives logout).

```bash
mkdir -p ~/.config/systemd/user
cp gemini-api.service ~/.config/systemd/user/
# edit ExecStart / DISPLAY / WorkingDirectory in the unit if your paths differ
systemctl --user daemon-reload
systemctl --user enable --now gemini-api.service
loginctl enable-linger "$USER"      # keep it running after logout
```

Manage it:

```bash
systemctl --user status gemini-api
systemctl --user restart gemini-api
journalctl --user -u gemini-api -f  # live logs (the server.log file stays empty; the journal is the log)
```

## Authentication (important)

Everything depends on a logged-in Google session stored in `gemini_profile/`. When answers come back empty or you see the "Meet Gemini" / "Sign in" landing page, the session has expired.

**Re-auth must be done in the automation's own browser, on a real display.** The background service's Chrome is invisible (Xvfb), so you can't sign in there — the helper below opens a visible browser instead. Also, `nodriver` launches Chrome with `--password-store=basic`; a normal Chrome uses the system keyring, and cookies written by one cannot be decrypted by the other. So do **not** sign in with a plain `google-chrome` — use the helper:

```bash
systemctl --user stop gemini-api
DISPLAY=:1 python3 login_gemini.py   # a Chrome window opens — sign in; it auto-detects and closes
systemctl --user start gemini-api
```

## Caveats

- **Fragile by nature** — it depends on Gemini's DOM/aria-labels (`model-response`, `Send message`, etc.). A Google UI change can break extraction.
- **One request at a time** (global lock); concurrent callers queue.
- **`usage` token counts are approximate** (word-split, not a real tokenizer).
- A hard crash can leave a stale `gemini_profile/SingletonLock`; the unit clears it on start (`ExecStartPre`).
