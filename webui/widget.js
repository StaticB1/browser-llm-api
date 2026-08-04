/*
 * Browser-LLM embeddable chat widget.
 * ---------------------------------------------------------------------------
 * Drop into ANY page on the LAN:
 *
 *   <script src="http://localhost:8081/widget.js"></script>
 *
 * It injects a floating chat bubble (bottom-right) that streams from this
 * server's OpenAI-compatible /v1/chat/completions. The API base is discovered
 * from THIS script's own src, so the host page can live on any origin/port —
 * the server already sends open CORS headers.
 *
 * Optional config via data-* attributes on the <script> tag:
 *   data-provider  "gemini-browser" | "chatgpt-browser"   (default: server default)
 *   data-title     header text                            (default: "Ask AI")
 *   data-accent    CSS color for the bubble/buttons       (default: #2E8BFF)
 *   data-theme     "auto" | "light" | "dark"              (default: auto — follows
 *                  the host page's prefers-color-scheme, so the bubble never lands
 *                  dark-on-light)
 *   data-position  "br" | "bl"                            (default: br)
 *   data-greeting  first assistant line                   (default: friendly hi)
 *   data-system    a system prompt sent with every turn   (default: none)
 *   data-open      "1" to start expanded                  (default: closed)
 *   data-key       API key, sent as Bearer — needed when the server sets
 *                  BROWSER_LLM_API_KEY and this page isn't on the server box
 *   data-attach    "0" to hide the 📎 image-attach button (default: shown; the
 *                  server uploads attached images into the provider's chat)
 * All are also overridable at runtime via window.BrowserLLMWidget.config(...).
 * ---------------------------------------------------------------------------
 */
