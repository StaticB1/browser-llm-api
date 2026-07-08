# Native Linux desktop app + tray widget

A **native GTK3 desktop client** for the Browser LLM API — not a browser tab. This is the
Linux-app counterpart to the web UI in `../webui/`.

The **tray widget** is built around one job: **generate quick image assets for whatever VS
Code project you're working in.** It auto-detects the focused VS Code window and drops
generated images straight into that project — remembering a save folder per project. It also
has a Chat tab, and chats are shared with the full app window and remembered across restarts.

| `webui/` (browser) | `desktop/` (this) |
|--------------------|-------------------|
| `index.html` — dashboard in a browser tab | **full app window** — Chat / Images / Gallery / Status |
| `widget.js` — chat bubble in *other web pages* | **tray indicator** — project-aware image popup + chat |

It talks to the same server (`http://localhost:8081` by default) over its OpenAI-compatible
HTTP API and contains **no browser-automation code** — that all stays in the server.

## The tray widget (project-aware images)

Click the top-bar icon (or middle-click) to open the popup. Two tabs:

- **Image** — the main event. A **Project** selector at the top shows the VS Code project you're
  in and **auto-follows the focused window** (a location button toggles following; pick from the
  dropdown to override/pin). Type a prompt → the image generates and is **saved into that
  project**. The **first** time you generate for a project it asks where to save (a folder
  picker); after that it remembers and saves there automatically. Files are named from the
  prompt (e.g. `settings-gear-icon-143512.png`), and **Open** / **Folder** buttons appear.
- **Chat** — a normal streaming chat.

> **How project detection works:** open VS Code windows and their real folder paths are read
> from `~/.config/Code/User/globalStorage/storage.json`; the *focused* window is resolved via
> `xdotool`. If `xdotool` isn't installed, auto-follow is disabled but you can still pick a
> project from the dropdown. No project open? Images just go to the server gallery.

## The full app window

`Chat` (streaming) · `Images` (same project-aware generation, larger) · `Gallery` (thumbnails
of saved images, click to view full-size) · `Status` (live per-provider telemetry). A **New
chat** button and a **History** menu (recent conversations) live in the header.

## Chats are shared & remembered

The popup and the window are two views of **one** conversation store, so **enlarging the popup
keeps your chat** (it no longer disappears). Conversations are persisted to
`~/.local/share/browser-llm-desktop/chats.json` and reloaded on launch. Per-project save
folders live in `projects.json` next to it.

## Requirements

Runs on the **system `python3`** (PyGObject ships on Ubuntu) — **no venv, no pip packages**,
standard library only. System packages (present by default on Ubuntu GNOME):

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libnotify-bin xdotool
```

- `gir1.2-ayatanaappindicator3-0.1` — the tray icon (needs a StatusNotifier host; GNOME's
  **AppIndicator extension** is enabled by default on Ubuntu). Without it the app just opens the
  window.
- `xdotool` — focus detection for auto-following the active VS Code window (optional; manual
  project selection works without it).

## Run

```bash
./desktop/run.sh                 # from the repo root — starts in the tray
# or directly:
/usr/bin/python3 desktop/browser_llm_desktop.py
```

Point it at a non-default server with `BROWSER_LLM_API=http://host:8081 ./desktop/run.sh`.

## Install a launcher (optional)

```bash
./desktop/install-desktop.sh              # adds "Browser LLM" to the app grid + its icon
./desktop/install-desktop.sh --uninstall  # remove
gtk-launch browser-llm-desktop            # launch after installing
```

Autostart at login: `cp ~/.local/share/applications/browser-llm-desktop.desktop ~/.config/autostart/`

## Files

```
browser_llm_desktop.py  # the whole app (stdlib + GTK3):
                        #   Api, ProjectManager (VS Code detection + per-project save folders),
                        #   ChatStore (shared + persisted conversations), ProjectImagePanel,
                        #   ChatPanel, GalleryPanel/ImageViewer, StatusPanel,
                        #   MainWindow, QuickChatWindow, TrayApp
icon.svg                # app / tray icon
run.sh                  # launch on system python3 (checks for gi, gives an install hint)
install-desktop.sh      # install/uninstall the .desktop launcher + icon (no root)
browser-llm-desktop.desktop.in  # launcher template (__APP__ -> absolute path at install)
```

## Notes

- **GTK3, not GTK4, on purpose:** the tray uses `AppIndicator`, which is a GTK3 library, and
  GTK3 + GTK4 cannot coexist in one process — so the whole app is one clean GTK3 process.
- The app **doesn't start the server** — run `./serve.sh` for that. If it's down, Status/tray
  say so and chat/image show a friendly error.
- ChatGPT answers arrive all-at-once (the server buffers that provider's stream); Gemini streams
  incrementally. ChatGPT image generation needs a real display (see the top-level README).
