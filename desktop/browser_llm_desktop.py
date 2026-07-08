#!/usr/bin/env python3
"""
Browser LLM — native Linux desktop app + tray widget.

A GTK3 client for the local Browser LLM API (the OpenAI-compatible server in this
repo, default http://localhost:8081). It does NOT drive any browser itself — all the
nodriver/Chrome machinery lives in the server; this is a thin, native front-end.

It provides two things:

  * a **tray indicator** (the "widget") — an icon in the GNOME top bar with a menu:
    Quick chat, Open the full app, pick a provider, live server status, Quit;
  * a **full app window** — Chat / Images / Gallery / Status tabs, mirroring the web
    dashboard but as a real desktop window.

Runs on the SYSTEM python3 (which has PyGObject); it needs no pip packages and no
venv — only the Python standard library plus system GTK3 + AppIndicator.

    /usr/bin/python3 desktop/browser_llm_desktop.py
    # or:  desktop/run.sh          # or install a launcher:  desktop/install-desktop.sh

Env:
    BROWSER_LLM_API   API base URL (default http://localhost:8081)
"""
import base64
import json
import os
import subprocess
import threading
import time
import urllib.error
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
        """Yield assistant text deltas from an SSE stream."""
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


def humanize_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return f"server error {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError) or "refused" in str(reason).lower():
            return "server offline — start it with ./serve.sh"
        return f"network error: {reason}"
    return str(exc) or exc.__class__.__name__