(function () {
  "use strict";
  if (window.__browserLlmWidget) return;          // guard against double-inject

  // ---- resolve this script tag + the API base from its src ----------------
  var script = document.currentScript;
  if (!script) {
    var all = document.getElementsByTagName("script");
    for (var i = all.length - 1; i >= 0; i--) {
      if (/widget\.js(\?|#|$)/.test(all[i].src)) { script = all[i]; break; }
    }
  }
  var src = (script && script.src) || "";
  // base = origin+path that served this file, minus "/widget.js[?...]"
  var base = src.replace(/\/widget\.js(\?.*)?(#.*)?$/, "");
  if (!base) base = window.BROWSER_LLM_BASE || location.origin;

  function attr(name, dflt) {
    if (!script) return dflt;
    var v = script.getAttribute("data-" + name);
    return (v === null || v === "") ? dflt : v;
  }
  var cfg = {
    base: base,
    provider: attr("provider", ""),          // "" → let the server pick its default
    title: attr("title", "Ask AI"),
    accent: attr("accent", "#2E8BFF"),
    theme: attr("theme", "auto"),
    position: attr("position", "br"),
    greeting: attr("greeting", "Hi! Ask me anything."),
    system: attr("system", ""),
    open: attr("open", "") === "1",
    key: attr("key", ""),
    attach: attr("attach", "1") !== "0",
  };
  var MAX_ATTACH = 6;

  function apiHeaders(h) {
    h = h || {};
    if (cfg.key) h["Authorization"] = "Bearer " + cfg.key;
    return h;
  }

  // ---- shadow-DOM host (style isolation both ways) ------------------------
  var host = document.createElement("div");
  host.id = "browser-llm-widget";
  host.style.cssText = "all:initial;position:fixed;z-index:2147483000;";
  var root = host.attachShadow({ mode: "open" });
  document.documentElement.appendChild(host);

  var sideProp = cfg.position === "bl" ? "left" : "right";

  /* Theme tokens. Light is the base; dark is applied either explicitly
     (data-theme="dark") or, in auto mode, from the HOST page's colour scheme —
     a dark bubble on a light site was the old default and looked broken. */
  var DARK_TOKENS =
    "--w-surface:#181B1F;--w-surface2:#20242A;--w-border:#343A42;--w-hairline:#272C33;" +
    "--w-text:#EEF2F5;--w-muted:#A0AAB5;--w-code:#14181D;--w-user:#1A3F6A;" +
    "--w-user-border:#2E8BFF;--w-err:#E55C63;--w-ink:#F8FBFF;" +
    "--w-shadow:0 18px 50px rgba(0,0,0,.55)";

  var style = document.createElement("style");
  style.textContent = [
    ":host{all:initial}",
    "*{box-sizing:border-box}",
    // ---- tokens: light base ----
    ".wrap{position:fixed;bottom:20px;" + sideProp + ":20px;",
    "  --w-surface:#FDFCF8;--w-surface2:#F1EFE9;--w-border:#C7C1B6;--w-hairline:#E2DDD3;",
    "  --w-text:#1D232B;--w-muted:#5F6974;--w-code:#F2F0EA;--w-user:#E8F2FF;",
    "  --w-user-border:#7DB7FF;--w-err:#C83F49;--w-ink:#FFFFFF;",
    "  --w-shadow:0 18px 50px rgba(20,24,28,.18);",
    "  font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}",
    ".wrap[data-t='dark']{" + DARK_TOKENS + "}",
    "@media (prefers-color-scheme: dark){.wrap[data-t='auto']{" + DARK_TOKENS + "}}",
    // launcher bubble
    ".fab{width:56px;height:56px;border-radius:50%;border:none;cursor:pointer;",
    "  background:var(--accent);color:var(--w-ink);font-size:26px;line-height:1;",
    "  box-shadow:0 6px 22px rgba(0,0,0,.28);display:flex;align-items:center;",
    "  justify-content:center;transition:transform .15s ease}",
    ".fab:hover{transform:scale(1.06)}",
    ".fab:active{transform:scale(.96)}",
    // panel
    ".panel{position:absolute;bottom:70px;" + sideProp + ":0;width:370px;max-width:calc(100vw - 32px);",
    "  height:520px;max-height:calc(100vh - 120px);display:none;flex-direction:column;",
    "  background:var(--w-surface);color:var(--w-text);border:1px solid var(--w-border);",
    "  border-radius:12px;overflow:hidden;box-shadow:var(--w-shadow)}",
    ".panel.open{display:flex}",
    ".hd{display:flex;align-items:center;gap:8px;padding:11px 13px;background:var(--w-surface2);",
    "  border-bottom:1px solid var(--w-border)}",
    ".hd b{font-weight:600;font-size:13px;letter-spacing:.04em;text-transform:uppercase;",
    "  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
    ".hd .sp{flex:1}",
    ".hd select{background:var(--w-surface);color:var(--w-muted);border:1px solid var(--w-border);",
    "  border-radius:6px;padding:3px 6px;font-size:11px;max-width:120px;",
    "  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
    ".hd button{background:none;border:none;color:var(--w-muted);cursor:pointer;font-size:20px;",
    "  line-height:1;padding:2px 4px;border-radius:6px}",
    ".hd button:hover{color:var(--w-text);background:var(--w-hairline)}",
    ".log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}",
    ".msg{max-width:88%;padding:9px 12px;border-radius:8px;border:1px solid var(--w-border);",
    "  overflow-wrap:anywhere;white-space:normal}",
    ".msg.user{align-self:flex-end;background:var(--w-user);border-color:var(--w-user-border)}",
    ".msg.bot{align-self:flex-start;background:var(--w-surface2)}",
    ".msg.err{border-color:var(--w-err);color:var(--w-err)}",
    ".msg pre{background:var(--w-code);border:1px solid var(--w-hairline);border-radius:6px;",
    "  padding:9px 11px;overflow-x:auto;font-size:12.5px;margin:6px 0}",
    ".msg code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
    ".msg :not(pre)>code{background:var(--w-code);border:1px solid var(--w-hairline);",
    "  border-radius:4px;padding:1px 5px;font-size:12.5px}",
    ".msg img{max-width:100%;border-radius:6px;margin-top:6px;display:block}",
    ".msg a{color:var(--accent)}",
    ".hint{color:var(--w-muted);font-size:12px}",
    ".cmp{display:flex;gap:8px;align-items:flex-end;padding:10px;",
    "  border-top:1px solid var(--w-border);background:var(--w-surface2)}",
    ".cmp.drag textarea{border-color:var(--accent)}",
    ".clip{background:var(--w-surface)!important;color:var(--w-muted)!important;",
    "  border:1px solid var(--w-border)!important;",
    "  border-radius:6px!important;width:36px;height:36px;padding:0!important;font-size:16px;cursor:pointer}",
    ".clip:hover,.clip.on{color:var(--accent)!important;border-color:var(--accent)!important}",
    ".atts{display:flex;gap:6px;flex-wrap:wrap;padding:8px 10px 0;background:var(--w-surface2)}",
    ".atts:empty{display:none}",
    ".att{position:relative;width:48px;height:48px;border-radius:6px;overflow:hidden;",
    "  border:1px solid var(--w-border);flex:none}",
    ".att img{width:100%;height:100%;object-fit:cover;display:block;margin:0}",
    ".att button{position:absolute;top:0;right:0;width:16px;height:16px;padding:0;border:none;",
    "  border-radius:0 0 0 6px;background:rgba(10,12,16,.85);color:#fff;font-size:11px;",
    "  line-height:16px;cursor:pointer}",
    ".msg .atts{padding:0 0 6px;background:none}",
    ".cmp textarea{flex:1;background:var(--w-surface);color:var(--w-text);",
    "  border:1px solid var(--w-border);border-radius:6px;",
    "  padding:8px 10px;font:inherit;resize:none;outline:none;max-height:120px;min-height:20px}",
    ".cmp textarea:focus{border-color:var(--accent)}",
    ".cmp button{background:var(--accent);color:var(--w-ink);border:none;border-radius:6px;",
    "  padding:9px 15px;font-weight:600;font-size:11.5px;letter-spacing:.06em;",
    "  text-transform:uppercase;cursor:pointer;flex:none;",
    "  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
    ".cmp button:disabled{opacity:.45;cursor:default}",
    ".spin{display:inline-block;width:12px;height:12px;border:2px solid var(--w-hairline);",
    "  border-top-color:var(--accent);border-radius:50%;animation:blmrot .8s linear infinite;",
    "  vertical-align:-1px;margin-right:6px}",
    "@keyframes blmrot{to{transform:rotate(360deg)}}",
  ].join("\n");
  root.appendChild(style);

  var wrap = document.createElement("div");
  wrap.className = "wrap";
  wrap.style.setProperty("--accent", cfg.accent);
  wrap.setAttribute("data-t", /^(light|dark|auto)$/.test(cfg.theme) ? cfg.theme : "auto");
  wrap.innerHTML =
    '<div class="panel" part="panel">' +
      '<div class="hd">' +
        '<b class="title"></b><span class="sp"></span>' +
        '<select class="prov" title="provider"></select>' +
        '<button class="close" title="close" aria-label="close">×</button>' +
      '</div>' +
      '<div class="log"></div>' +
      '<div class="atts"></div>' +
      '<div class="cmp">' +
        '<button class="clip" title="attach image(s)" aria-label="attach image">📎</button>' +
        '<input type="file" class="file" accept="image/*" multiple hidden>' +
        '<textarea rows="1" placeholder="Message…"></textarea>' +
        '<button class="send">Send</button>' +
      '</div>' +
    '</div>' +
    '<button class="fab" title="Chat" aria-label="Open chat">💬</button>';
  root.appendChild(wrap);

  var el = {
    panel: root.querySelector(".panel"),
    title: root.querySelector(".title"),
    prov: root.querySelector(".prov"),
    close: root.querySelector(".close"),
    log: root.querySelector(".log"),
    ta: root.querySelector(".cmp textarea"),
    send: root.querySelector(".send"),
    fab: root.querySelector(".fab"),
    cmp: root.querySelector(".cmp"),
    clip: root.querySelector(".clip"),
    file: root.querySelector(".file"),
    atts: root.querySelector(".atts"),
  };
  el.title.textContent = cfg.title;
  if (!cfg.attach) el.clip.style.display = "none";

  // ---- tiny, safe markdown (escape first, then a few constructs) ----------
  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function md(srcText) {
    var text = esc(srcText), blocks = [];
    text = text.replace(/```([^\n`]*)\n([\s\S]*?)```/g, function (_, lang, code) {
      blocks.push("<pre><code>" + code + "</code></pre>");
      return "\u0000B" + (blocks.length - 1) + "\u0000";
    });
    text = text
      .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img src="$2" alt="$1" loading="lazy">')
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
      .replace(/\n/g, "<br>");
    return text.replace(/\u0000B(\d+)\u0000/g, function (_, i) { return blocks[+i]; });
  }

  function bubble(cls, html) {
    var d = document.createElement("div");
    d.className = "msg " + cls;
    d.innerHTML = html;
    el.log.appendChild(d);
    el.log.scrollTop = el.log.scrollHeight;
    return d;
  }

  // ---- provider selector (populated from /v1/models) ----------------------
  function loadModels() {
    fetch(cfg.base + "/v1/models", { headers: apiHeaders() }).then(function (r) { return r.json(); }).then(function (j) {
      el.prov.innerHTML = "";
      (j.data || []).forEach(function (m) {
        var o = document.createElement("option");
        o.value = m.id; o.textContent = m.id.replace("-browser", "");
        el.prov.appendChild(o);
      });
      if (cfg.provider) el.prov.value = cfg.provider;
    }).catch(function () { el.prov.style.display = "none"; });
  }

  // ---- attachments (image input) ------------------------------------------
  // Files are read to data: URLs and sent as OpenAI content parts; the server
  // uploads them into the provider's chat. Paste and drag-drop work too.
  var atts = [];   // {name, url}

  function renderAtts() {
    el.atts.innerHTML = "";
    atts.forEach(function (a, i) {
      var d = document.createElement("div");
      d.className = "att";
      d.title = a.name;
      d.innerHTML = '<img alt=""><button title="remove">×</button>';
      d.querySelector("img").src = a.url;
      d.querySelector("button").onclick = function () { atts.splice(i, 1); renderAtts(); };
      el.atts.appendChild(d);
    });
    el.clip.classList.toggle("on", atts.length > 0);
  }

  function addFiles(files) {
    var list = [].slice.call(files || []);
    list.forEach(function (f) {
      if (!f || !f.type || f.type.indexOf("image/") !== 0) return;
      if (atts.length >= MAX_ATTACH) return;
      var fr = new FileReader();
      fr.onload = function () {
        atts.push({ name: f.name || "pasted image", url: fr.result });
        renderAtts();
      };
      fr.readAsDataURL(f);
    });
  }

  // ---- chat state + send loop --------------------------------------------
  var messages = [];
  var busy = false;

  function greet() {
    el.log.innerHTML = "";
    if (cfg.greeting) bubble("bot", md(cfg.greeting));
  }

  function send() {
    var text = el.ta.value.trim();
    var imgs = atts.map(function (a) { return a.url; });
    if ((!text && !imgs.length) || busy) return;
    busy = true; el.send.disabled = true;
    el.ta.value = ""; el.ta.style.height = "auto";

    var provider = el.prov.value || cfg.provider || undefined;
    if (imgs.length) {
      var parts = [];
      if (text) parts.push({ type: "text", text: text });
      imgs.forEach(function (u) { parts.push({ type: "image_url", image_url: { url: u } }); });
      messages.push({ role: "user", content: parts });
    } else {
      messages.push({ role: "user", content: text });
    }
    var thumbs = imgs.length
      ? '<div class="atts">' + imgs.map(function (u) {
          return '<div class="att"><img src="' + esc(u) + '" alt=""></div>';
        }).join("") + '</div>'
      : "";
    bubble("user", thumbs + md(text));
    atts = []; renderAtts();
    var out = bubble("bot", '<span class="spin"></span><span class="hint">thinking…</span>');

    var payload = { stream: true, messages: [] };
    if (provider) payload.model = provider;
    if (cfg.system) payload.messages.push({ role: "system", content: cfg.system });
    payload.messages = payload.messages.concat(messages);

    var full = "";
    fetch(cfg.base + "/v1/chat/completions", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(payload),
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (t) {
          throw new Error("HTTP " + res.status + " — " + t.slice(0, 200));
        });
      }
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
      function pump() {
        return reader.read().then(function (r) {
          if (r.done) return;
          buf += dec.decode(r.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf("\n\n")) >= 0) {
            var evt = buf.slice(0, idx); buf = buf.slice(idx + 2);
            evt.split("\n").forEach(function (line) {
              if (line.indexOf("data:") !== 0) return;
              var data = line.slice(5).trim();
              if (data === "[DONE]") return;
              try {
                var delta = JSON.parse(data).choices[0].delta.content || "";
                if (delta) {
                  full += delta;
                  out.innerHTML = md(full);
                  el.log.scrollTop = el.log.scrollHeight;
                }
              } catch (e) { /* ignore keep-alives / partials */ }
            });
          }
          return pump();
        });
      }
      return pump();
    }).then(function () {
      if (!full) {
        out.innerHTML = '<span class="hint">(empty answer — is this provider logged in?)</span>';
      }
      messages.push({ role: "assistant", content: full });
    }).catch(function (e) {
      out.className = "msg err";
      out.textContent = String(e);
      messages.pop();  // drop the failed user turn so a retry is clean
    }).then(function () {
      busy = false; el.send.disabled = false; el.ta.focus();
    });
  }

  // ---- wiring -------------------------------------------------------------
  function toggle(force) {
    var open = force === undefined ? !el.panel.classList.contains("open") : force;
    el.panel.classList.toggle("open", open);
    el.fab.textContent = open ? "✕" : "💬";
    if (open) { if (!messages.length) greet(); el.ta.focus(); }
  }
  el.fab.onclick = function () { toggle(); };
  el.close.onclick = function () { toggle(false); };
  el.send.onclick = send;
  el.ta.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    else if (e.key === "Escape") { e.preventDefault(); toggle(false); el.fab.focus(); }
  });
  el.ta.addEventListener("input", function () {
    el.ta.style.height = "auto";
    el.ta.style.height = Math.min(el.ta.scrollHeight, 120) + "px";
  });
  el.clip.onclick = function () { el.file.click(); };
  el.file.onchange = function (e) { addFiles(e.target.files); e.target.value = ""; };
  el.ta.addEventListener("paste", function (e) {
    if (!cfg.attach || !e.clipboardData || !e.clipboardData.files.length) return;
    e.preventDefault(); addFiles(e.clipboardData.files);
  });
  ["dragenter", "dragover"].forEach(function (ev) {
    el.cmp.addEventListener(ev, function (e) {
      if (!cfg.attach || !e.dataTransfer) return;
      e.preventDefault(); el.cmp.classList.add("drag");
    });
  });
  ["dragleave", "dragend"].forEach(function (ev) {
    el.cmp.addEventListener(ev, function () { el.cmp.classList.remove("drag"); });
  });
  el.cmp.addEventListener("drop", function (e) {
    if (!cfg.attach || !e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault(); el.cmp.classList.remove("drag"); addFiles(e.dataTransfer.files);
  });

  loadModels();
  greet();
  if (cfg.open) toggle(true);

  // ---- public handle ------------------------------------------------------
  window.__browserLlmWidget = true;
  window.BrowserLLMWidget = {
    open: function () { toggle(true); },
    close: function () { toggle(false); },
    reset: function () { messages = []; atts = []; renderAtts(); greet(); },
    config: function (o) {
      o = o || {};
      if (o.provider !== undefined) { cfg.provider = o.provider; if (o.provider) el.prov.value = o.provider; }
      if (o.system !== undefined) cfg.system = o.system;
      if (o.title !== undefined) { cfg.title = o.title; el.title.textContent = o.title; }
      if (o.accent !== undefined) { cfg.accent = o.accent; wrap.style.setProperty("--accent", o.accent); }
      if (o.theme !== undefined && /^(light|dark|auto)$/.test(o.theme)) {
        cfg.theme = o.theme; wrap.setAttribute("data-t", o.theme);
      }
    },
  };
})();
