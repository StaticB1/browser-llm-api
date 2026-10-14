/*
 * Browser-LLM embeddable chat widget.
 * ---------------------------------------------------------------------------
 * Drop into ANY page on the LAN:
 *
 *   <script src="http://localhost:8081/widget.js"></script>
 *
 * It injects a floating chat bubble (bottom-right) that streams from this
 * server's OpenAI-compatible /v1/chat/completions. The API base is discovered
 * from THIS script's own src, so the host page can live on any origin or port:
 * the server already sends open CORS headers.
 *
 * The panel follows the same conventions as the dashboard at "/": grey user
 * bubbles, plain assistant prose, code cards with Copy, a rounded composer,
 * a stop button while an answer streams, and a thread that survives a page
 * reload (localStorage, one key per API base).
 *
 * Optional config via data-* attributes on the <script> tag:
 *   data-provider  "gemini-browser" | "chatgpt-browser"   (default: server default)
 *   data-title     header text                            (default: "Ask AI")
 *   data-accent    CSS colour for the bubble and send button
 *                  (default: follows the theme, black on light, white on dark)
 *   data-theme     "auto" | "light" | "dark"              (default: auto, which
 *                  follows the HOST page's prefers-color-scheme)
 *   data-position  "br" | "bl"                            (default: br)
 *   data-greeting  first assistant line                   (default: friendly hi)
 *   data-system    a system prompt sent with every turn   (default: none)
 *   data-open      "1" to start expanded                  (default: closed)
 *   data-persist   "0" to forget the thread on reload     (default: remembered)
 *   data-key       API key, sent as Bearer. Needed when the server sets
 *                  BROWSER_LLM_API_KEY and this page is not on the server box
 *   data-attach    "0" to hide the attach button (default: shown; the server
 *                  uploads attached images into the provider's chat)
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
  var base = src.replace(/\/widget\.js(\?.*)?(#.*)?$/, "");
  if (!base) base = window.BROWSER_LLM_BASE || location.origin;

  function attr(name, dflt) {
    if (!script) return dflt;
    var v = script.getAttribute("data-" + name);
    return (v === null || v === "") ? dflt : v;
  }
  var cfg = {
    base: base,
    provider: attr("provider", ""),          // "" -> let the server pick its default
    title: attr("title", "Ask AI"),
    accent: attr("accent", ""),              // "" -> theme decides
    theme: attr("theme", "auto"),
    position: attr("position", "br"),
    greeting: attr("greeting", "Hi. Ask me anything."),
    system: attr("system", ""),
    open: attr("open", "") === "1",
    persist: attr("persist", "1") !== "0",
    key: attr("key", ""),
    attach: attr("attach", "1") !== "0",
  };
  var MAX_ATTACH = 6;
  var STORE = "blm.widget." + cfg.base;

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

  var side = cfg.position === "bl" ? "left" : "right";

  /* Neutral greys, same palette as the dashboard. Light is the base; dark
     arrives either explicitly (data-theme="dark") or, in auto mode, from the
     HOST page's colour scheme. */
  var DARK =
    "--w-bg:#212121;--w-elev:#2F2F2F;--w-hd:#171717;--w-line:#3A3A3A;" +
    "--w-text:#ECECEC;--w-muted:#B4B4B4;--w-bubble:#3A3A3A;--w-code:#0D0D0D;" +
    "--w-codehd:#1C1C1C;--w-hover:#3A3A3A;--w-err:#F87171;" +
    "--w-btn:#FFFFFF;--w-btn-ink:#0D0D0D;--w-shadow:0 20px 52px rgba(0,0,0,.55)";

  var style = document.createElement("style");
  style.textContent = [
    ":host{all:initial}",
    "*{box-sizing:border-box}",
    ".wrap{position:fixed;bottom:20px;" + side + ":20px;",
    "  --w-bg:#FFFFFF;--w-elev:#FFFFFF;--w-hd:#F9F9F9;--w-line:#E5E5E5;",
    "  --w-text:#0D0D0D;--w-muted:#5D5D5D;--w-bubble:#F4F4F4;--w-code:#F7F7F7;",
    "  --w-codehd:#EFEFEF;--w-hover:#ECECEC;--w-err:#DC2B2B;",
    "  --w-btn:#0D0D0D;--w-btn-ink:#FFFFFF;--w-shadow:0 18px 48px rgba(20,24,28,.20);",
    "  font:15px/1.6 Inter,ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}",
    ".wrap[data-t='dark']{" + DARK + "}",
    "@media (prefers-color-scheme: dark){.wrap[data-t='auto']{" + DARK + "}}",
    ".wrap[data-accent]{--w-btn:var(--w-a);--w-btn-ink:#fff}",
    // launcher
    ".fab{width:56px;height:56px;border-radius:50%;border:0;cursor:pointer;padding:0;",
    "  background:var(--w-btn);color:var(--w-btn-ink);display:flex;align-items:center;",
    "  justify-content:center;box-shadow:0 8px 24px rgba(0,0,0,.26);",
    "  transition:transform .14s ease}",
    ".fab:hover{transform:scale(1.05)} .fab:active{transform:scale(.96)}",
    ".fab svg{width:26px;height:26px;display:block}",
    // panel
    ".panel{position:absolute;bottom:70px;" + side + ":0;width:392px;",
    "  max-width:calc(100vw - 32px);height:560px;max-height:calc(100vh - 120px);",
    "  display:none;flex-direction:column;overflow:hidden;background:var(--w-bg);",
    "  color:var(--w-text);border:1px solid var(--w-line);border-radius:18px;",
    "  box-shadow:var(--w-shadow)}",
    ".panel.open{display:flex}",
    ".hd{display:flex;align-items:center;gap:6px;padding:10px 10px 10px 15px;",
    "  background:var(--w-hd);border-bottom:1px solid var(--w-line)}",
    ".hd b{font-weight:600;font-size:14.5px;letter-spacing:-.01em}",
    ".hd .sp{flex:1}",
    ".hd select{background:none;color:var(--w-muted);border:0;font-size:12px;outline:none;",
    "  max-width:104px;cursor:pointer}",
    ".ic{width:32px;height:32px;border:0;border-radius:8px;background:none;cursor:pointer;",
    "  color:var(--w-muted);display:grid;place-items:center;padding:0}",
    ".ic:hover{background:var(--w-hover);color:var(--w-text)}",
    ".ic svg{width:17px;height:17px;display:block}",
    ".log{flex:1;overflow-y:auto;padding:16px 14px 6px;display:flex;flex-direction:column;gap:14px}",
    ".log::-webkit-scrollbar{width:8px}",
    ".log::-webkit-scrollbar-thumb{background:var(--w-line);border-radius:8px}",
    ".msg{overflow-wrap:anywhere}",
    ".msg.user{align-self:flex-end;max-width:86%;background:var(--w-bubble);",
    "  border-radius:18px;padding:9px 14px}",
    ".msg.bot{align-self:stretch}",
    ".msg.err{color:var(--w-err);border:1px solid var(--w-err);border-radius:12px;padding:9px 13px}",
    ".msg p{margin:0 0 10px} .msg p:last-child{margin:0}",
    ".msg h1,.msg h2,.msg h3{font-size:16px;margin:14px 0 8px;font-weight:600}",
    ".msg ul,.msg ol{margin:0 0 10px;padding-left:22px} .msg li{margin:3px 0}",
    ".msg blockquote{margin:0 0 10px;padding-left:12px;border-left:2px solid var(--w-line);",
    "  color:var(--w-muted)}",
    ".msg table{border-collapse:collapse;font-size:13px;display:block;overflow-x:auto;",
    "  margin:0 0 10px}",
    ".msg th,.msg td{border:1px solid var(--w-line);padding:5px 9px;text-align:left}",
    ".msg code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}",
    ".msg :not(pre)>code{background:var(--w-code);border:1px solid var(--w-line);",
    "  border-radius:5px;padding:1px 5px;font-size:12.5px}",
    ".msg img{max-width:100%;border-radius:10px;margin:6px 0;display:block}",
    ".msg a{color:var(--w-text);text-decoration:underline;text-underline-offset:2px}",
    ".card{border:1px solid var(--w-line);border-radius:10px;overflow:hidden;margin:0 0 10px}",
    ".cardhd{display:flex;align-items:center;gap:6px;padding:5px 6px 5px 11px;",
    "  background:var(--w-codehd);border-bottom:1px solid var(--w-line);",
    "  font:11px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--w-muted)}",
    ".cardhd .sp{flex:1}",
    ".cardhd button{border:0;background:none;cursor:pointer;color:var(--w-muted);",
    "  font:11px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;padding:4px 7px;border-radius:5px}",
    ".cardhd button:hover{background:var(--w-hover);color:var(--w-text)}",
    ".card pre{margin:0;padding:11px;overflow-x:auto;background:var(--w-code)}",
    ".card code{font-size:12.5px;line-height:1.6}",
    ".think{display:flex;align-items:center;gap:8px;color:var(--w-muted);font-size:14px}",
    ".orb{width:12px;height:12px;border-radius:50%;background:var(--w-muted);",
    "  animation:blmorb 1.15s ease-in-out infinite}",
    "@keyframes blmorb{0%,100%{transform:scale(.6);opacity:.45}50%{transform:scale(1);opacity:1}}",
    ".cur{display:inline-block;width:8px;height:15px;border-radius:2px;background:var(--w-text);",
    "  vertical-align:-2px;margin-left:2px;animation:blmblink 1s steps(2,start) infinite}",
    "@keyframes blmblink{50%{opacity:0}}",
    ".hint{color:var(--w-muted);font-size:13px}",
    // composer
    ".cmpwrap{padding:10px 12px 12px}",
    ".cmp{background:var(--w-elev);border:1px solid var(--w-line);border-radius:22px;",
    "  padding:6px 8px 6px 13px}",
    ".cmp.focus{border-color:var(--w-muted)}",
    ".cmp.drag{border-color:var(--w-text);border-style:dashed}",
    ".cmp textarea{width:100%;border:0;background:none;outline:none;resize:none;",
    "  font:inherit;color:var(--w-text);padding:6px 0 2px;max-height:130px;min-height:24px}",
    ".cmp textarea::placeholder{color:var(--w-muted)}",
    ".row{display:flex;align-items:center;gap:4px;margin-top:2px}",
    ".row .sp{flex:1}",
    ".rnd{width:30px;height:30px;border:0;border-radius:50%;background:none;cursor:pointer;",
    "  color:var(--w-muted);display:grid;place-items:center;padding:0}",
    ".rnd:hover{background:var(--w-hover);color:var(--w-text)}",
    ".rnd.on{color:var(--w-text);background:var(--w-hover)}",
    ".rnd svg{width:17px;height:17px;display:block}",
    ".go{width:30px;height:30px;border:0;border-radius:50%;cursor:pointer;padding:0;",
    "  background:var(--w-btn);color:var(--w-btn-ink);display:grid;place-items:center}",
    ".go:disabled{opacity:.25;cursor:default}",
    ".go svg{width:16px;height:16px;display:block}",
    ".clock{font:11px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:var(--w-muted)}",
    ".atts{display:flex;gap:6px;flex-wrap:wrap;padding:2px 0 6px}",
    ".atts:empty{display:none}",
    ".att{position:relative;width:48px;height:48px;border-radius:9px;overflow:hidden;",
    "  border:1px solid var(--w-line);flex:none}",
    ".att img{width:100%;height:100%;object-fit:cover;display:block;margin:0}",
    ".att button{position:absolute;top:1px;right:1px;width:17px;height:17px;padding:0;border:0;",
    "  border-radius:50%;background:rgba(13,13,13,.8);color:#fff;font-size:11px;",
    "  line-height:17px;cursor:pointer}",
    ".msg .atts{padding:0 0 6px}",
    "@media (max-width:520px){",
    "  .panel{position:fixed;inset:0;width:100vw;height:100vh;max-width:none;max-height:none;",
    "    border-radius:0;border:0}",
    "  .wrap{bottom:16px;" + side + ":16px}",
    "}",
    "@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}",
  ].join("\n");
  root.appendChild(style);

  var I = {
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a8.5 8.5 0 0 1-12.2 7.7L4 21l1.4-4.4A8.5 8.5 0 1 1 21 12z"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    fresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16.5 3.6a2.1 2.1 0 0 1 3 3L8 18.1l-4 1 1-4z"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/></svg>',
    stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>',
  };

  var wrap = document.createElement("div");
  wrap.className = "wrap";
  wrap.setAttribute("data-t", /^(light|dark|auto)$/.test(cfg.theme) ? cfg.theme : "auto");
  if (cfg.accent) { wrap.setAttribute("data-accent", "1"); wrap.style.setProperty("--w-a", cfg.accent); }
  wrap.innerHTML =
    '<div class="panel" part="panel">' +
      '<div class="hd">' +
        '<b class="title"></b><span class="sp"></span>' +
        '<select class="prov" title="model" aria-label="model"></select>' +
        '<button class="ic fresh" title="New chat" aria-label="New chat">' + I.fresh + "</button>" +
        '<button class="ic close" title="Close" aria-label="Close">' + I.close + "</button>" +
      "</div>" +
      '<div class="log"></div>' +
      '<div class="cmpwrap"><div class="cmp">' +
        '<div class="atts"></div>' +
        '<textarea rows="1" placeholder="Ask anything"></textarea>' +
        '<div class="row">' +
          '<button class="rnd clip" title="Attach images" aria-label="Attach images">' + I.plus + "</button>" +
          '<input type="file" class="file" accept="image/*" multiple hidden>' +
          '<span class="sp"></span><span class="clock"></span>' +
          '<button class="go" title="Send" aria-label="Send">' + I.up + "</button>" +
        "</div>" +
      "</div></div>" +
    "</div>" +
    '<button class="fab" title="Chat" aria-label="Open chat">' + I.chat + "</button>";
  root.appendChild(wrap);

  var el = {
    panel: root.querySelector(".panel"),
    title: root.querySelector(".title"),
    prov:  root.querySelector(".prov"),
    fresh: root.querySelector(".fresh"),
    close: root.querySelector(".close"),
    log:   root.querySelector(".log"),
    cmp:   root.querySelector(".cmp"),
    ta:    root.querySelector("textarea"),
    go:    root.querySelector(".go"),
    clock: root.querySelector(".clock"),
    fab:   root.querySelector(".fab"),
    clip:  root.querySelector(".clip"),
    file:  root.querySelector(".file"),
    atts:  root.querySelector(".cmp .atts"),
  };
  el.title.textContent = cfg.title;
  if (!cfg.attach) el.clip.style.display = "none";

  // ---- markdown: escape first, then a small block and inline grammar ------
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  function inline(s) {
    return s
      .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, '<img src="$2" alt="$1" loading="lazy">')
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<i>$2</i>");
  }
  var FENCE = /^@@BLMCODE(\d+)@@$/;
  function md(srcText) {
    var blocks = [];
    var raw = String(srcText == null ? "" : srcText)
      .replace(/```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g, function (_, lang, code) {
        blocks.push('<div class="card"><div class="cardhd"><span>' + esc(lang.trim() || "code") +
          '</span><span class="sp"></span><button type="button" data-copy>Copy</button></div>' +
          "<pre><code>" + esc(code.replace(/\n+$/, "")) + "</code></pre></div>");
        return "\n@@BLMCODE" + (blocks.length - 1) + "@@\n";
      });
    var lines = esc(raw).split("\n"), out = [], i, m;
    for (i = 0; i < lines.length; i++) {
      var ln = lines[i];
      if (/^\s*$/.test(ln)) continue;
      if ((m = ln.match(FENCE))) { out.push(blocks[+m[1]]); continue; }
      if ((m = ln.match(/^(#{1,6})\s+(.*)$/))) { out.push("<h3>" + inline(m[2]) + "</h3>"); continue; }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(ln)) { out.push("<hr>"); continue; }
      if (/^&gt;\s?/.test(ln)) {
        var q = [];
        while (i < lines.length && /^&gt;\s?/.test(lines[i])) q.push(lines[i++].replace(/^&gt;\s?/, ""));
        i--; out.push("<blockquote>" + inline(q.join("<br>")) + "</blockquote>"); continue;
      }
      if (/^\s*\|.*\|\s*$/.test(ln) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i+1] || "")) {
        var cells = function (r) {
          return r.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); });
        };
        var head = cells(ln); i += 2;
        var rows = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
        i--;
        out.push("<table><thead><tr>" + head.map(function (h) { return "<th>" + inline(h) + "</th>"; }).join("") +
          "</tr></thead><tbody>" + rows.map(function (r) {
            return "<tr>" + r.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>";
          }).join("") + "</tbody></table>");
        continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(ln)) {
        var ordered = /^\s*\d+[.)]\s+/.test(ln), items = [];
        while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i]))
          items.push(lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
        i--;
        var tag = ordered ? "ol" : "ul";
        out.push("<" + tag + ">" + items.map(function (t) { return "<li>" + inline(t) + "</li>"; }).join("") +
          "</" + tag + ">");
        continue;
      }
      var para = [ln];
      while (i + 1 < lines.length && !/^\s*$/.test(lines[i+1]) && !FENCE.test(lines[i+1]) &&
             !/^(#{1,6}\s|&gt;\s?)/.test(lines[i+1]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i+1]) &&
             !/^\s*\|.*\|\s*$/.test(lines[i+1])) para.push(lines[++i]);
      out.push("<p>" + inline(para.join("<br>")) + "</p>");
    }
    return out.join("");
  }
  root.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-copy]");
    if (!b) return;
    var code = b.closest(".card").querySelector("code").textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(code);
    b.textContent = "Copied";
    setTimeout(function () { b.textContent = "Copy"; }, 1500);
  });

  // ---- thread state, remembered per API base ------------------------------
  var messages = [];          // {role, content, imgs}
  var busy = false, ac = null;

  function save() {
    if (!cfg.persist) return;
    try { localStorage.setItem(STORE, JSON.stringify(messages)); }
    catch (e) {                                  // over quota: shed images, then history
      for (var i = 0; i < messages.length; i++)
        if (messages[i].imgs) messages[i].imgs = [];
      try { localStorage.setItem(STORE, JSON.stringify(messages)); }
      catch (e2) { try { localStorage.removeItem(STORE); } catch (e3) {} }
    }
  }
  function restore() {
    if (!cfg.persist) return;
    try {
      var v = JSON.parse(localStorage.getItem(STORE) || "[]");
      if (Array.isArray(v)) messages = v;
    } catch (e) {}
  }

  function attsHtml(m) {
    if (!m.imgs || !m.imgs.length) return "";
    return '<div class="atts">' + m.imgs.map(function (u) {
      return '<div class="att"><img src="' + esc(u) + '" alt=""></div>';
    }).join("") + "</div>";
  }
  function render() {
    el.log.innerHTML = "";
    if (!messages.length && cfg.greeting) bubble("bot", md(cfg.greeting));
    messages.forEach(function (m) {
      if (m.role === "user") bubble("user", attsHtml(m) + md(m.content));
      else bubble(m.error ? "bot err" : "bot", m.error ? esc(m.content)
        : (m.content ? md(m.content) : '<span class="hint">Empty answer. Is this provider signed in?</span>'));
    });
    el.log.scrollTop = el.log.scrollHeight;
  }
  function bubble(cls, html) {
    var d = document.createElement("div");
    d.className = "msg " + cls;
    d.innerHTML = html;
    el.log.appendChild(d);
    el.log.scrollTop = el.log.scrollHeight;
    return d;
  }

  // ---- model list ---------------------------------------------------------
  function loadModels() {
    fetch(cfg.base + "/v1/models", { headers: apiHeaders() })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        el.prov.innerHTML = "";
        (j.data || []).forEach(function (m) {
          var o = document.createElement("option");
          o.value = m.id; o.textContent = m.id.replace("-browser", "");
          el.prov.appendChild(o);
        });
        if (cfg.provider) el.prov.value = cfg.provider;
      })
      .catch(function () { el.prov.style.display = "none"; });
  }

  // ---- attachments -------------------------------------------------------
  var atts = [];
  function renderAtts() {
    el.atts.innerHTML = "";
    atts.forEach(function (a, i) {
      var d = document.createElement("div");
      d.className = "att"; d.title = a.name;
      d.innerHTML = '<img alt=""><button type="button" aria-label="Remove">&times;</button>';
      d.querySelector("img").src = a.url;
      d.querySelector("button").onclick = function () { atts.splice(i, 1); renderAtts(); };
      el.atts.appendChild(d);
    });
    el.clip.classList.toggle("on", atts.length > 0);
    syncGo();
  }
  function addFiles(files) {
    [].slice.call(files || []).forEach(function (f) {
      if (!f || !f.type || f.type.indexOf("image/") !== 0) return;
      if (atts.length >= MAX_ATTACH) return;
      var fr = new FileReader();
      fr.onload = function () { atts.push({ name: f.name || "pasted image", url: fr.result }); renderAtts(); };
      fr.readAsDataURL(f);
    });
  }

  // ---- one turn ----------------------------------------------------------
  function syncGo() {
    if (busy) { el.go.innerHTML = I.stop; el.go.disabled = false; el.go.title = "Stop"; return; }
    el.go.innerHTML = I.up; el.go.title = "Send";
    el.go.disabled = !el.ta.value.trim() && !atts.length;
  }
  function payloadMessages() {
    var out = [];
    if (cfg.system) out.push({ role: "system", content: cfg.system });
    messages.forEach(function (m) {
      if (m.error) return;
      if (m.role === "user" && m.imgs && m.imgs.length) {
        var parts = [];
        if (m.content) parts.push({ type: "text", text: m.content });
        m.imgs.forEach(function (u) { parts.push({ type: "image_url", image_url: { url: u } }); });
        out.push({ role: "user", content: parts });
      } else out.push({ role: m.role, content: m.content });
    });
    return out;
  }

  function send() {
    var text = el.ta.value.trim();
    var imgs = atts.map(function (a) { return a.url; });
    if ((!text && !imgs.length) || busy) return;
    el.ta.value = ""; el.ta.style.height = "auto";
    atts = []; renderAtts();
    if (!messages.length) el.log.innerHTML = "";        // drop the greeting
    messages.push({ role: "user", content: text, imgs: imgs });
    bubble("user", attsHtml(messages[messages.length - 1]) + md(text));
    save();
    drive();
  }

  function drive() {
    busy = true; syncGo();
    var provider = el.prov.value || cfg.provider || "";
    var out = bubble("bot", '<div class="think"><span class="orb"></span><span class="st">Thinking</span></div>');
    var t0 = Date.now();
    var clock = setInterval(function () {
      var s = Math.floor((Date.now() - t0) / 1000);
      el.clock.textContent = s + "s";
      var st = out.querySelector(".st");
      if (st) st.textContent = "Thinking for " + s + "s";
    }, 500);

    var body = { stream: true, messages: payloadMessages() };
    if (provider) body.model = provider;
    var full = "", failed = null, stopped = false;
    ac = window.AbortController ? new AbortController() : null;

    fetch(cfg.base + "/v1/chat/completions", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      signal: ac ? ac.signal : undefined,
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (t) {
          throw new Error("HTTP " + res.status + " " + t.slice(0, 200));
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
                  out.innerHTML = md(full) + '<span class="cur"></span>';
                  el.log.scrollTop = el.log.scrollHeight;
                }
              } catch (e) { /* keep-alives and partial frames */ }
            });
          }
          return pump();
        });
      }
      return pump();
    }).catch(function (e) {
      if (e && e.name === "AbortError") stopped = true;
      else failed = String(e && e.message ? e.message : e);
    }).then(function () {
      clearInterval(clock);
      el.clock.textContent = "";
      busy = false; ac = null;
      out.remove();
      /* a stop with nothing streamed yet leaves no turn behind, rather than an
         empty bubble that reads like a broken provider */
      if (!(stopped && !full)) {
        messages.push(failed ? { role: "assistant", content: failed, error: true }
                             : { role: "assistant", content: full });
        var m = messages[messages.length - 1];
        bubble(m.error ? "bot err" : "bot", m.error ? esc(m.content)
          : (m.content ? md(m.content) : '<span class="hint">Empty answer. Is this provider signed in?</span>'));
      }
      save(); syncGo(); el.ta.focus();
    });
  }

  // ---- wiring ------------------------------------------------------------
  function toggle(force) {
    var open = force === undefined ? !el.panel.classList.contains("open") : force;
    el.panel.classList.toggle("open", open);
    el.fab.innerHTML = open ? I.close : I.chat;
    el.fab.title = open ? "Close chat" : "Chat";
    if (open) { el.ta.focus(); el.log.scrollTop = el.log.scrollHeight; }
  }
  function reset() {
    if (busy && ac) ac.abort();
    messages = []; atts = []; renderAtts();
    try { localStorage.removeItem(STORE); } catch (e) {}
    render(); el.ta.focus();
  }
  el.fab.onclick   = function () { toggle(); };
  el.close.onclick = function () { toggle(false); };
  el.fresh.onclick = reset;
  el.go.onclick    = function () { if (busy) { if (ac) ac.abort(); } else send(); };
  el.ta.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    else if (e.key === "Escape") { e.preventDefault(); toggle(false); el.fab.focus(); }
  });
  el.ta.addEventListener("input", function () {
    el.ta.style.height = "auto";
    el.ta.style.height = Math.min(el.ta.scrollHeight, 130) + "px";
    syncGo();
  });
  el.ta.addEventListener("focus", function () { el.cmp.classList.add("focus"); });
  el.ta.addEventListener("blur",  function () { el.cmp.classList.remove("focus"); });
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

  restore();
  render();
  syncGo();
  loadModels();
  if (cfg.open) toggle(true);

  // ---- public handle ------------------------------------------------------
  window.__browserLlmWidget = true;
  window.BrowserLLMWidget = {
    open: function () { toggle(true); },
    close: function () { toggle(false); },
    reset: reset,
    config: function (o) {
      o = o || {};
      if (o.provider !== undefined) { cfg.provider = o.provider; if (o.provider) el.prov.value = o.provider; }
      if (o.system !== undefined) cfg.system = o.system;
      if (o.title !== undefined) { cfg.title = o.title; el.title.textContent = o.title; }
      if (o.accent !== undefined) {
        cfg.accent = o.accent;
        if (o.accent) { wrap.setAttribute("data-accent", "1"); wrap.style.setProperty("--w-a", o.accent); }
        else wrap.removeAttribute("data-accent");
      }
      if (o.theme !== undefined && /^(light|dark|auto)$/.test(o.theme)) {
        cfg.theme = o.theme; wrap.setAttribute("data-t", o.theme);
      }
    },
  };
})();