# ============================== shared state ==============================
class AppState(GObject.GObject):
    __gsignals__ = {"model-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, api):
        super().__init__()
        self.api = api
        self.models = list(FALLBACK_MODELS)
        self.model = FALLBACK_MODELS[0]

    def set_model(self, m):
        if m and m != self.model:
            self.model = m
            self.emit("model-changed", m)


# ============================== small helpers ==============================
def notify(title, body):
    if Notify is None:
        return
    try:
        # libnotify hard-aborts (SIGABRT, not a catchable exception) if .show() runs
        # before init — so always ensure init here, not only in main().
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


def make_provider_combo(state):
    """A ComboBoxText bound two-way to AppState.model (kept in sync across combos)."""
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


# ============================== reusable chat panel ==============================
class ChatPanel(Gtk.Box):
    """Transcript + input row with streaming. Used by both the popup and the app."""

    def __init__(self, state, system=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.state = state
        self.system = system
        self.history = []
        self.streaming = False
        self._has_hint = False

        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        for setter in ("set_left_margin", "set_right_margin"):
            getattr(self.view, setter)(10)
        self.view.set_top_margin(8)
        self.view.set_bottom_margin(8)
        self.buf = self.view.get_buffer()
        self._init_tags()

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

        self._show_hint()

    def _init_tags(self):
        self.buf.create_tag("user_label", foreground="#4f46e5", weight=700)
        self.buf.create_tag("ai_label", foreground="#7c3aed", weight=700)
        self.buf.create_tag("meta", foreground="#6b7280", style=Pango.Style.ITALIC, scale=0.92)
        self.buf.create_tag("error", foreground="#dc2626", weight=700)

    # ---- transcript primitives ----
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
        mark = self.buf.get_insert()
        self.buf.place_cursor(self.buf.get_end_iter())
        self.view.scroll_to_mark(mark, 0.0, True, 0, 1.0)

    def _show_hint(self):
        self._has_hint = True
        self._insert("Ask anything — answered by the browser-driven LLM on this machine.\n", "meta")

    def _clear_hint(self):
        if self._has_hint:
            self.buf.set_text("")
            self._has_hint = False

    def reset(self):
        self.history = []
        self.buf.set_text("")
        self._show_hint()
        self.entry.grab_focus()

    # ---- send / stream ----
    def _on_send(self, *_):
        if self.streaming:
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self._clear_hint()
        self.history.append({"role": "user", "content": text})
        self._role("You", "user_label")
        self._insert(text + "\n")
        self._scroll()
        self._start_stream()

    def _start_stream(self):
        self.streaming = True
        self.entry.set_sensitive(False)
        self.send_btn.set_sensitive(False)
        self.spinner.show()
        self.spinner.start()
        self._role("Assistant", "ai_label")
        self._got_text = False
        self._ai_text = ""
        self._scroll()
        msgs = list(self.history)
        if self.system:
            msgs = [{"role": "system", "content": self.system}] + msgs
        model = self.state.model or FALLBACK_MODELS[0]
        threading.Thread(target=self._worker, args=(msgs, model), daemon=True).start()

    def _worker(self, msgs, model):
        try:
            for delta in self.state.api.chat_stream(msgs, model):
                GLib.idle_add(self._on_delta, delta)
            GLib.idle_add(self._on_done)
        except Exception as e:
            GLib.idle_add(self._on_error, humanize_error(e))

    def _on_delta(self, delta):
        self._got_text = True
        self._ai_text += delta
        self._insert(delta)
        self._scroll()
        return False

    def _on_done(self):
        if not self._got_text:
            self._insert("(no response — the session may need re-auth)", "meta")
        else:
            self.history.append({"role": "assistant", "content": self._ai_text})
        self._insert("\n")
        self._end_stream()
        return False

    def _on_error(self, msg):
        self._insert(f"⚠ {msg}\n", "error")
        self._end_stream()
        return False

    def _end_stream(self):
        self.streaming = False
        self.spinner.stop()
        self.spinner.hide()
        self.entry.set_sensitive(True)
        self.send_btn.set_sensitive(True)
        self.entry.grab_focus()
        self._scroll()


# ============================== image generation panel ==============================
class ImagePanel(Gtk.Box):
    def __init__(self, state):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.state = state
        self.busy = False
        self._t0 = None
        self._timer_id = None
        self._last_raw = None
        self.set_border_width(10)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Describe an image to generate…")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_gen)
        self.gen = Gtk.Button.new_with_label("Generate")
        self.gen.get_style_context().add_class("suggested-action")
        self.gen.connect("clicked", self._on_gen)
        row.pack_start(self.entry, True, True, 0)
        row.pack_start(self.gen, False, False, 0)
        self.pack_start(row, False, False, 0)

        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.spinner = Gtk.Spinner()
        self.spinner.set_no_show_all(True)
        self.status = Gtk.Label(label="")
        self.status.set_xalign(0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        status.pack_start(self.spinner, False, False, 0)
        status.pack_start(self.status, True, True, 0)
        self.open_btn = Gtk.Button.new_with_label("Open full image")
        self.open_btn.set_sensitive(False)
        self.open_btn.connect("clicked", self._on_open)
        status.pack_start(self.open_btn, False, False, 0)
        self.pack_start(status, False, False, 0)

        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sw.set_vexpand(True)
        self.image = Gtk.Image()
        self.image.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
        sw.add(self.image)
        self.pack_start(sw, True, True, 0)

    def _on_gen(self, *_):
        if self.busy:
            return
        prompt = self.entry.get_text().strip()
        if not prompt:
            return
        model = self.state.model or FALLBACK_MODELS[0]
        self.busy = True
        self.gen.set_sensitive(False)
        self.open_btn.set_sensitive(False)
        self.spinner.show()
        self.spinner.start()
        self._t0 = time.monotonic()
        self.status.set_text(f"Generating with {model}…  0s")
        self._timer_id = GLib.timeout_add_seconds(1, self._tick, model)
        threading.Thread(target=self._worker, args=(prompt, model), daemon=True).start()

    def _tick(self, model):
        if not self.busy:
            return False
        self.status.set_text(f"Generating with {model}…  {int(time.monotonic() - self._t0)}s")
        return True

    def _worker(self, prompt, model):
        try:
            res = self.state.api.generate_image(prompt, model)
            data = (res or {}).get("data") or []
            if not data:
                raise RuntimeError("server returned no image")
            item = data[0]
            raw, link = None, item.get("url") or item.get("path")
            if item.get("b64_json"):
                raw = base64.b64decode(item["b64_json"])
            elif item.get("path") and os.path.exists(item["path"]):
                with open(item["path"], "rb") as f:
                    raw = f.read()
            elif item.get("url"):
                raw = self.state.api.fetch_bytes(item["url"])
            if raw is None:
                raise RuntimeError("could not load the generated image")
            GLib.idle_add(self._on_result, raw, str(link or ""))
        except Exception as e:
            GLib.idle_add(self._on_error, humanize_error(e))

    def _finish(self):
        self.busy = False
        self.spinner.stop()
        self.spinner.hide()
        self.gen.set_sensitive(True)
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _on_result(self, raw, link):
        self._finish()
        self._last_raw = raw
        try:
            self.image.set_from_pixbuf(pixbuf_from_bytes(raw, 900, 900))
        except Exception as e:
            self.status.set_text(f"loaded but could not display: {e}")
            return
        secs = int(time.monotonic() - self._t0)
        self.status.set_text(f"Done in {secs}s  ·  {link}" if link else f"Done in {secs}s")
        self.open_btn.set_sensitive(True)
        return False

    def _on_error(self, msg):
        self._finish()
        self.status.set_text(f"⚠ {msg}")
        return False

    def _on_open(self, *_):
        if not self._last_raw:
            return
        path = os.path.join(GLib.get_tmp_dir(), f"browser-llm-{int(time.monotonic() * 1000)}.png")
        try:
            with open(path, "wb") as f:
                f.write(self._last_raw)
            open_uri("file://" + path)
        except Exception:
            pass


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
        except Exception as e:
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
        self.set_default_size(780, 640)
        self.connect("delete-event", self._on_delete)

        hb = Gtk.HeaderBar()
        hb.set_show_close_button(True)
        hb.set_title(APP_NAME)
        hb.set_subtitle("browser-driven LLM")
        new_chat = Gtk.Button.new_from_icon_name("document-new-symbolic", Gtk.IconSize.BUTTON)
        new_chat.set_tooltip_text("New chat")
        new_chat.connect("clicked", lambda *_: self.chat.reset())
        hb.pack_start(new_chat)
        prov_lbl = Gtk.Label(label="Provider")
        prov_lbl.get_style_context().add_class("dim-label")
        hb.pack_end(make_provider_combo(state))
        hb.pack_end(prov_lbl)
        self.set_titlebar(hb)

        nb = Gtk.Notebook()
        self.chat = ChatPanel(state)
        self.chat.set_border_width(10)
        nb.append_page(self.chat, Gtk.Label(label="Chat"))
        nb.append_page(ImagePanel(state), Gtk.Label(label="Images"))
        nb.append_page(GalleryPanel(state), Gtk.Label(label="Gallery"))
        nb.append_page(StatusPanel(state), Gtk.Label(label="Status"))
        self.add(nb)

    def _on_delete(self, *_):
        self.hide()
        return True  # keep running in the tray

    def present_window(self):
        self.show_all()
        self.present()


# ============================== quick-chat popup (the widget) ==============================
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
        self.set_default_size(370, 480)
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
        expand.set_tooltip_text("Open full app")
        expand.connect("clicked", self._on_expand)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.BUTTON)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.hide())
        head.pack_start(title, True, True, 0)
        head.pack_start(combo, False, False, 0)
        head.pack_start(expand, False, False, 0)
        head.pack_start(close, False, False, 0)
        outer.pack_start(head, False, False, 0)

        self.chat = ChatPanel(state)
        self.chat.set_border_width(8)
        outer.pack_start(self.chat, True, True, 0)
        self.add(outer)

    def _on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    def _on_expand(self, *_):
        self.hide()
        if self.on_expand:
            self.on_expand()

    def toggle(self):
        if self.get_visible():
            self.hide()
        else:
            self.show_all()
            self.present()
            w, _h = self.get_size()
            sw, _sh, sx, sy = screen_wh()
            self.move(sx + sw - w - 12, sy + 44)
            self.chat.entry.grab_focus()


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

    def _bootstrap(self):
        """Discover providers + default from the live server (falls back gracefully)."""
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
        """Rasterize the SVG to a cached PNG for the indicator; fall back to a themed name."""
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
            # No tray library — degrade to just showing the app window.
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
        mi_quick = Gtk.MenuItem.new_with_label("💬  Quick chat")
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
            ind.set_secondary_activate_target(mi_quick)  # middle-click → quick chat
        except Exception:
            pass
        self.indicator = ind
        notify(APP_NAME, "Running in the tray — click the icon to chat.")

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
