#!/usr/bin/env python3
"""
Browser LLM — native Linux desktop app + tray widget.

A GTK3 client for the local Browser LLM API (the OpenAI-compatible server in this
repo, default http://localhost:8081). It does NOT drive any browser itself — all the
nodriver/Chrome machinery lives in the server; this is a thin, native front-end.

It provides:

  * a **tray indicator** (the "widget") — a top-bar icon whose popup is built around
    **quick image generation for whatever VS Code project you're in**: it auto-detects
    the focused VS Code window, and drops generated images straight into that project
    (remembering a save folder per project). It also has a Chat tab.
  * a **full app window** — Chat / Images / Gallery / Status tabs.

Chats are **shared** between the popup and the window (enlarging keeps your conversation)
and **persisted** to ~/.local/share/browser-llm-desktop/ so they survive restarts.

Runs on the SYSTEM python3 (which has PyGObject); no pip packages, no venv — only the
Python standard library plus system GTK3 + AppIndicator (+ xdotool for focus detection).

    /usr/bin/python3 desktop/browser_llm_desktop.py
    # or:  desktop/run.sh          # or install a launcher:  desktop/install-desktop.sh

Env:
    BROWSER_LLM_API   API base URL (default http://localhost:8081)
"""
import base64
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify
except (ValueError, ImportError):
    Notify = None
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango


# ---- tray library (AppIndicator is GTK3-only; try Ayatana first) -----------------
def _load_appindicator():
    for _name in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(_name, "0.1")
            mod = __import__("gi.repository", fromlist=[_name])
            return getattr(mod, _name)
        except (ValueError, ImportError, AttributeError):
            continue
    return None


AppIndicator3 = _load_appindicator()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_SVG = os.path.join(SCRIPT_DIR, "icon.svg")
APP_ID = "browser-llm-desktop"
APP_NAME = "Browser LLM"
DEFAULT_BASE = os.environ.get("BROWSER_LLM_API", "http://localhost:8081")
FALLBACK_MODELS = ["gemini-browser", "chatgpt-browser"]
VSCODE_STORAGE = os.path.expanduser("~/.config/Code/User/globalStorage/storage.json")
VSCODE_TITLE_SUFFIXES = (" - Visual Studio Code", " — Visual Studio Code",
                         " - Code - OSS", " - VSCodium")


