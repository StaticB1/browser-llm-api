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
 *   data-accent    CSS color for the bubble/buttons       (default: #6ea8fe)
 *   data-position  "br" | "bl"                            (default: br)
 *   data-greeting  first assistant line                   (default: friendly hi)
 *   data-system    a system prompt sent with every turn   (default: none)
 *   data-open      "1" to start expanded                  (default: closed)
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
    accent: attr("accent", "#6ea8fe"),
    position: attr("position", "br"),
    greeting: attr("greeting", "Hi! Ask me anything."),
    system: attr("system", ""),
    open: attr("open", "") === "1",
  };

  // ---- shadow-DOM host (style isolation both ways) ------------------------
  var host = document.createElement("div");
  host.id = "browser-llm-widget";
  host.style.cssText = "all:initial;position:fixed;z-index:2147483000;";
  var root = host.attachShadow({ mode: "open" });
  document.documentElement.appendChild(host);

  var sideProp = cfg.position === "bl" ? "left" : "right";

  var style = document.createElement("style");
  style.textContent = [
    ":host{all:initial}",
    "*{box-sizing:border-box}",
    ".wrap{position:fixed;bottom:20px;" + sideProp + ":20px;",
    "  font:14px/1.5 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}",
    // launcher bubble
    ".fab{width:56px;height:56px;border-radius:50%;border:none;cursor:pointer;",
    "  background:var(--accent);color:#fff;font-size:26px;line-height:1;",
    "  box-shadow:0 6px 22px rgba(0,0,0,.28);display:flex;align-items:center;",
    "  justify-content:center;transition:transform .15s ease}",
    ".fab:hover{transform:scale(1.06)}",
    ".fab:active{transform:scale(.96)}",
    // panel
    ".panel{position:absolute;bottom:70px;" + sideProp + ":0;width:370px;max-width:calc(100vw - 32px);",
    "  height:520px;max-height:calc(100vh - 120px);display:none;flex-direction:column;",
    "  background:#171a21;color:#e6e9ef;border:1px solid #2a2f3a;border-radius:16px;",
    "  overflow:hidden;box-shadow:0 18px 50px rgba(0,0,0,.45)}",
    ".panel.open{display:flex}",
    ".hd{display:flex;align-items:center;gap:8px;padding:12px 14px;background:#1d212b;",
    "  border-bottom:1px solid #2a2f3a}",
    ".hd b{font-weight:650;font-size:14.5px}",
    ".hd .sp{flex:1}",
    ".hd select{background:#171a21;color:#8b93a7;border:1px solid #2a2f3a;border-radius:7px;",
    "  padding:3px 6px;font-size:11.5px;max-width:120px}",
    ".hd button{background:none;border:none;color:#8b93a7;cursor:pointer;font-size:20px;",
    "  line-height:1;padding:2px 4px;border-radius:6px}",
    ".hd button:hover{color:#e6e9ef;background:#262b36}",
    ".log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px}",
    ".msg{max-width:88%;padding:9px 12px;border-radius:12px;border:1px solid #2a2f3a;",
    "  overflow-wrap:anywhere;white-space:normal}",
    ".msg.user{align-self:flex-end;background:#20304d;border-color:#2c4066}",
    ".msg.bot{align-self:flex-start;background:#12151b}",
    ".msg.err{border-color:#f47067;color:#f47067}",
    ".msg pre{background:#0c0e12;border:1px solid #2a2f3a;border-radius:8px;padding:9px 11px;",
    "  overflow-x:auto;font-size:12.5px;margin:6px 0}",
    ".msg code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
    ".msg :not(pre)>code{background:#0c0e12;border:1px solid #2a2f3a;border-radius:5px;padding:1px 5px;font-size:12.5px}",
    ".msg img{max-width:100%;border-radius:8px;margin-top:6px;display:block}",
    ".msg a{color:var(--accent)}",
    ".hint{color:#8b93a7;font-size:12px}",
    ".cmp{display:flex;gap:8px;align-items:flex-end;padding:10px;border-top:1px solid #2a2f3a;background:#1d212b}",
    ".cmp textarea{flex:1;background:#171a21;color:#e6e9ef;border:1px solid #2a2f3a;border-radius:9px;",
    "  padding:8px 10px;font:inherit;resize:none;outline:none;max-height:120px;min-height:20px}",
    ".cmp textarea:focus{border-color:var(--accent)}",
    ".cmp button{background:var(--accent);color:#0b1020;border:none;border-radius:9px;",
    "  padding:8px 15px;font-weight:650;cursor:pointer;flex:none}",
    ".cmp button:disabled{opacity:.45;cursor:default}",
    ".spin{display:inline-block;width:12px;height:12px;border:2px solid #2a2f3a;",
    "  border-top-color:var(--accent);border-radius:50%;animation:blmrot .8s linear infinite;",
    "  vertical-align:-1px;margin-right:6px}",
    "@keyframes blmrot{to{transform:rotate(360deg)}}",
  ].join("\n");
  root.appendChild(style);

  var wrap = document.createElement("div");
  wrap.className = "wrap";
  wrap.style.setProperty("--accent", cfg.accent);
  wrap.innerHTML =
    '<div class="panel" part="panel">' +
      '<div class="hd">' +
        '<b class="title"></b><span class="sp"></span>' +
        '<select class="prov" title="provider"></select>' +
        '<button class="close" title="close" aria-label="close">×</button>' +
      '</div>' +
      '<div class="log"></div>' +
      '<div class="cmp">' +
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
  };
  el.title.textContent = cfg.title;

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
    fetch(cfg.base + "/v1/models").then(function (r) { return r.json(); }).then(function (j) {
      el.prov.innerHTML = "";
      (j.data || []).forEach(function (m) {
        var o = document.createElement("option");
        o.value = m.id; o.textContent = m.id.replace("-browser", "");
        el.prov.appendChild(o);
      });
      if (cfg.provider) el.prov.value = cfg.provider;
    }).catch(function () { el.prov.style.display = "none"; });
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
    if (!text || busy) return;
    busy = true; el.send.disabled = true;
    el.ta.value = ""; el.ta.style.height = "auto";

    var provider = el.prov.value || cfg.provider || undefined;
    messages.push({ role: "user", content: text });
    bubble("user", md(text));
    var out = bubble("bot", '<span class="spin"></span><span class="hint">thinking…</span>');

    var payload = { stream: true, messages: [] };
    if (provider) payload.model = provider;
    if (cfg.system) payload.messages.push({ role: "system", content: cfg.system });
    payload.messages = payload.messages.concat(messages);

    var full = "";
    fetch(cfg.base + "/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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

  loadModels();
  greet();
  if (cfg.open) toggle(true);

  // ---- public handle ------------------------------------------------------
  window.__browserLlmWidget = true;
  window.BrowserLLMWidget = {
    open: function () { toggle(true); },
    close: function () { toggle(false); },
    reset: function () { messages = []; greet(); },
    config: function (o) {
      o = o || {};
      if (o.provider !== undefined) { cfg.provider = o.provider; if (o.provider) el.prov.value = o.provider; }
      if (o.system !== undefined) cfg.system = o.system;
      if (o.title !== undefined) { cfg.title = o.title; el.title.textContent = o.title; }
      if (o.accent !== undefined) { cfg.accent = o.accent; wrap.style.setProperty("--accent", o.accent); }
    },
  };
})();
