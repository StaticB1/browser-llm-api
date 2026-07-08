# Native Linux desktop app + tray widget

A **native GTK3 desktop client** for the Browser LLM API — not a browser tab. This is the
Linux-app counterpart to the web UI in `../webui/`:

| `webui/` (already in the repo) | `desktop/` (this) |
|--------------------------------|-------------------|
| `index.html` — dashboard in a browser tab | **full app window** — Chat / Images / Gallery / Status tabs |
| `widget.js` — chat bubble embedded in *other web pages* | **tray indicator** — icon in the GNOME top bar with a Quick-chat popup |

It talks to the same server (`http://localhost:8081` by default) over its OpenAI-compatible
HTTP API. It contains **no browser-automation code** — all the `nodriver`/Chrome machinery
stays in the server; this is a thin front-end.

## What you get

* **Tray indicator** (the "widget") — a top-bar icon. Its menu:
  * **Quick chat** — a compact, always-on-top popup chat (middle-click the icon also opens it)
  * **Open Browser LLM** — the full app window
  * **Provider** — switch between `gemini-browser` / `chatgpt-browser` (kept in sync everywhere)
  * a live **status line** (online / offline / which provider is busy)
  * **Open web dashboard**, **Quit**
* **Full app window** — `Chat` (streaming), `Images` (generate + inline preview + elapsed
  timer), `Gallery` (thumbnails of saved images, click to view full-size), `Status` (live
  per-provider telemetry, same data as the web Status tab).

## Requirements

Runs on the **system `python3`** (which ships PyGObject on Ubuntu) — **no venv, no pip
packages**, standard library only. You need these system packages (present by default on
Ubuntu GNOME):

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin
```

> The tray icon needs a StatusNotifier host. On GNOME that's the **AppIndicator extension**
> (`gnome-shell-extension-appindicator`, enabled by default on Ubuntu). Without it the icon
> won't show and the app falls back to just opening the window.

## Run

```bash
./desktop/run.sh                 # from the repo root — starts in the tray
# or directly:
/usr/bin/python3 desktop/browser_llm_desktop.py
```

Point it at a non-default server with `BROWSER_LLM_API=http://host:8081 ./desktop/run.sh`.

## Install a launcher (optional)

Adds a **Browser LLM** entry to the GNOME app grid + its icon:

```bash
./desktop/install-desktop.sh              # install
./desktop/install-desktop.sh --uninstall  # remove
gtk-launch browser-llm-desktop            # launch after installing
```

To start it automatically at login, copy the installed launcher into autostart:

```bash
cp ~/.local/share/applications/browser-llm-desktop.desktop ~/.config/autostart/
```

## Files

```
browser_llm_desktop.py        # the whole app (stdlib + GTK3): Api client, ChatPanel,
                              #   ImagePanel, GalleryPanel/ImageViewer, StatusPanel,
                              #   MainWindow, QuickChatWindow, TrayApp
icon.svg                      # app / tray icon (chat bubble + AI sparkle)
run.sh                        # launch on system python3 (checks for gi, gives install hint)
install-desktop.sh            # install/uninstall the .desktop launcher + icon (no root)
browser-llm-desktop.desktop.in# launcher template (__APP__ -> absolute path at install)
```

## Notes

* **GTK3, not GTK4, on purpose:** the tray uses `AppIndicator`, which is a GTK3 library, and
  GTK3 + GTK4 cannot coexist in one process — so the whole app is one clean GTK3 process.
* The app **doesn't start the server** — run `./serve.sh` for that. If the server is down,
  the Status tab and tray say so and chat shows a friendly error.
* ChatGPT answers arrive **all-at-once** (the server buffers that provider's stream); Gemini
  streams incrementally. That's the server's behaviour, faithfully reflected here.
