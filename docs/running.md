# Running it

The server keeps one Chrome per provider, with its own signed-in profile in the checkout (`chatgpt_profile/`, `gemini_profile/`, never committed). Both sites block Chrome's headless mode, so a background run uses an invisible Xvfb display instead. Run commands from the checkout, because the profiles are found relative to it.

## The service

```bash
scripts/install-service.sh
```

That builds the venv, writes a `systemd --user` service and a watchdog timer for this checkout, starts both, and turns on lingering so they survive logout.

```bash
systemctl --user status browser-llm-api
systemctl --user restart browser-llm-api
journalctl --user -u browser-llm-api -f     # the journal is the log
```

Before a restart, check that nothing is in flight. A restart cuts any answer being read, and the caller still gets a 200 with a partial reply:

```bash
browser-llm health          # "idle" for every provider means it is safe
```

Settings for the service go in `.env` or in a systemd drop-in (`systemctl --user edit browser-llm-api`); see [Configuration](configuration.md).

`./serve.sh` runs the same thing in the foreground. It uses the venv, rebuilding it if needed, clears stale browser locks from a crash, and picks the display: the current `DISPLAY` when it works, else its own Xvfb.

## The display

ChatGPT draws images on a canvas, so it needs an X display; any Xvfb display works. The service runs on whatever `DISPLAY` its environment sets.

On a desktop machine, `scripts/mode.sh visible` runs the service's Chrome on your real display, `scripts/mode.sh headless` on a private Xvfb, and `scripts/mode.sh` shows which.

**Do not cover or move the service's Chrome windows.** Chrome treats a fully covered or off-screen window as hidden, and a hidden page gets no animation frames. Both sites then take the prompt and never render an answer. The server starts Chrome with `--disable-backgrounding-occluded-windows`, restores and raises a hidden window before each request, and fails a request that shows no sign of life within 60 seconds. Keep that flag.

## Signing in

Each provider needs its own login, stored in its own profile. Sign in with the helper, on a visible display:

```bash
systemctl --user stop browser-llm-api
DISPLAY=:1 ./venv/bin/browser-llm login chatgpt      # or: gemini
systemctl --user start browser-llm-api
```

A Chrome window opens on the same profile the server uses; sign in, and it detects the session and closes itself. Use the helper, not your everyday Chrome: the automation's Chrome stores cookies with `--password-store=basic`, and a normal Chrome uses the system keyring, so neither can read the other's cookies.

## Watchdog

The installer also starts `browser-llm-health.timer`, which runs `scripts/healthcheck.sh` every 10 minutes. It checks three things that fail independently:

- Integrity: can the venv still import the dependencies? A deleted or broken venv does not take the API down at once: the running process holds its imports and keeps serving, so the damage stays invisible until the next restart. In the incident behind this check, that gap was ten days, and the restart became a crash loop. The watchdog rebuilds the venv while the service is still healthy.
- Liveness: does `/api/status` answer? If not, it restarts the service once and alerts only if that fails.
- Sign-in: once a day it sends one short, self-deleting question per provider. An expired login is the most common failure, and the server stays healthy while returning nothing, so nothing cheaper can detect it.

It is silent when healthy and logs to `~/.local/state/browser-llm-api/health.log`. Alerts are off by default; `BLM_ALERT_DIR` and `BLM_ALERT_CMD` send them somewhere, and `notify-send` is used when installed. Each kind of alert has a cooldown.

```bash
systemctl --user list-timers browser-llm-health.timer
scripts/healthcheck.sh --deep       # run it now, including the daily question
```

## Troubleshooting

Read the page before guessing. Each provider's Chrome runs with a debugging port, which `pgrep -af 'user-data-dir=./chatgpt_profile'` shows. The page's `document.visibilityState`, its URL and its composer tell you more than an empty answer does.

| Symptom | Likely cause | What to do |
|---|---|---|
| Every answer empty, page shows a sign-in or "verify you're human" wall | the session expired | sign in again (above) |
| Every answer empty, page signed in, `visibilityState` is `hidden` | the window is covered or off screen | uncover it; keep the occlusion flag |
| `input box not found`, and the error says the page still holds an editable element | the site changed its page | update the selector lists in the provider; see [Contributing](../CONTRIBUTING.md#when-a-site-changes) |
| `input box not found`, and the page has no editable element | a sign-in wall, or the page did not load | sign in again, or check the network |
| Image responses report `image_tool: false` | the **+** menu or its Create image item moved | update the image-mode selectors in the ChatGPT provider |
| 429 with a reset time | the site's usage limit | wait for the reset |
| A long answer at GPT-6 High comes back empty | not yet explained (seen twice in October 2026) | read the log line "answer read failed"; retry at `GPT-6/medium` |
| The service will not start, "No module named nodriver" | a broken venv | `scripts/ensure-venv.sh` rebuilds it |
| Chrome will not start after a crash | a stale `SingletonLock` in the profile | `./serve.sh` clears it; otherwise delete `*_profile/Singleton*` |

The provider's browser restarts on its own when the connection to Chrome dies, so one dead browser fails at most one request.