def data_dir():
    d = os.path.join(GLib.get_user_data_dir(), APP_ID)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json_atomic(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        pass


# ============================== API client (stdlib) ==============================
class Api:
    """Blocking stdlib HTTP client. The GTK layer runs these on worker threads and
    marshals results back with GLib.idle_add()."""

    def __init__(self, base=None):
        self.base = (base or DEFAULT_BASE).rstrip("/")

    def _url(self, path):
        return path if path.startswith("http") else self.base + path

    def _get_json(self, path, timeout):
        with urllib.request.urlopen(self._url(path), timeout=timeout) as r:
            return json.load(r)

    def status(self, timeout=8):
        return self._get_json("/api/status", timeout)

    def gallery(self, timeout=8):
        return self._get_json("/api/gallery", timeout)

    def models(self, timeout=5):
        return [m["id"] for m in self._get_json("/v1/models", timeout)["data"]]

    def fetch_bytes(self, url, timeout=30):
        with urllib.request.urlopen(self._url(url), timeout=timeout) as r:
            return r.read()

    def chat_stream(self, messages, model, timeout=900):
        payload = {"model": model, "messages": messages, "stream": True}
        req = urllib.request.Request(
            self._url("/v1/chat/completions"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "ignore").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content", "")
                except Exception:
                    continue
                if delta:
                    yield delta

    def generate_image(self, prompt, model, timeout=900):
        payload = {"model": model, "prompt": prompt}
        req = urllib.request.Request(
            self._url("/v1/images/generations"),
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def image_bytes(self, item):
        """Turn an images/generations data[] item into raw bytes (b64 / local path / URL)."""
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("path") and os.path.exists(item["path"]):
            with open(item["path"], "rb") as f:
                return f.read()
        if item.get("url"):
            return self.fetch_bytes(item["url"])
        return None


def humanize_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"server error {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
            return "server offline — start it with ./serve.sh"
        return f"network error: {reason}"
    return str(exc) or exc.__class__.__name__


# ============================== VS Code project detection ==============================
def vscode_open_projects():
    """[(name, path)] for every currently-open VS Code window, newest first."""
    out, seen = [], set()
    try:
        d = json.load(open(VSCODE_STORAGE))
        for w in d.get("windowsState", {}).get("openedWindows", []):
            uri = w.get("folder")
            if not uri or not uri.startswith("file://"):
                continue
            path = urllib.parse.unquote(uri[len("file://"):]).rstrip("/")
            if path and path not in seen:
                seen.add(path)
                out.append((os.path.basename(path) or path, path))
    except Exception:
        pass
    return out


def active_window_title():
    try:
        env = dict(os.environ)
        env.setdefault("DISPLAY", ":0")
        r = subprocess.run(["xdotool", "getactivewindow", "getwindowname"],
                           capture_output=True, text=True, timeout=1.5, env=env)
        return r.stdout.strip()
    except Exception:
        return ""


def vscode_focused_path(projects):
    """Match the focused window title against `projects` [(name,path)] -> path or None.
    Only returns for a VS Code window; matches by longest folder-basename suffix so
    project names that contain ' - ' still resolve."""
    title = active_window_title()
    head = None
    for suf in VSCODE_TITLE_SUFFIXES:
        if title.endswith(suf):
            head = title[:-len(suf)].strip().lstrip("●•*✳ ").strip()
            break
    if not head:
        return None
    best = None
    for _name, path in projects:
        b = os.path.basename(path)
        if head == b or head.endswith(" - " + b):
            if best is None or len(os.path.basename(best)) < len(b):
                best = path
    return best


class ProjectManager(GObject.GObject):
    """Tracks open VS Code projects, auto-follows the focused window, and remembers a
    save folder per project. Emits 'changed' when the current project / list changes."""
    __gsignals__ = {"changed": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.path = os.path.join(data_dir(), "projects.json")
        self.folders = {}      # project_path -> save folder
        self.auto = True       # follow the focused VS Code window
        self.current = None    # project path
        self._projects = []    # [(name, path)]
        self._load()
        self.refresh()
        paths = [p for _, p in self._projects]
        f = vscode_focused_path(self._projects)
        if self.auto and f:
            self.current = f
        if not self.current:
            self.current = f or (paths[0] if paths else None)

    def _load(self):
        d = load_json(self.path, {})
        self.folders = d.get("folders", {}) or {}
        self.auto = d.get("auto", True)
        self.current = d.get("current")

    def _save(self):
        save_json_atomic(self.path, {"folders": self.folders, "auto": self.auto,
                                     "current": self.current})

    def refresh(self):
        self._projects = vscode_open_projects()

    def projects(self):
        return list(self._projects)

    def name_of(self, path):
        for n, p in self._projects:
            if p == path:
                return n
        return os.path.basename(path) if path else "—"

    def set_current(self, path, manual=False):
        if manual:
            self.auto = False
        if path != self.current or manual:
            self.current = path
            self._save()
            self.emit("changed")

    def set_auto(self, on):
        self.auto = on
        if on:
            f = vscode_focused_path(self._projects)
            if f:
                self.current = f
        self._save()
        self.emit("changed")

    def poll(self):
        """Called every 2s from the GTK main loop. The actual work (re-reading
        storage.json, shelling out to xdotool) is offloaded to a worker thread so a
        slow/hanging xdotool can't freeze the UI (unlike a direct call, which would
        run on the main thread)."""
        threading.Thread(target=self._poll_worker, daemon=True).start()
        return True

    def _poll_worker(self):
        projects = vscode_open_projects()
        focused = vscode_focused_path(projects) if self.auto else None
        GLib.idle_add(self._poll_apply, projects, focused)

    def _poll_apply(self, projects, focused):
        self._projects = projects
        if self.auto and focused and focused != self.current:
            self.current = focused
            self._save()
            self.emit("changed")
        return False

    def folder_for(self, path):
        return self.folders.get(path)

    def set_folder(self, path, folder):
        self.folders[path] = folder
        self._save()


# ============================== chat store (shared + persisted) ==============================
class ChatStore(GObject.GObject):
    """The single source of truth for conversations. Owns streaming. Both the popup and
    the app window are views bound to it, so a chat is never lost by switching surface,
    and it's persisted to disk."""
    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),      # full re-render
        "delta": (GObject.SignalFlags.RUN_FIRST, None, (str,)),    # append streamed chunk
        "busy": (GObject.SignalFlags.RUN_FIRST, None, (bool, str)),  # (streaming, conversation id)
    }

    def __init__(self, state):
        super().__init__()
        self.state = state
        self.path = os.path.join(data_dir(), "chats.json")
        self.conversations = []
        self.current_id = None
        self.streaming = set()  # conversation ids with an in-flight request
        self._seq = 0          # disambiguates ids minted within the same millisecond
        self._load()
        if not self.conversations:
            self.new_conversation(emit=False)
        elif self.current_id not in [c["id"] for c in self.conversations]:
            self.current_id = self.conversations[0]["id"]

    # -- persistence --
    def _load(self):
        d = load_json(self.path, None)
        if d is None:
            self.conversations, self.current_id = [], None
        else:
            self.conversations = d.get("conversations", [])
            self.current_id = d.get("current_id")

    def _save(self):
        save_json_atomic(self.path, {"current_id": self.current_id,
                                     "conversations": self.conversations})

    # -- model --
    def _by_id(self, cid):
        for c in self.conversations:
            if c["id"] == cid:
                return c
        return None

    def current(self):
        return self._by_id(self.current_id)

    def new_conversation(self, emit=True):
        # Timestamp alone can collide when two conversations are minted within the
        # same millisecond (e.g. rapid "New chat" clicks); the counter guarantees
        # a unique id within this process regardless.
        self._seq += 1
        cid = "c%d-%d" % (int(time.time() * 1000), self._seq)
        conv = {"id": cid, "title": "New chat", "model": self.state.model,
                "messages": [], "updated": time.time()}
        self.conversations.insert(0, conv)
        self.current_id = cid
        self._save()
        if emit:
            self.emit("changed")
        return conv

    def switch(self, cid):
        if cid != self.current_id and self._by_id(cid):
            self.current_id = cid
            conv = self.current()
            if conv and conv.get("model"):
                self.state.set_model(conv["model"])
            self._save()
            self.emit("changed")

    def is_streaming(self, cid):
        return cid in self.streaming

    def send(self, text):
        conv = self.current() or self.new_conversation()
        cid = conv["id"]
        if cid in self.streaming:
            return
        conv["messages"].append({"role": "user", "content": text})
        if conv["title"] in ("", "New chat"):
            conv["title"] = text.strip()[:44]
        conv["messages"].append({"role": "assistant", "content": ""})  # streaming placeholder
        conv["model"] = self.state.model
        conv["updated"] = time.time()
        self.streaming.add(cid)
        self.emit("changed")
        self.emit("busy", True, cid)
        self._save()
        msgs = [{"role": m["role"], "content": m["content"]} for m in conv["messages"][:-1]]
        threading.Thread(target=self._worker, args=(cid, msgs, self.state.model),
                         daemon=True).start()

    def _worker(self, cid, msgs, model):
        try:
            got = False
            for delta in self.state.api.chat_stream(msgs, model):
                got = True
                GLib.idle_add(self._on_delta, cid, delta)
            GLib.idle_add(self._on_done, cid, got, None)
        except Exception as e:
            GLib.idle_add(self._on_done, cid, False, humanize_error(e))

    def _on_delta(self, cid, delta):
        conv = self._by_id(cid)
        if conv and conv["messages"]:
            conv["messages"][-1]["content"] += delta
            if cid == self.current_id:
                self.emit("delta", delta)
        return False

    def _on_done(self, cid, got, err):
        conv = self._by_id(cid)
        if conv and conv["messages"]:
            last = conv["messages"][-1]
            if err:
                last["content"] = (last["content"] + f"\n⚠ {err}").strip()
                last["error"] = True
            elif not got and not last["content"]:
                last["content"] = "(no response — the session may need re-auth)"
                last["error"] = True
            conv["updated"] = time.time()
        self.streaming.discard(cid)
        self.emit("busy", False, cid)
        if err or not got:
            self.emit("changed")
        self._save()
        return False


# ============================== shared state ==============================
class AppState(GObject.GObject):
    __gsignals__ = {"model-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, api):
        super().__init__()
        self.api = api
        self.models = list(FALLBACK_MODELS)
        self.model = FALLBACK_MODELS[0]
        self.chats = ChatStore(self)
        self.projects = ProjectManager()

    def set_model(self, m):
        if m and m != self.model:
            self.model = m
            self.emit("model-changed", m)


# ============================== small helpers ==============================
def notify(title, body):
    if Notify is None:
        return
    try:
        if not Notify.is_initted():
            Notify.init(APP_NAME)
        Notify.Notification.new(title, body, APP_ID).show()
    except Exception:
        pass


def open_uri(uri):
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
    except Exception:
        try:
            subprocess.Popen(["xdg-open", uri])
        except Exception:
            pass


def screen_wh():
    disp = Gdk.Display.get_default()
    mon = disp.get_primary_monitor() or disp.get_monitor(0)
    g = mon.get_geometry()
    return g.width, g.height, g.x, g.y


def pixbuf_from_bytes(raw, max_w=0, max_h=0):
    loader = GdkPixbuf.PixbufLoader()
    loader.write(raw)
    loader.close()
    pb = loader.get_pixbuf()
    if pb is None:
        raise RuntimeError("could not decode image")
    if max_w and max_h:
        w, h = pb.get_width(), pb.get_height()
        if w > max_w or h > max_h:
            s = min(max_w / w, max_h / h)
            pb = pb.scale_simple(max(1, int(w * s)), max(1, int(h * s)),
                                 GdkPixbuf.InterpType.BILINEAR)
    return pb


def fmt_uptime(s):
    s = int(s or 0)
    h, m = s // 3600, (s % 3600) // 60
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s % 60:02d}s"
    return f"{s}s"


def slugify(text, maxwords=5):
    words = re.findall(r"[A-Za-z0-9]+", (text or "").lower())[:maxwords]
    return "-".join(words) or "image"


def ext_for_bytes(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


def unique_path(folder, base, ext):
    cand = os.path.join(folder, base + ext)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(folder, f"{base}-{i}{ext}")
        i += 1
    return cand


def make_provider_combo(state):
    combo = Gtk.ComboBoxText()
    for m in state.models:
        combo.append_text(m)
    if state.model in state.models:
        combo.set_active(state.models.index(state.model))
    guard = {"on": False}

    def on_changed(c):
        if guard["on"]:
            return
        t = c.get_active_text()
        if t:
            state.set_model(t)

    def on_state(_s, m):
        if m in state.models:
            guard["on"] = True
            combo.set_active(state.models.index(m))
            guard["on"] = False

    combo.connect("changed", on_changed)
    state.connect("model-changed", on_state)
    return combo


def make_project_combo(projects):
    """ComboBox of open VS Code projects, two-way bound to ProjectManager."""
    combo = Gtk.ComboBoxText()
    guard = {"on": False}

    def rebuild(*_):
        guard["on"] = True
        combo.remove_all()
        for name, path in projects.projects():
            combo.append(path, name)
        if projects.current:
            if combo.get_active_id() != projects.current:
                if not combo.set_active_id(projects.current):
                    # current project isn't in the open list — show it anyway
                    combo.append(projects.current, projects.name_of(projects.current) + " (closed)")
                    combo.set_active_id(projects.current)
        guard["on"] = False

    def on_changed(c):
        if guard["on"]:
            return
        pid = c.get_active_id()
        if pid:
            projects.set_current(pid, manual=True)

    combo.connect("changed", on_changed)
    projects.connect("changed", rebuild)
    rebuild()
    return combo


# ============================== reusable chat panel (view over ChatStore) ==============================
class ChatPanel(Gtk.Box):
    def __init__(self, state):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.state = state
        self.store = state.chats
        self._tail = False

        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_left_margin(10)
        self.view.set_right_margin(10)
        self.view.set_top_margin(8)
        self.view.set_bottom_margin(8)
        self.buf = self.view.get_buffer()
        self.buf.create_tag("user_label", foreground="#4f46e5", weight=700)
        self.buf.create_tag("ai_label", foreground="#7c3aed", weight=700)
        self.buf.create_tag("meta", foreground="#6b7280", style=Pango.Style.ITALIC, scale=0.92)
        self.buf.create_tag("error", foreground="#dc2626", weight=700)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.add(self.view)
        sw.set_vexpand(True)
        self.pack_start(sw, True, True, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Message…  (Enter to send)")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_send)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.send_btn = Gtk.Button.new_with_label("Send")
        self.send_btn.get_style_context().add_class("suggested-action")
        self.send_btn.connect("clicked", self._on_send)
        row.pack_start(self.entry, True, True, 0)
        row.pack_start(self.spinner, False, False, 0)
        row.pack_start(self.send_btn, False, False, 0)
        self.pack_start(row, False, False, 0)

        self.store.connect("changed", self._on_store_changed)
        self.store.connect("delta", lambda _s, d: self._append(d))
        self.store.connect("busy", lambda _s, b, cid: self._on_busy(cid))
        self.render()
        self._sync_busy()

    def _insert(self, text, *tags):
        end = self.buf.get_end_iter()
        if tags:
            self.buf.insert_with_tags_by_name(end, text, *tags)
        else:
            self.buf.insert(end, text)

    def _role(self, name, tag):
        if self.buf.get_char_count() > 0:
            self._insert("\n")
        self._insert(name + "\n", tag)

    def _scroll(self):
        self.buf.place_cursor(self.buf.get_end_iter())
        self.view.scroll_to_mark(self.buf.get_insert(), 0.0, True, 0, 1.0)

    def render(self):
        self.buf.set_text("")
        conv = self.store.current()
        msgs = conv["messages"] if conv else []
        if not msgs:
            self._insert("Ask anything — answered by the browser-driven LLM on this machine.\n",
                         "meta")
            self._tail = True
            return
        for m in msgs:
            if m["role"] == "user":
                self._role("You", "user_label")
            else:
                self._role("Assistant", "ai_label")
            if m.get("error"):
                self._insert(m["content"], "error")
            else:
                self._insert(m["content"])
        self._tail = True
        self._scroll()

    def _append(self, delta):
        if self._tail:
            self._insert(delta)
            self._scroll()

    def _on_store_changed(self, *_):
        self.render()
        self._sync_busy()

    def _on_busy(self, cid):
        if cid == self.store.current_id:
            self._sync_busy()

    def _sync_busy(self):
        self._set_busy(self.store.is_streaming(self.store.current_id))

    def _set_busy(self, b):
        self.entry.set_sensitive(not b)
        self.send_btn.set_sensitive(not b)
        if b:
            self.spinner.show()
            self.spinner.start()
        else:
            self.spinner.stop()
            self.spinner.hide()
            self.entry.grab_focus()

    def _on_send(self, *_):
        if self.store.is_streaming(self.store.current_id):
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.store.send(text)


# ============================== project-aware image panel ==============================
class ProjectImagePanel(Gtk.Box):
    """Generate an image and drop it into the active VS Code project's save folder
    (pick-and-remember). Degrades to just previewing + the server's copy if there's no
    project."""

    def __init__(self, state, compact=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.state = state
        self.projects = state.projects
        self.compact = compact
        self.busy = False
        self._t0 = None
        self._timer_id = None
        self._last_file = None
        self.set_border_width(8 if compact else 10)

        # project bar
        pbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        lbl = Gtk.Label(label="Project")
        lbl.get_style_context().add_class("dim-label")
        self.project_combo = make_project_combo(self.projects)
        self.project_combo.set_hexpand(True)
        self.follow = Gtk.ToggleButton()
        self.follow.set_image(Gtk.Image.new_from_icon_name("find-location-symbolic",
                                                           Gtk.IconSize.BUTTON))
        self.follow.set_tooltip_text("Follow the focused VS Code window")
        self.follow.set_active(self.projects.auto)
        self.follow.connect("toggled", lambda t: self.projects.set_auto(t.get_active()))
        pbar.pack_start(lbl, False, False, 0)
        pbar.pack_start(self.project_combo, True, True, 0)
        pbar.pack_start(self.follow, False, False, 0)
        self.pack_start(pbar, False, False, 0)

        # prompt row
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Describe an image for this project…")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_gen)
        self.gen = Gtk.Button.new_with_label("Generate")
        self.gen.get_style_context().add_class("suggested-action")
        self.gen.connect("clicked", self._on_gen)
        row.pack_start(self.entry, True, True, 0)
        row.pack_start(self.gen, False, False, 0)
        self.pack_start(row, False, False, 0)

        # status row
        srow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.status = Gtk.Label(label="")
        self.status.set_xalign(0)
        self.status.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.open_file = Gtk.Button.new_with_label("Open")
        self.open_file.set_sensitive(False)
        self.open_file.connect("clicked", lambda *_: self._last_file and open_uri("file://" + self._last_file))
        self.open_dir = Gtk.Button.new_with_label("Folder")
        self.open_dir.set_sensitive(False)
        self.open_dir.connect("clicked", lambda *_: self._last_file and open_uri("file://" + os.path.dirname(self._last_file)))
        srow.pack_start(self.spinner, False, False, 0)
        srow.pack_start(self.status, True, True, 0)
        srow.pack_start(self.open_file, False, False, 0)
        srow.pack_start(self.open_dir, False, False, 0)
        self.pack_start(srow, False, False, 0)

        # preview
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        self.image = Gtk.Image()
        self.image.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
        sw.add(self.image)
        self.pack_start(sw, True, True, 0)

        self.projects.connect("changed", lambda *_: self._refresh_hint())
        self._refresh_hint()

    def _refresh_hint(self):
        self.follow.set_active(self.projects.auto)
        cur = self.projects.current
        if cur:
            saved = self.projects.folder_for(cur)
            where = os.path.relpath(saved, cur) + "/" if saved else "you'll pick a folder"
            self.status.set_text(f"→ {self.projects.name_of(cur)}  ({where})")
        else:
            self.status.set_text("No VS Code project detected — open one, or images save to the gallery only.")

    def _ensure_folder(self, project):
        """Return the save folder for `project`, prompting once (pick & remember)."""
        if not project:
            return None
        saved = self.projects.folder_for(project)
        if saved and os.path.isdir(saved):
            return saved
        dlg = Gtk.FileChooserDialog(
            title=f"Where should images for “{self.projects.name_of(project)}” be saved?",
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.set_transient_for(self.get_toplevel())
        dlg.set_modal(True)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("Save here", Gtk.ResponseType.ACCEPT)
        if os.path.isdir(project):
            dlg.set_current_folder(project)
        folder = dlg.get_filename() if dlg.run() == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        if folder:
            self.projects.set_folder(project, folder)
        return folder

    def _on_gen(self, *_):
        if self.busy:
            return
        prompt = self.entry.get_text().strip()
        if not prompt:
            return
        # Capture the target project now — generation can take tens of seconds to
        # minutes, during which auto-follow may move self.projects.current elsewhere;
        # the result must still be attributed to the project it was requested for.
        project = self.projects.current
        folder = None
        if project:
            folder = self._ensure_folder(project)
            if folder is None and self.projects.folder_for(project) is None:
                self.status.set_text("Cancelled — no folder chosen.")
                return
        model = self.state.model or FALLBACK_MODELS[0]
        self.busy = True
        self.gen.set_sensitive(False)
        self.open_file.set_sensitive(False)
        self.open_dir.set_sensitive(False)
        self.spinner.show()
        self.spinner.start()
        self._t0 = time.monotonic()
        self.status.set_text(f"Generating with {model}…  0s")
        self._timer_id = GLib.timeout_add_seconds(1, self._tick, model)
        threading.Thread(target=self._worker, args=(prompt, model, folder, project),
                         daemon=True).start()

    def _tick(self, model):
        if not self.busy:
            return False
        self.status.set_text(f"Generating with {model}…  {int(time.monotonic() - self._t0)}s")
        return True

    def _worker(self, prompt, model, folder, project):
        try:
            res = self.state.api.generate_image(prompt, model)
            data = (res or {}).get("data") or []
            if not data:
                raise RuntimeError("server returned no image")
            item = data[0]
            raw = self.state.api.image_bytes(item)
            if raw is None:
                raise RuntimeError("could not load the generated image")
            saved_path = None
            if folder:
                name = slugify(prompt) + "-" + time.strftime("%H%M%S")
                saved_path = unique_path(folder, name, ext_for_bytes(raw))
                with open(saved_path, "wb") as f:
                    f.write(raw)
            server_link = item.get("path") or item.get("url") or ""
            GLib.idle_add(self._on_result, raw, saved_path, server_link, project)
        except Exception as e:
            GLib.idle_add(self._on_error, humanize_error(e))

    def _local_viewable_copy(self, raw, server_link):
        """A path Open/Folder can always use, regardless of where the server runs.
        Prefers the server's own path/url if it happens to already exist on this
        filesystem, otherwise writes the (already-fetched) bytes to a temp file —
        so viewing still works against a remote server or with no project folder."""
        if server_link and os.path.exists(server_link):
            return server_link
        try:
            fd, path = tempfile.mkstemp(prefix="browser-llm-", suffix=ext_for_bytes(raw))
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            return path
        except Exception:
            return None

    def _finish(self):
        self.busy = False
        self.spinner.stop()
        self.spinner.hide()
        self.gen.set_sensitive(True)
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _on_result(self, raw, saved_path, server_link, project):
        self._finish()
        secs = int(time.monotonic() - self._t0)
        try:
            self.image.set_from_pixbuf(pixbuf_from_bytes(raw, 300 if self.compact else 820,
                                                         300 if self.compact else 820))
        except Exception as e:
            self.status.set_text(f"generated but could not display: {e}")
            return False
        if saved_path:
            rel = os.path.relpath(saved_path, project) if project else saved_path
            self.status.set_text(f"✓ Saved to {rel}  ({secs}s)")
            self._last_file = saved_path
            self.open_file.set_sensitive(True)
            self.open_dir.set_sensitive(True)
            notify(APP_NAME, f"Image saved to {self.projects.name_of(project)}/{rel}")
        else:
            self.status.set_text(f"✓ Generated in {secs}s · saved to gallery ({server_link})")
            self._last_file = self._local_viewable_copy(raw, server_link)
            self.open_file.set_sensitive(self._last_file is not None)
            self.open_dir.set_sensitive(self._last_file is not None)
        return False

    def _on_error(self, msg):
        self._finish()
        self.status.set_text(f"⚠ {msg}")
        return False


# ============================== gallery panel ==============================
class GalleryPanel(Gtk.Box):
    def __init__(self, state):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.state = state
        self._loaded = False
        self.set_border_width(10)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.count = Gtk.Label(label="Saved images")
        self.count.set_xalign(0)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda *_: self.load())
        bar.pack_start(self.count, True, True, 0)
        bar.pack_start(refresh, False, False, 0)
        self.pack_start(bar, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_max_children_per_line(5)
        self.flow.set_column_spacing(8)
        self.flow.set_row_spacing(8)
        self.flow.set_homogeneous(True)
        sw.add(self.flow)
        self.pack_start(sw, True, True, 0)

        self.connect("map", lambda *_: self._maybe_load())

    def _maybe_load(self):
        if not self._loaded:
            self.load()

    def load(self):
        self._loaded = True
        for child in self.flow.get_children():
            self.flow.remove(child)
        self.count.set_text("Loading…")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            items = (self.state.api.gallery().get("images") or [])[:60]
            GLib.idle_add(self._populate, items)
        except Exception as e:
            GLib.idle_add(self.count.set_text, f"⚠ {humanize_error(e)}")

    def _populate(self, items):
        self.count.set_text(f"{len(items)} image(s)" if items else "No saved images yet")
        for it in items:
            btn = Gtk.Button()
            btn.set_relief(Gtk.ReliefStyle.NONE)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            img = Gtk.Image()
            img.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
            img.set_size_request(150, 150)
            lbl = Gtk.Label(label=it.get("provider", "?"))
            lbl.get_style_context().add_class("dim-label")
            box.pack_start(img, False, False, 0)
            box.pack_start(lbl, False, False, 0)
            btn.add(box)
            kb = int(it.get("bytes", 0) / 1024)
            btn.set_tooltip_text(f"{it.get('file', '')}  ·  {kb} KB")
            url = it.get("url", "")
            btn.connect("clicked", lambda _b, u=url, f=it.get("file", ""): self._open(u, f))
            self.flow.insert(btn, -1)
            threading.Thread(target=self._thumb, args=(url, img), daemon=True).start()
        self.flow.show_all()
        return False

    def _thumb(self, url, img):
        try:
            raw = self.state.api.fetch_bytes(url)
            pb = pixbuf_from_bytes(raw, 150, 150)
            GLib.idle_add(img.set_from_pixbuf, pb)
        except Exception:
            pass

    def _open(self, url, title):
        ImageViewer(self.state.api, title or "Image", url=url)


class ImageViewer(Gtk.Window):
    def __init__(self, api, title, url=None, raw=None):
        super().__init__(title=title)
        sw_w, sw_h, _, _ = screen_wh()
        self.set_default_size(min(820, int(sw_w * 0.8)), min(700, int(sw_h * 0.8)))
        self._max = (int(sw_w * 0.85), int(sw_h * 0.8))
        sw = Gtk.ScrolledWindow()
        self.img = Gtk.Image()
        sw.add(self.img)
        self.add(sw)
        if raw is not None:
            self._set(raw)
        elif url:
            self.img.set_from_icon_name("image-loading-symbolic", Gtk.IconSize.DIALOG)
            threading.Thread(target=self._load, args=(api, url), daemon=True).start()
        self.show_all()

    def _load(self, api, url):
        try:
            raw = api.fetch_bytes(url)
            GLib.idle_add(self._set, raw)
        except Exception:
            GLib.idle_add(self.img.set_from_icon_name, "image-missing-symbolic", Gtk.IconSize.DIALOG)

    def _set(self, raw):
        try:
            self.img.set_from_pixbuf(pixbuf_from_bytes(raw, *self._max))
        except Exception:
            self.img.set_from_icon_name("image-missing-symbolic", Gtk.IconSize.DIALOG)
        return False


# ============================== status panel ==============================
class StatusPanel(Gtk.Box):
    def __init__(self, state):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.state = state
        self._tid = None
        self.set_border_width(14)

        self.header = Gtk.Label()
        self.header.set_use_markup(True)
        self.header.set_xalign(0)
        self.header.set_line_wrap(True)
        self.pack_start(self.header, False, False, 0)

        self.body = Gtk.Label()
        self.body.set_use_markup(True)
        self.body.set_xalign(0)
        self.body.set_yalign(0)
        self.body.set_selectable(True)
        self.body.set_line_wrap(True)
        self.pack_start(self.body, True, True, 0)

        self.connect("map", lambda *_: self._start())
        self.connect("unmap", lambda *_: self._stop())

    def _start(self):
        self.refresh()
        if self._tid is None:
            self._tid = GLib.timeout_add_seconds(3, self._tick)

    def _tick(self):
        self.refresh()
        return True

    def _stop(self):
        if self._tid:
            GLib.source_remove(self._tid)
            self._tid = None

    def refresh(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            st = self.state.api.status()
        except Exception:
            st = None
        GLib.idle_add(self.render, st)

    def render(self, st):
        esc = GLib.markup_escape_text
        if not st:
            self.header.set_markup(
                "<span size='x-large' weight='bold' foreground='#dc2626'>● Server offline</span>\n"
                "Start it with <tt>./serve.sh</tt>, then this updates automatically.")
            self.body.set_markup("")
            return False
        self.header.set_markup(
            f"<span size='x-large' weight='bold' foreground='#16a34a'>● Online</span>  "
            f"<span foreground='#6b7280'>Browser LLM API v{esc(str(st.get('version', '?')))}</span>\n"
            f"uptime {fmt_uptime(st.get('uptime_seconds'))} · display {esc(str(st.get('display', '?')))} · "
            f"default <b>{esc(str(st.get('default_provider', '?')))}</b>\n"
            f"images → <tt>{esc(str(st.get('image_dir', '?')))}</tt> "
            f"(saving: {'on' if st.get('image_saving') else 'off'})")
        blocks = []
        for name, p in (st.get("providers") or {}).items():
            running = p.get("browser_running")
            dot = "🟢" if running else "⚪"
            busy = "busy" if p.get("busy") else "idle"
            deflabel = "  <span foreground='#6b7280'>(default)</span>" if p.get("default") else ""
            ll, al = p.get("last_latency"), p.get("avg_latency")
            lat = f"last {ll:.1f}s · avg {al:.1f}s" if ll is not None else "—"
            err = p.get("last_error")
            errline = (f"\n     last error: <span foreground='#dc2626'>{esc(str(err)[:90])}</span>"
                       if err else "")
            blocks.append(
                f"<b>{esc(name)}</b>{deflabel}\n"
                f"     {dot} browser {'running' if running else 'stopped'} · {busy}\n"
                f"     requests {p.get('requests', 0)} · errors {p.get('errors', 0)} · {lat}\n"
                f"     recycle {p.get('images_since_recycle', 0)}/{p.get('recycle_after_images', 0)} images"
                f"{errline}")
        self.body.set_markup("\n\n".join(blocks))
        return False


# ============================== full app window ==============================
class MainWindow(Gtk.Window):
    def __init__(self, state):
        super().__init__(title=APP_NAME)
        self.state = state
        self.set_default_size(800, 660)
        self.connect("delete-event", self._on_delete)

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(APP_NAME)
        hb.set_subtitle("browser-driven LLM")
        new_chat = Gtk.Button.new_from_icon_name("document-new-symbolic", Gtk.IconSize.BUTTON)
        new_chat.set_tooltip_text("New chat")
        new_chat.connect("clicked", lambda *_: self.state.chats.new_conversation())
        history = Gtk.Button.new_from_icon_name("document-open-recent-symbolic", Gtk.IconSize.BUTTON)
        history.set_tooltip_text("Chat history")
        history.connect("clicked", self._show_history)
        hb.pack_start(new_chat)
        hb.pack_start(history)
        prov_lbl = Gtk.Label(label="Provider")
        prov_lbl.get_style_context().add_class("dim-label")
        hb.pack_end(make_provider_combo(state))
        hb.pack_end(prov_lbl)
        self.set_titlebar(hb)

        self.nb = Gtk.Notebook()
        self.chat = ChatPanel(state)
        self.chat.set_border_width(10)
        self.nb.append_page(self.chat, Gtk.Label(label="Chat"))
        self.nb.append_page(ProjectImagePanel(state), Gtk.Label(label="Images"))
        self.nb.append_page(GalleryPanel(state), Gtk.Label(label="Gallery"))
        self.nb.append_page(StatusPanel(state), Gtk.Label(label="Status"))
        self._tabs = {"chat": 0, "images": 1, "gallery": 2, "status": 3}
        self.add(self.nb)

    def _show_history(self, btn):
        menu = Gtk.Menu()
        chats = self.state.chats
        if not chats.conversations:
            mi = Gtk.MenuItem.new_with_label("(no chats yet)")
            mi.set_sensitive(False)
            menu.append(mi)
        for c in chats.conversations[:25]:
            mark = "●  " if c["id"] == chats.current_id else "     "
            title = (c.get("title") or "New chat")[:50]
            mi = Gtk.MenuItem.new_with_label(mark + title)
            mi.connect("activate", lambda _m, cid=c["id"]: (chats.switch(cid),
                                                            self.nb.set_current_page(0)))
            menu.append(mi)
        menu.show_all()
        menu.popup_at_widget(btn, Gdk.Gravity.SOUTH_WEST, Gdk.Gravity.NORTH_WEST, None)

    def _on_delete(self, *_):
        self.hide()
        return True

    def present_window(self, tab=None):
        self.show_all()
        if tab in self._tabs:
            self.nb.set_current_page(self._tabs[tab])
        self.present()


# ============================== quick popup (the widget) ==============================
class QuickChatWindow(Gtk.Window):
    def __init__(self, state, on_expand=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.state = state
        self.on_expand = on_expand
        self.set_decorated(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_default_size(390, 580)
        self.connect("delete-event", lambda *_: self.hide() or True)
        self.connect("key-press-event", self._on_key)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.get_style_context().add_class("blm-popup")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        head.get_style_context().add_class("blm-pop-header")
        head.set_border_width(6)
        title = Gtk.Label(label=APP_NAME)
        title.get_style_context().add_class("blm-pop-title")
        title.set_xalign(0)
        combo = make_provider_combo(state)
        expand = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
        expand.set_relief(Gtk.ReliefStyle.NONE)
        expand.set_tooltip_text("Open full app (keeps this chat)")
        expand.connect("clicked", self._on_expand)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.hide())
        head.pack_start(title, True, True, 0)
        head.pack_start(combo, False, False, 0)
        head.pack_start(expand, False, False, 0)
        head.pack_start(close, False, False, 0)
        outer.pack_start(head, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.image_panel = ProjectImagePanel(state, compact=True)
        self.stack.add_titled(self.image_panel, "image", "Image")

        chat_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        chat_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chat_bar.set_border_width(6)
        new_btn = Gtk.Button.new_from_icon_name("document-new-symbolic", Gtk.IconSize.BUTTON)
        new_btn.set_tooltip_text("New chat")
        new_btn.set_relief(Gtk.ReliefStyle.NONE)
        new_btn.connect("clicked", lambda *_: self.state.chats.new_conversation())
        chat_bar.pack_end(new_btn, False, False, 0)
        chat_box.pack_start(chat_bar, False, False, 0)
        self.chat = ChatPanel(state)
        self.chat.set_border_width(8)
        chat_box.pack_start(self.chat, True, True, 0)
        self.stack.add_titled(chat_box, "chat", "Chat")

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_halign(Gtk.Align.CENTER)
        switcher.set_border_width(4)
        outer.pack_start(switcher, False, False, 0)
        outer.pack_start(self.stack, True, True, 0)
        self.add(outer)
        self.stack.set_visible_child_name("image")

    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def _on_expand(self, *_):
        page = self.stack.get_visible_child_name()
        self.hide()
        if self.on_expand:
            self.on_expand("images" if page == "image" else "chat")

    def toggle(self):
        if self.get_visible():
            self.hide()
        else:
            self.show_all()
            self.present()
            w, _h = self.get_size()
            sw, _sh, sx, sy = screen_wh()
            self.move(sx + sw - w - 12, sy + 44)


# ============================== tray + application ==============================
CSS = b"""
.blm-pop-header { background-image: linear-gradient(105deg,#6366f1,#a855f7); }
.blm-pop-header * { color: #ffffff; }
.blm-pop-title { font-weight: bold; font-size: 12pt; }
.blm-popup { border: 1px solid rgba(0,0,0,0.25); }
"""


class TrayApp:
    def __init__(self):
        self.api = Api()
        self.state = AppState(self.api)
        self._bootstrap()

        self._load_css()
        pb = self._app_pixbuf()
        if pb:
            Gtk.Window.set_default_icon(pb)

        self.main = MainWindow(self.state)
        self.quick = QuickChatWindow(self.state, on_expand=self.main.present_window)

        self._build_indicator()
        GLib.timeout_add_seconds(6, self._poll)
        self._poll()
        GLib.timeout_add_seconds(2, self.state.projects.poll)

    def _bootstrap(self):
        try:
            st = self.api.status(timeout=4)
            provs = list((st.get("providers") or {}).keys())
            if provs:
                self.state.models = provs
            default = st.get("default_provider")
            self.state.model = default if default in self.state.models else self.state.models[0]
        except Exception:
            try:
                m = self.api.models(timeout=4)
                if m:
                    self.state.models = m
                    self.state.model = m[0]
            except Exception:
                pass

    def _load_css(self):
        try:
            prov = Gtk.CssProvider()
            prov.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        except Exception:
            pass

    def _app_pixbuf(self, size=64):
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_size(ICON_SVG, size, size)
        except Exception:
            return None

    def _tray_icon(self):
        cache = os.path.join(GLib.get_user_cache_dir(), APP_ID)
        try:
            os.makedirs(cache, exist_ok=True)
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(ICON_SVG, 48, 48)
            pb.savev(os.path.join(cache, APP_ID + ".png"), "png", [], [])
            return APP_ID, cache
        except Exception:
            return "applications-internet", None

    def _build_indicator(self):
        if AppIndicator3 is None:
            notify(APP_NAME, "Tray unavailable; opening the app window.")
            self.main.present_window()
            return

        name, path = self._tray_icon()
        ind = AppIndicator3.Indicator.new(
            APP_ID, name, AppIndicator3.IndicatorCategory.APPLICATION_STATUS)
        if path:
            ind.set_icon_theme_path(path)
        ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        ind.set_title(APP_NAME)

        menu = Gtk.Menu()
        mi_quick = Gtk.MenuItem.new_with_label("🎨  Quick image / chat")
        mi_quick.connect("activate", lambda *_: self.quick.toggle())
        menu.append(mi_quick)

        mi_open = Gtk.MenuItem.new_with_label("🗔  Open Browser LLM")
        mi_open.connect("activate", lambda *_: self.main.present_window())
        menu.append(mi_open)

        menu.append(Gtk.SeparatorMenuItem())

        prov = Gtk.MenuItem.new_with_label("Provider")
        sub = Gtk.Menu()
        prov.set_submenu(sub)
        first = None
        self._prov_items = {}
        for m in self.state.models:
            ri = Gtk.RadioMenuItem.new_with_label_from_widget(first, m)
            if first is None:
                first = ri
            if m == self.state.model:
                ri.set_active(True)
            ri.connect("toggled", self._on_prov_toggled, m)
            sub.append(ri)
            self._prov_items[m] = ri
        self.state.connect("model-changed", self._on_model_changed)
        menu.append(prov)

        self.mi_project = Gtk.MenuItem.new_with_label("Project: …")
        self.mi_project.set_sensitive(False)
        menu.append(self.mi_project)
        self.state.projects.connect("changed", lambda *_: self._update_project_label())
        self._update_project_label()

        self.mi_status = Gtk.MenuItem.new_with_label("…")
        self.mi_status.set_sensitive(False)
        menu.append(self.mi_status)

        menu.append(Gtk.SeparatorMenuItem())

        mi_web = Gtk.MenuItem.new_with_label("Open web dashboard")
        mi_web.connect("activate", lambda *_: open_uri(self.api.base))
        menu.append(mi_web)

        mi_quit = Gtk.MenuItem.new_with_label("Quit")
        mi_quit.connect("activate", lambda *_: self._quit())
        menu.append(mi_quit)

        menu.show_all()
        ind.set_menu(menu)
        try:
            ind.set_secondary_activate_target(mi_quick)
        except Exception:
            pass
        self.indicator = ind
        notify(APP_NAME, "Running in the tray — click the icon for quick project images.")

    def _update_project_label(self):
        if hasattr(self, "mi_project"):
            cur = self.state.projects.current
            label = self.state.projects.name_of(cur) if cur else "none open"
            follow = " (following)" if self.state.projects.auto else ""
            self.mi_project.set_label(f"Project: {label}{follow}")

    def _on_prov_toggled(self, item, model):
        if item.get_active():
            self.state.set_model(model)

    def _on_model_changed(self, _state, model):
        item = getattr(self, "_prov_items", {}).get(model)
        if item and not item.get_active():
            item.set_active(True)

    def _poll(self):
        threading.Thread(target=self._poll_worker, daemon=True).start()
        return True

    def _poll_worker(self):
        try:
            st = self.api.status(timeout=5)
        except Exception:
            st = None
        GLib.idle_add(self._poll_update, st)

    def _poll_update(self, st):
        if not hasattr(self, "mi_status"):
            return False
        if not st:
            self.mi_status.set_label("● server offline")
            return False
        busy = [n for n, p in (st.get("providers") or {}).items() if p.get("busy")]
        self.mi_status.set_label("● busy: " + ", ".join(busy) if busy else "● online · idle")
        return False

    def _quit(self):
        if Notify is not None:
            try:
                Notify.uninit()
            except Exception:
                pass
        Gtk.main_quit()


def main():
    if Notify is not None:
        try:
            Notify.init(APP_NAME)
        except Exception:
            pass
    TrayApp()
    Gtk.main()


if __name__ == "__main__":
    main()
