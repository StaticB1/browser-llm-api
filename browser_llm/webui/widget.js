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
 * The panel shares the dashboard's design system (near-black dark theme, one
 * lavender accent, hairline borders, Geist type served from this server's
 * /assets): plain assistant prose, code cards with Copy, an image viewer, a
 * rounded composer, a stop button while an answer streams, and a thread that
 * survives a page reload (localStorage, one key per API base).
 *
 * Optional config via data-* attributes on the <script> tag:
 *   data-provider     "chatgpt-browser" | "gemini-browser"    (default: server default)
 *   data-title        header text                             (default: "Ask AI")
 *   data-accent       CSS colour for the launcher, send button and focus ring,
 *                     pinned for both themes                  (default: brand lavender)
 *   data-theme        "light" | "auto" | "dark"               (default: light; auto
 *                     follows the HOST page's prefers-color-scheme)
 *   data-position     "br" | "bl"                             (default: br)
 *   data-greeting     first assistant line                    (default: a short hello)
 *   data-suggestions  starter prompts shown on an empty thread, separated by "|"
 *   data-system       a system prompt sent with every turn    (default: none)
 *   data-open         "1" to start expanded                   (default: closed)
 *   data-persist      "0" to forget the thread on reload      (default: remembered)
 *   data-key          API key, sent as Bearer. Needed when the server sets
 *                     BROWSER_LLM_API_KEY and this page is not on the server box
 *   data-attach       "0" to hide the attach button (default: shown; the server
 *                     uploads attached images into the provider's chat)
 *
 * Runtime handle, window.BrowserLLMWidget:
 *   open() close() reset() destroy() config({...})
 *   ask(text, {open, images}) -> Promise of the answer text. Calls made while an
 *     answer is streaming wait their turn (a queue), so a host can fire several.
 *   on(event, fn) -> unsubscribe function; off(event, fn).
 *     Events: "ready", "open", "close", "message" ({role, content}), "error" ({message}).
 *   window also receives a "browserllm:ready" CustomEvent once the widget is up.
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
  function splitSuggestions(v) {
    if (Array.isArray(v)) return v.map(String).map(function (s) { return s.trim(); }).filter(Boolean);
    return String(v || "").split("|").map(function (s) { return s.trim(); }).filter(Boolean);
  }
  var cfg = {
    base: base,
    provider: attr("provider", ""),          // "" -> let the server pick its default
    title: attr("title", "Ask AI"),
    accent: attr("accent", ""),              // "" -> brand lavender
    theme: attr("theme", "light"),
    position: attr("position", "br"),
    greeting: attr("greeting", "Hi. Ask me anything."),
    suggestions: splitSuggestions(attr("suggestions", "")),
    system: attr("system", ""),
    open: attr("open", "") === "1",
    persist: attr("persist", "1") !== "0",
    key: attr("key", ""),
    attach: attr("attach", "1") !== "0",
  };
  var MAX_ATTACH = 6;
  var STORE = "blm.widget." + cfg.base;
  var SIZE_STORE = "blm.widget.size." + cfg.base;

  function apiHeaders(h) {
    h = h || {};
    if (cfg.key) h["Authorization"] = "Bearer " + cfg.key;
    return h;
  }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  // ---- fonts: @font-face does not apply inside a shadow root, so the faces
  //      are declared once on the host document under names of our own ------
  if (!document.getElementById("blm-widget-fonts")) {
    var faces = document.createElement("style");
    faces.id = "blm-widget-fonts";
    faces.textContent =
      '@font-face{font-family:"BLM Geist";src:url("' + cfg.base + '/assets/Geist-Variable.woff2") format("woff2");' +
      "font-weight:100 900;font-style:normal;font-display:swap}" +
      '@font-face{font-family:"BLM Geist Mono";src:url("' + cfg.base + '/assets/GeistMono-Variable.woff2") format("woff2");' +
      "font-weight:100 900;font-style:normal;font-display:swap}";
    (document.head || document.documentElement).appendChild(faces);
  }

  // ---- shadow-DOM host (style isolation both ways) ------------------------
  var host = document.createElement("div");
  host.id = "browser-llm-widget";
  host.style.cssText = "all:initial;position:fixed;z-index:2147483000;";
  var root = host.attachShadow({ mode: "open" });
  document.documentElement.appendChild(host);

  /* One design system with the dashboard (linear.app reference). Light is the
     base; dark arrives explicitly (data-theme="dark") or, in auto mode, from
     the HOST page's colour scheme. */
  var DARK =
    "--w-canvas:#08090a;--w-s1:#0f1011;--w-s2:#141516;--w-s3:#18191a;--w-raise:#141516;" +
    "--w-bubble:#1c1d1f;--w-line:#23252a;--w-line2:#34343a;" +
    "--w-ink:#f7f8f8;--w-ink2:#d0d6e0;--w-ink3:#8a8f98;--w-ink4:#62666d;" +
    "--w-accent-hover:#828fff;--w-accent-soft:rgba(94,106,210,.16);" +
    "--w-ok:#27a644;--w-warn:#e2a33d;--w-err:#eb5757;--w-code:#0f1011;" +
    "--w-shadow:0 24px 64px rgba(0,0,0,.55),0 0 0 1px rgba(255,255,255,.02);" +
    "--w-hi:inset 0 1px 0 rgba(255,255,255,.04);color-scheme:dark";

  var style = document.createElement("style");
  style.textContent = `
:host{all:initial}
*{box-sizing:border-box}
.wrap{position:fixed;bottom:20px;right:20px;
  --w-canvas:#ffffff;--w-s1:#f5f6f6;--w-s2:#eeeff1;--w-s3:#e8e9ec;--w-raise:#ffffff;
  --w-bubble:#eeeff1;--w-line:#e6e7ea;--w-line2:#d5d7dc;
  --w-ink:#08090a;--w-ink2:#3b3f46;--w-ink3:#6b6f76;--w-ink4:#9a9ea6;
  --w-accent:#5e6ad2;--w-accent-hover:#4c57c4;--w-accent-soft:rgba(94,106,210,.10);--w-accent-ink:#ffffff;
  --w-ok:#1f8f3a;--w-warn:#b7791f;--w-err:#d93a3a;--w-code:#f5f6f6;
  --w-shadow:0 24px 64px rgba(16,18,40,.16),0 2px 6px rgba(16,18,40,.06);
  --w-hi:inset 0 1px 0 rgba(255,255,255,.7);
  --w-ease:cubic-bezier(.32,.72,0,1);--w-out:cubic-bezier(.16,1,.3,1);
  --w-sans:"BLM Geist",Geist,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  --w-mono:"BLM Geist Mono","Geist Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font:400 14.5px/1.6 var(--w-sans);letter-spacing:-.006em;color:var(--w-ink);
  -webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
.wrap[data-side="left"]{right:auto;left:20px}
.wrap[data-t="dark"]{${DARK}}
@media (prefers-color-scheme:dark){.wrap[data-t="auto"]{${DARK}}}
.wrap[data-accent]{--w-accent:var(--w-a);--w-accent-hover:var(--w-a);
  --w-accent-soft:color-mix(in srgb,var(--w-a) 15%,transparent);--w-accent-ink:var(--w-a-ink,#fff)}
button{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--w-accent);outline-offset:2px}
svg{display:block;flex:none}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* launcher */
.fab{position:relative;width:56px;height:56px;border-radius:50%;border:0;padding:0;cursor:pointer;
  background:var(--w-accent);color:var(--w-accent-ink);display:block;
  box-shadow:0 12px 30px -6px color-mix(in srgb,var(--w-accent) 55%,transparent),
    0 2px 6px rgba(16,18,40,.16),inset 0 1px 0 rgba(255,255,255,.24);
  transition:transform .34s var(--w-ease),background-color .2s var(--w-out),opacity .2s var(--w-out)}
.fab:hover{transform:translateY(-2px);background:var(--w-accent-hover)}
.fab:active{transform:scale(.95);transition-duration:.12s}
.fab .i{position:absolute;inset:0;display:grid;place-items:center;
  transition:transform .4s var(--w-ease),opacity .22s var(--w-out)}
.fab .i svg{width:24px;height:24px}
.fab .i-close{opacity:0;transform:rotate(-90deg) scale(.5)}
.wrap.is-open .fab .i-chat{opacity:0;transform:rotate(90deg) scale(.5)}
.wrap.is-open .fab .i-close{opacity:1;transform:none}
.fab .dot{position:absolute;top:2px;right:2px;width:13px;height:13px;border-radius:50%;
  background:var(--w-ink);border:2.5px solid var(--w-canvas);transform:scale(0);
  transition:transform .34s var(--w-ease)}
.fab.unread .dot{transform:scale(1)}

/* panel */
.panel{position:absolute;bottom:72px;right:0;width:400px;max-width:calc(100vw - 32px);
  height:600px;max-height:calc(100vh - 112px);display:flex;flex-direction:column;overflow:hidden;
  background:var(--w-canvas);color:var(--w-ink);border:1px solid var(--w-line);border-radius:20px;
  box-shadow:var(--w-shadow);transform-origin:bottom right;
  opacity:0;visibility:hidden;transform:translateY(14px) scale(.96);
  transition:opacity .18s var(--w-out),transform .3s var(--w-ease),visibility 0s linear .3s}
.wrap[data-side="left"] .panel{right:auto;left:0;transform-origin:bottom left}
.panel.open{opacity:1;visibility:visible;transform:none;
  transition:opacity .22s var(--w-out),transform .38s var(--w-ease),visibility 0s}
.panel.large{width:720px;height:min(86vh,860px)}

.hd{display:flex;align-items:center;gap:10px;padding:12px 10px 12px 14px;
  border-bottom:1px solid var(--w-line);flex:none}
.mark{width:30px;height:30px;border-radius:9px;background:var(--w-accent);color:var(--w-accent-ink);
  display:grid;place-items:center;flex:none;box-shadow:inset 0 1px 0 rgba(255,255,255,.22)}
.mark svg{width:17px;height:17px}
.tt{display:flex;flex-direction:column;min-width:0;flex:1;gap:1px}
.title{font-weight:600;font-size:14px;letter-spacing:-.014em;line-height:1.25;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.model{position:relative;display:inline-flex;align-items:center;gap:6px;color:var(--w-ink3);
  font-size:12px;line-height:1.3;align-self:flex-start;max-width:100%}
.model .live{width:6px;height:6px;border-radius:50%;background:var(--w-ink4);flex:none}
.model.ok .live{background:var(--w-ok)}
.model.off .live{background:var(--w-err)}
.prov{appearance:none;-webkit-appearance:none;background:none;border:0;color:inherit;font:inherit;
  padding:0 14px 0 0;margin:0;cursor:pointer;outline:none;max-width:150px;
  text-overflow:ellipsis;border-radius:4px}
.prov:focus-visible{outline:2px solid var(--w-accent);outline-offset:2px}
.prov option{color:#08090a;background:#fff}
.model .chev{position:absolute;right:0;top:50%;width:11px;height:11px;margin-top:-5.5px;pointer-events:none}
.model .state{color:var(--w-ink3)}
.ic{width:32px;height:32px;border:0;border-radius:8px;background:none;cursor:pointer;
  color:var(--w-ink3);display:grid;place-items:center;padding:0;
  transition:background-color .16s var(--w-out),color .16s var(--w-out),transform .16s var(--w-ease)}
.ic:hover{background:var(--w-s2);color:var(--w-ink)}
.ic:active{transform:scale(.92)}
.ic svg{width:17px;height:17px}

.log{flex:1;overflow-y:auto;padding:20px 16px 10px;display:flex;flex-direction:column;gap:18px;
  overscroll-behavior:contain}
.log::-webkit-scrollbar{width:10px}
.log::-webkit-scrollbar-thumb{background:var(--w-line2);border:3px solid transparent;
  background-clip:content-box;border-radius:8px}
.msg{overflow-wrap:anywhere;min-width:0}
.msg.new{animation:blmin .42s var(--w-out) both}
@keyframes blmin{from{opacity:0;transform:translateY(8px)}}
.msg.user{align-self:flex-end;max-width:86%;background:var(--w-bubble);color:var(--w-ink);
  border-radius:18px 18px 6px 18px;padding:9px 14px}
.msg.bot{align-self:stretch}
.msg.err .body{color:var(--w-err);border:1px solid color-mix(in srgb,var(--w-err) 45%,transparent);
  background:color-mix(in srgb,var(--w-err) 7%,transparent);border-radius:12px;padding:9px 13px;font-size:13.5px}
.body>*:first-child{margin-top:0}
.body>*:last-child{margin-bottom:0}
.msg p{margin:0 0 10px}
.msg h1,.msg h2,.msg h3{font-size:15px;line-height:1.35;margin:16px 0 8px;font-weight:600;letter-spacing:-.012em}
.msg ul,.msg ol{margin:0 0 10px;padding-left:22px} .msg li{margin:3px 0}
.msg li::marker{color:var(--w-ink3)}
.msg blockquote{margin:0 0 10px;padding-left:12px;border-left:2px solid var(--w-line2);color:var(--w-ink2)}
.msg hr{border:0;border-top:1px solid var(--w-line);margin:14px 0}
.msg table{border-collapse:collapse;font-size:13px;display:block;overflow-x:auto;margin:0 0 10px;max-width:100%}
.msg th,.msg td{border:1px solid var(--w-line);padding:6px 10px;text-align:left}
.msg th{background:var(--w-s1);font-weight:600}
.msg code{font-family:var(--w-mono)}
.msg :not(pre)>code{background:var(--w-s1);border:1px solid var(--w-line);border-radius:5px;
  padding:1px 5px;font-size:12.5px}
.msg img{max-width:100%;max-height:340px;border-radius:12px;margin:6px 0;display:block;cursor:zoom-in;
  border:1px solid var(--w-line);transition:transform .3s var(--w-ease)}
.msg img:hover{transform:scale(1.01)}
.msg a{color:var(--w-ink);text-decoration:underline;text-decoration-color:var(--w-accent);
  text-decoration-thickness:1.5px;text-underline-offset:3px}
.msg a:hover{text-decoration-thickness:2px}
.card{border:1px solid var(--w-line);border-radius:12px;overflow:hidden;margin:0 0 10px;background:var(--w-code)}
.cardhd{display:flex;align-items:center;gap:6px;padding:5px 6px 5px 12px;background:var(--w-s1);
  border-bottom:1px solid var(--w-line);font:500 11px/1 var(--w-mono);color:var(--w-ink3);letter-spacing:.02em}
.cardhd .sp{flex:1}
.cardhd button{border:0;background:none;cursor:pointer;color:var(--w-ink3);display:inline-flex;align-items:center;gap:5px;
  font:500 11px/1 var(--w-mono);padding:5px 8px;border-radius:6px}
.cardhd button:hover{background:var(--w-s2);color:var(--w-ink)}
.cardhd button svg{width:12px;height:12px}
.card pre{margin:0;padding:12px;overflow-x:auto}
.card code{font-size:12.5px;line-height:1.62}
.acts{display:flex;gap:2px;margin-top:6px;opacity:0;transition:opacity .18s var(--w-out)}
.msg.bot:hover .acts,.msg.bot:focus-within .acts,.msg.bot.last .acts,.msg.err .acts{opacity:1}
@media (hover:none){.acts{opacity:1}}
.act{display:inline-flex;align-items:center;gap:5px;height:26px;padding:0 8px;border:0;border-radius:7px;
  background:none;cursor:pointer;color:var(--w-ink3);font-size:12px;
  transition:background-color .16s var(--w-out),color .16s var(--w-out)}
.act:hover{background:var(--w-s2);color:var(--w-ink)}
.act.done{color:var(--w-ok)}
.act svg{width:13px;height:13px}
.think{display:flex;align-items:center;gap:11px;color:var(--w-ink3);font-size:13.5px;height:24px}
.orb{position:relative;width:9px;height:9px;border-radius:50%;background:var(--w-accent);flex:none}
.orb::after{content:"";position:absolute;inset:-5px;border-radius:50%;border:1.5px solid var(--w-accent);
  animation:blmring 1.5s var(--w-out) infinite}
@keyframes blmring{0%{transform:scale(.45);opacity:.8}100%{transform:scale(1.5);opacity:0}}
.num{font-family:var(--w-mono);font-variant-numeric:tabular-nums;font-size:12px}
.cur{display:inline-block;width:7px;height:15px;border-radius:2px;background:var(--w-accent);
  vertical-align:-2px;margin-left:2px;animation:blmblink 1s steps(2,start) infinite}
@keyframes blmblink{50%{opacity:0}}
.hint{color:var(--w-ink3);font-size:13px}
.sugs{display:flex;flex-wrap:wrap;gap:8px;margin-top:-6px}
.sug{border:1px solid var(--w-line2);background:var(--w-canvas);color:var(--w-ink2);border-radius:999px;
  padding:7px 13px;font-size:13px;line-height:1.3;cursor:pointer;text-align:left;
  animation:blmin .46s var(--w-out) both;
  transition:border-color .18s var(--w-out),background-color .18s var(--w-out),color .18s var(--w-out),transform .18s var(--w-ease)}
.sug:hover{border-color:var(--w-accent);background:var(--w-accent-soft);color:var(--w-ink)}
.sug:active{transform:scale(.97)}

/* composer */
.cmpwrap{padding:8px 12px 12px;flex:none}
.cmp{background:var(--w-raise);border:1px solid var(--w-line2);border-radius:18px;padding:8px 8px 8px 14px;
  box-shadow:var(--w-hi),0 1px 2px rgba(16,18,40,.04);
  transition:border-color .18s var(--w-out),box-shadow .22s var(--w-out),background-color .18s var(--w-out)}
.cmp.focus{border-color:var(--w-accent);box-shadow:var(--w-hi),0 0 0 3px var(--w-accent-soft)}
.cmp.drag{border-style:dashed;border-color:var(--w-accent);background:var(--w-accent-soft)}
.cmp textarea{display:block;width:100%;border:0;background:none;outline:none;resize:none;
  font:inherit;color:var(--w-ink);padding:5px 0 3px;max-height:150px;min-height:24px}
.panel.large .cmp textarea{max-height:240px}
.cmp textarea::placeholder{color:var(--w-ink4)}
.row{display:flex;align-items:center;gap:4px;margin-top:4px}
.row .sp{flex:1}
.rnd{width:32px;height:32px;border:0;border-radius:50%;background:none;cursor:pointer;
  color:var(--w-ink3);display:grid;place-items:center;padding:0;
  transition:background-color .16s var(--w-out),color .16s var(--w-out),transform .16s var(--w-ease)}
.rnd:hover{background:var(--w-s2);color:var(--w-ink)}
.rnd:active{transform:scale(.92)}
.rnd.on{color:var(--w-accent);background:var(--w-accent-soft)}
.rnd svg{width:17px;height:17px}
.go{width:32px;height:32px;border:0;border-radius:50%;cursor:pointer;padding:0;
  background:var(--w-accent);color:var(--w-accent-ink);display:grid;place-items:center;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.22);
  transition:background-color .18s var(--w-out),opacity .18s var(--w-out),transform .18s var(--w-ease)}
.go:not(:disabled):hover{background:var(--w-accent-hover)}
.go:not(:disabled):active{transform:scale(.92)}
.go:disabled{opacity:.28;cursor:default}
.go svg{width:16px;height:16px}
.clock{font:500 11.5px/1 var(--w-mono);font-variant-numeric:tabular-nums;color:var(--w-ink3);margin-right:6px}
.atts{display:flex;gap:6px;flex-wrap:wrap;padding:2px 0 8px}
.atts:empty{display:none}
.att{position:relative;width:52px;height:52px;border-radius:10px;overflow:hidden;
  border:1px solid var(--w-line);flex:none;animation:blmin .3s var(--w-out) both}
.att img{width:100%;height:100%;object-fit:cover;display:block;margin:0;border:0;max-height:none;cursor:default}
.att button{position:absolute;top:3px;right:3px;width:18px;height:18px;padding:0;border:0;
  border-radius:50%;background:rgba(8,9,10,.78);color:#fff;font-size:12px;line-height:18px;cursor:pointer}
.msg .atts{padding:0 0 8px}
.msg .att{width:72px;height:72px;animation:none}
.msg .att img{cursor:zoom-in}

/* image viewer */
.lb{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
  gap:14px;padding:24px;background:rgba(8,9,10,.8);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  opacity:0;visibility:hidden;transition:opacity .22s var(--w-out),visibility 0s linear .22s}
.lb.open{opacity:1;visibility:visible;transition:opacity .24s var(--w-out),visibility 0s}
.lb img{max-width:min(92vw,1400px);max-height:calc(100vh - 128px);border-radius:14px;display:block;
  box-shadow:0 30px 90px rgba(0,0,0,.55);transform:scale(.96);transition:transform .38s var(--w-ease)}
.lb.open img{transform:none}
.lb .bar{display:flex;gap:8px}
.lb .bar a,.lb .bar button{display:inline-flex;align-items:center;gap:7px;height:36px;padding:0 15px;
  border-radius:999px;background:rgba(255,255,255,.1);color:#f7f8f8;border:1px solid rgba(255,255,255,.16);
  font:500 13px/1 var(--w-sans);text-decoration:none;cursor:pointer;transition:background-color .16s var(--w-out)}
.lb .bar a:hover,.lb .bar button:hover{background:rgba(255,255,255,.18)}
.lb .bar svg{width:15px;height:15px}

@media (max-width:520px){
  .wrap{bottom:16px;right:16px}
  .wrap[data-side="left"]{left:16px}
  .panel,.panel.large{position:fixed;inset:0;width:100vw;height:100vh;height:100dvh;max-width:none;max-height:none;
    border-radius:0;border:0;transform-origin:bottom center;padding-bottom:env(safe-area-inset-bottom)}
  .hd{padding-top:calc(12px + env(safe-area-inset-top))}
  .grow{display:none}
  .wrap.is-open .fab{opacity:0;pointer-events:none;transform:scale(.8)}
}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
`;
  root.appendChild(style);

  var I = {
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 11.6a8.1 8.1 0 0 1-11.7 7.3L4 20l1.2-4.3a8.1 8.1 0 1 1 15.3-4.1z"/><path d="M8.6 11.5h.01M12 11.5h.01M15.4 11.5h.01" stroke-width="2.4"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></svg>',
    mark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.1"><circle cx="11" cy="13" r="5.6"/><circle cx="18.3" cy="5.7" r="2.2" fill="currentColor" stroke="none"/></svg>',
    fresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4.5H6.5a2 2 0 0 0-2 2v11a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V13"/><path d="M17.6 3.9a2 2 0 0 1 2.8 2.8L12.5 14.6l-3.6.9.9-3.6z"/></svg>',
    grow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4.5h5v5M9.5 19.5h-5v-5M19.5 4.5l-6 6M4.5 19.5l6-6"/></svg>',
    shrink: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M19.5 9.5h-5v-5M4.5 14.5h5v5M14.5 9.5l6-6M9.5 14.5l-6 6"/></svg>',
    plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
    up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5.5M5.8 11.5L12 5.3l6.2 6.2"/></svg>',
    stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2.5"/><path d="M15.5 5.5A1.7 1.7 0 0 0 13.8 4H5.7A1.7 1.7 0 0 0 4 5.7v8.1a1.7 1.7 0 0 0 1.5 1.7"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7"/></svg>',
    retry: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 11a8 8 0 1 0-2.3 5.6"/><path d="M20 4.5V11h-6.5"/></svg>',
    down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11.5M7 11l5 5 5-5M5 20h14"/></svg>',
    chev: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9.5l6 6 6-6"/></svg>',
  };

  var wrap = document.createElement("div");
  wrap.className = "wrap";
  wrap.innerHTML =
    '<div class="panel" part="panel" role="dialog" aria-modal="false">' +
      '<div class="hd">' +
        '<span class="mark" aria-hidden="true">' + I.mark + "</span>" +
        '<div class="tt"><b class="title"></b>' +
          '<label class="model"><span class="live" aria-hidden="true"></span>' +
            '<select class="prov" aria-label="Model"></select><span class="state" hidden></span>' +
            '<span class="chev" aria-hidden="true">' + I.chev + "</span></label>" +
        "</div>" +
        '<button class="ic grow" type="button" title="Larger" aria-label="Larger panel">' + I.grow + "</button>" +
        '<button class="ic fresh" type="button" title="New chat" aria-label="New chat">' + I.fresh + "</button>" +
        '<button class="ic close" type="button" title="Close" aria-label="Close">' + I.close + "</button>" +
      "</div>" +
      '<div class="log"></div>' +
      '<div class="cmpwrap"><div class="cmp">' +
        '<div class="atts"></div>' +
        '<textarea rows="1" placeholder="Ask anything" aria-label="Message"></textarea>' +
        '<div class="row">' +
          '<button class="rnd clip" type="button" title="Attach images" aria-label="Attach images">' + I.plus + "</button>" +
          '<input type="file" class="file" accept="image/*" multiple hidden>' +
          '<span class="sp"></span><span class="clock" aria-hidden="true"></span>' +
          '<button class="go" type="button" title="Send" aria-label="Send">' + I.up + "</button>" +
        "</div>" +
      "</div></div>" +
      '<span class="sr" role="status" aria-live="polite"></span>' +
    "</div>" +
    '<button class="fab" type="button" title="Chat" aria-label="Open chat" aria-expanded="false">' +
      '<span class="i i-chat" aria-hidden="true">' + I.chat + "</span>" +
      '<span class="i i-close" aria-hidden="true">' + I.close + "</span>" +
      '<span class="dot" aria-hidden="true"></span>' +
    "</button>" +
    '<div class="lb" role="dialog" aria-label="Image" aria-hidden="true">' +
      '<img alt="">' +
      '<div class="bar"><a class="dl" target="_blank" rel="noopener" download>' + I.down + "Download</a>" +
        '<button type="button" class="lbx">' + I.close + "Close</button></div>" +
    "</div>";
  root.appendChild(wrap);

  var el = {
    panel: root.querySelector(".panel"),
    title: root.querySelector(".title"),
    model: root.querySelector(".model"),
    prov:  root.querySelector(".prov"),
    state: root.querySelector(".model .state"),
    grow:  root.querySelector(".grow"),
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
    sr:    root.querySelector(".sr"),
    lb:    root.querySelector(".lb"),
    lbImg: root.querySelector(".lb img"),
    lbDl:  root.querySelector(".lb .dl"),
    lbX:   root.querySelector(".lb .lbx"),
  };

  // ---- appearance: theme, side, accent, title, size -----------------------
  /* White or near-black ink on the accent, whichever reads better, so a host
     that pins a pale accent still gets a visible icon. */
  function inkFor(color) {
    var probe = document.createElement("span");
    probe.style.color = color;
    root.appendChild(probe);
    var m = getComputedStyle(probe).color.match(/[\d.]+/g);
    probe.remove();
    if (!m || m.length < 3) return "#ffffff";
    var lum = [0, 1, 2].map(function (k) {
      var c = +m[k] / 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
    });
    var L = 0.2126 * lum[0] + 0.7152 * lum[1] + 0.0722 * lum[2];
    return (1.05 / (L + 0.05)) >= ((L + 0.05) / 0.05) ? "#ffffff" : "#08090a";
  }
  function applyLook() {
    wrap.setAttribute("data-t", /^(light|dark|auto)$/.test(cfg.theme) ? cfg.theme : "light");
    wrap.setAttribute("data-side", cfg.position === "bl" ? "left" : "right");
    if (cfg.accent) {
      wrap.setAttribute("data-accent", "1");
      wrap.style.setProperty("--w-a", cfg.accent);
      wrap.style.setProperty("--w-a-ink", inkFor(cfg.accent));
    } else {
      wrap.removeAttribute("data-accent");
      wrap.style.removeProperty("--w-a");
      wrap.style.removeProperty("--w-a-ink");
    }
    el.title.textContent = cfg.title;
    el.panel.setAttribute("aria-label", cfg.title);
    el.clip.style.display = cfg.attach ? "" : "none";
  }
  var large = lsGet(SIZE_STORE) === "large";
  function applySize() {
    el.panel.classList.toggle("large", large);
    el.grow.innerHTML = large ? I.shrink : I.grow;
    el.grow.title = large ? "Smaller" : "Larger";
    el.grow.setAttribute("aria-label", large ? "Smaller panel" : "Larger panel");
  }

  // ---- events ---------------------------------------------------------------
  var listeners = {};
  var isReady = false;
  function emit(name, data) {
    (listeners[name] || []).slice().forEach(function (fn) {
      try { fn(data); } catch (e) { if (window.console) console.error("BrowserLLMWidget " + name + " listener:", e); }
    });
  }

  // ---- markdown: escape first, then a small block and inline grammar ------
  function esc(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  /* Answers are model output, and a prompt-injected page can make a model
     write any link, so only plain web, mail and same-site links survive. */
  function safeUrl(u, img) {
    if (/^(https?:|mailto:|\/|#|\.\.?\/)/i.test(u)) return u;
    if (img && /^(data:image\/|blob:)/i.test(u)) return u;
    return "#";
  }
  function inline(s) {
    return s
      .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, function (_, alt, u) {
        return '<img src="' + safeUrl(u, true) + '" alt="' + alt + '" loading="lazy">';
      })
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_, t, u) {
        return '<a href="' + safeUrl(u, false) + '" target="_blank" rel="noopener">' + t + "</a>";
      })
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<i>$2</i>")
      .replace(/~~([^~\n]+)~~/g, "<s>$1</s>");
  }
  var FENCE = /^@@BLMCODE(\d+)@@$/;
  function md(srcText) {
    var blocks = [];
    var raw = String(srcText == null ? "" : srcText)
      .replace(/```([^\n`]*)\n?([\s\S]*?)(?:```|$)/g, function (_, lang, code) {
        blocks.push('<div class="card"><div class="cardhd"><span>' + esc(lang.trim() || "code") +
          '</span><span class="sp"></span><button type="button" data-copy>' + I.copy + "Copy</button></div>" +
          "<pre><code>" + esc(code.replace(/\n+$/, "")) + "</code></pre></div>");
        return "\n@@BLMCODE" + (blocks.length - 1) + "@@\n";
      });
    var lines = esc(raw).split("\n"), out = [], i, m;
    for (i = 0; i < lines.length; i++) {
      var ln = lines[i];
      if (/^\s*$/.test(ln)) continue;
      if ((m = ln.match(FENCE))) { out.push(blocks[+m[1]]); continue; }
      if ((m = ln.match(/^(#{1,6})\s+(.*)$/))) { out.push("<h3>" + inline(m[2]) + "</h3>"); continue; }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(ln)) { out.push("<hr>"); continue; }
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

  // ---- thread state, remembered per API base ------------------------------
  var messages = [];          // {role, content, imgs, error}
  var busy = false, ac = null, pinned = true, unread = false;
  var unreadEl = null;        // the answer that finished while the panel was closed
  var queue = [];             // ask() calls waiting for the current answer
  var turnSeq = 0;            // bumped by reset(), so a cut-off turn cannot land late

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
      return '<div class="att"><img src="' + esc(safeUrl(u, true)) + '" alt="Attached image"></div>';
    }).join("") + "</div>";
  }
  function botHtml(m, idx) {
    if (m.error) {
      return '<div class="body">' + esc(m.content) + "</div>" +
        '<div class="acts"><button class="act" type="button" data-retry>' + I.retry + "<span>Retry</span></button></div>";
    }
    var body = m.content ? md(m.content)
      : '<span class="hint">Empty answer. Is this provider still signed in?</span>';
    return '<div class="body">' + body + "</div>" +
      (m.content ? '<div class="acts"><button class="act" type="button" data-copymsg="' + idx + '">' +
        I.copy + "<span>Copy</span></button></div>" : "");
  }
  function markLast() {
    var bots = el.log.querySelectorAll(".msg.bot");
    for (var i = 0; i < bots.length; i++) bots[i].classList.toggle("last", i === bots.length - 1);
  }
  function emptyState() {
    if (cfg.greeting) bubble("bot", '<div class="body">' + md(cfg.greeting) + "</div>");
    if (cfg.suggestions.length) {
      var row = document.createElement("div");
      row.className = "sugs";
      cfg.suggestions.forEach(function (s, k) {
        var b = document.createElement("button");
        b.type = "button"; b.className = "sug"; b.textContent = s;
        b.style.animationDelay = (60 + k * 45) + "ms";
        b.onclick = function () { if (!busy) startTurn(s, [], null); };
        row.appendChild(b);
      });
      el.log.appendChild(row);
    }
  }
  function render() {
    el.log.innerHTML = "";
    if (!messages.length) emptyState();
    messages.forEach(function (m, idx) {
      if (m.role === "user") bubble("user", '<div class="body">' + attsHtml(m) + md(m.content) + "</div>");
      else bubble(m.error ? "bot err" : "bot", botHtml(m, idx));
    });
    markLast();
    pinned = true; scrollDown();
  }
  function bubble(cls, html, fresh) {
    var d = document.createElement("div");
    d.className = "msg " + cls + (fresh ? " new" : "");
    d.innerHTML = html;
    el.log.appendChild(d);
    if (pinned) scrollDown();
    return d;
  }
  function scrollDown() { el.log.scrollTop = el.log.scrollHeight; }
  function showTop(node) {
    if (node.offsetHeight > el.log.clientHeight - 40)
      el.log.scrollTop = node.offsetTop - el.log.offsetTop - 12;
    else scrollDown();
  }
  el.log.addEventListener("scroll", function () {
    pinned = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight < 80;
  });
  function announce(t) { el.sr.textContent = t; }

  // ---- model list ---------------------------------------------------------
  function loadModels() {
    fetch(cfg.base + "/v1/models", { headers: apiHeaders() })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (j) {
        el.prov.innerHTML = "";
        (j.data || []).forEach(function (m) {
          var o = document.createElement("option");
          o.value = m.id; o.textContent = m.id.replace("-browser", "");
          el.prov.appendChild(o);
        });
        if (cfg.provider) el.prov.value = cfg.provider;
        el.model.classList.add("ok");
      })
      .catch(function (e) {
        el.prov.hidden = true;
        el.model.querySelector(".chev").hidden = true;
        el.state.hidden = false;
        el.state.textContent = /401|403/.test(String(e)) ? "needs a key" : "offline";
        el.model.classList.add("off");
      });
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
      fr.onload = function () {
        if (atts.length < MAX_ATTACH) atts.push({ name: f.name || "pasted image", url: fr.result });
        renderAtts();
      };
      fr.readAsDataURL(f);
    });
  }

  // ---- one turn ----------------------------------------------------------
  function syncGo() {
    if (busy) {
      el.go.innerHTML = I.stop; el.go.disabled = false;
      el.go.title = "Stop"; el.go.setAttribute("aria-label", "Stop");
      return;
    }
    el.go.innerHTML = I.up; el.go.title = "Send"; el.go.setAttribute("aria-label", "Send");
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
    startTurn(text, imgs, null);
  }
  /* ctx, when set, is an ask() caller waiting on this answer. */
  function startTurn(text, imgs, ctx) {
    if (!messages.length) el.log.innerHTML = "";        // drop the greeting and chips
    messages.push({ role: "user", content: text, imgs: imgs });
    pinned = true;
    bubble("user", '<div class="body">' + attsHtml(messages[messages.length - 1]) + md(text) + "</div>", true);
    save();
    emit("message", { role: "user", content: text });
    drive(ctx);
  }
  function pump() {
    if (busy || !queue.length) return;
    var job = queue.shift();
    startTurn(job.text, job.imgs, job.ctx);
  }
  function retry() {
    if (busy) return;
    while (messages.length && messages[messages.length - 1].error) messages.pop();
    if (!messages.length || messages[messages.length - 1].role !== "user") { render(); return; }
    save(); render(); drive(null);
  }

  function drive(ctx) {
    busy = true; syncGo();
    var turn = turnSeq;
    var provider = el.prov.value || cfg.provider || "";
    var out = bubble("bot new", '<div class="body"><div class="think"><span class="orb"></span>' +
      '<span>Thinking <span class="st num"></span></span></div></div>');
    announce("Thinking");
    var t0 = Date.now();
    var clock = setInterval(function () {
      var s = Math.floor((Date.now() - t0) / 1000);
      el.clock.textContent = s + "s";
      var st = out.querySelector(".st");
      if (st) st.textContent = s ? "· " + s + "s" : "";
    }, 500);

    var body = { stream: true, messages: payloadMessages() };
    if (provider) body.model = provider;
    var full = "", failed = null, stopped = false, painter = null, lastPaint = 0, paints = 0;
    function paint() {
      painter = null; lastPaint = Date.now(); paints++;
      out.innerHTML = '<div class="body">' + md(full) + '<span class="cur"></span></div>';
      if (pinned) scrollDown();
    }
    ac = window.AbortController ? new AbortController() : null;

    fetch(cfg.base + "/v1/chat/completions", {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
      signal: ac ? ac.signal : undefined,
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (t) {
          var detail = t.slice(0, 300);
          try { detail = JSON.parse(t).detail || detail; } catch (e) { /* not JSON */ }
          throw new Error("HTTP " + res.status + ": " + detail);
        });
      }
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = "";
      function pumpStream() {
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
                if (delta && turn === turnSeq) {
                  full += delta;
                  if (!painter) painter = setTimeout(paint, Math.max(0, 60 - (Date.now() - lastPaint)));
                }
              } catch (e) { /* keep-alives and partial frames */ }
            });
          }
          return pumpStream();
        });
      }
      return pumpStream();
    }).catch(function (e) {
      if (e && e.name === "AbortError") stopped = true;
      else failed = String(e && e.message ? e.message : e);
    }).then(function () {
      clearInterval(clock); clearTimeout(painter);
      el.clock.textContent = "";
      busy = false; ac = null;
      if (turn !== turnSeq) {                       // reset() cut this turn off
        if (ctx) ctx.reject(new Error("reset"));
        syncGo(); pump();
        return;
      }
      out.remove();
      /* a stop with nothing streamed yet leaves no turn behind, rather than an
         empty bubble that reads like a broken provider */
      if (!(stopped && !full)) {
        messages.push(failed ? { role: "assistant", content: failed, error: true }
                             : { role: "assistant", content: full });
        var idx = messages.length - 1, m = messages[idx];
        var done = bubble(m.error ? "bot err" : "bot", botHtml(m, idx), true);
        markLast();
        /* An answer that arrived in one piece (ChatGPT's are buffered) is read
           from its first line, not its last, so bring its top into view. */
        if (pinned && paints <= 1) showTop(done);
        if (!el.panel.classList.contains("open")) unreadEl = done;
      }
      save(); syncGo();
      if (failed) {
        announce("The answer failed");
        emit("error", { message: failed });
        if (ctx) ctx.reject(new Error(failed));
      } else if (stopped) {
        announce("Stopped");
        if (ctx) ctx.reject(new Error("stopped"));
      } else {
        announce("Answer ready");
        emit("message", { role: "assistant", content: full });
        if (ctx) ctx.resolve(full);
      }
      if (!el.panel.classList.contains("open") && !(stopped && !full)) setUnread(true);
      if (el.panel.classList.contains("open")) el.ta.focus({ preventScroll: true });
      pump();
    });
  }

  // ---- image viewer --------------------------------------------------------
  var lbOpener = null;
  function openViewer(srcUrl, alt) {
    lbOpener = root.activeElement;
    el.lbImg.src = srcUrl;
    el.lbImg.alt = alt || "Image";
    el.lbDl.href = srcUrl;
    el.lb.classList.add("open");
    el.lb.setAttribute("aria-hidden", "false");
    el.lbX.focus();
  }
  function closeViewer() {
    if (!el.lb.classList.contains("open")) return false;
    el.lb.classList.remove("open");
    el.lb.setAttribute("aria-hidden", "true");
    if (lbOpener && lbOpener.focus) lbOpener.focus();
    return true;
  }
  el.lb.addEventListener("click", function (e) {
    if (e.target === el.lb) closeViewer();
  });
  el.lbX.onclick = closeViewer;

  // ---- clicks inside the panel: copy, retry, open an image ----------------
  function flash(btn, label) {
    var keep = btn.innerHTML;
    btn.classList.add("done");
    btn.innerHTML = I.check + "<span>" + label + "</span>";
    setTimeout(function () { btn.classList.remove("done"); btn.innerHTML = keep; }, 1500);
  }
  function copyText(t) {
    if (navigator.clipboard && window.isSecureContext !== false) return navigator.clipboard.writeText(t);
    return Promise.reject(new Error("no clipboard"));
  }
  root.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    var b = t.closest("[data-copy]");
    if (b) {
      copyText(b.closest(".card").querySelector("code").textContent)
        .then(function () { flash(b, "Copied"); }, function () { flash(b, "Copy failed"); });
      return;
    }
    var cm = t.closest("[data-copymsg]");
    if (cm) {
      var m = messages[+cm.getAttribute("data-copymsg")];
      copyText((m && m.content) || "")
        .then(function () { flash(cm, "Copied"); }, function () { flash(cm, "Copy failed"); });
      return;
    }
    if (t.closest("[data-retry]")) { retry(); return; }
    var img = t.closest(".msg img");
    if (img && img.src) { e.preventDefault(); openViewer(img.src, img.alt); }
  });

  // ---- open, close, size, reset ------------------------------------------
  function setUnread(on) {
    unread = on;
    el.fab.classList.toggle("unread", on);
    el.fab.setAttribute("aria-label", on ? "Open chat, a new answer is waiting" :
      (el.panel.classList.contains("open") ? "Close chat" : "Open chat"));
  }
  function toggle(force) {
    var isOpen = el.panel.classList.contains("open");
    var open = force === undefined ? !isOpen : !!force;
    if (open === isOpen) return;
    el.panel.classList.toggle("open", open);
    wrap.classList.toggle("is-open", open);
    el.fab.title = open ? "Close chat" : "Chat";
    el.fab.setAttribute("aria-expanded", String(open));
    if (open) {
      pinned = true;
      if (unread && unreadEl && unreadEl.isConnected) showTop(unreadEl); else scrollDown();
      unreadEl = null;
      setUnread(false);
      setTimeout(function () { el.ta.focus({ preventScroll: true }); }, 60);
      emit("open");
    } else {
      closeViewer();
      setUnread(unread);
      emit("close");
    }
  }
  function reset() {
    turnSeq++;
    if (busy && ac) ac.abort();
    queue.splice(0).forEach(function (job) { if (job.ctx) job.ctx.reject(new Error("reset")); });
    messages = []; atts = []; renderAtts();
    try { localStorage.removeItem(STORE); } catch (e) {}
    render();
    if (el.panel.classList.contains("open")) el.ta.focus();
  }
  el.fab.onclick   = function () { toggle(); };
  el.close.onclick = function () { toggle(false); el.fab.focus(); };
  el.fresh.onclick = reset;
  el.grow.onclick  = function () {
    large = !large; applySize(); lsSet(SIZE_STORE, large ? "large" : "");
    if (pinned) scrollDown();
  };
  el.go.onclick    = function () { if (busy) { if (ac) ac.abort(); } else send(); };
  el.ta.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  root.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    if (closeViewer()) { e.preventDefault(); return; }
    if (el.panel.classList.contains("open")) { e.preventDefault(); toggle(false); el.fab.focus(); }
  });
  el.ta.addEventListener("input", function () {
    el.ta.style.height = "auto";
    el.ta.style.height = el.ta.scrollHeight + "px";
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

  applyLook();
  applySize();
  restore();
  render();
  syncGo();
  loadModels();
  if (cfg.open) toggle(true);

  // ---- public handle ------------------------------------------------------
  var api = {
    open: function () { toggle(true); },
    close: function () { toggle(false); },
    reset: reset,
    /* Send text (and optional image URLs or data URLs) as a user turn. Resolves
       with the answer text; rejects on a failed answer, a stop or a reset.
       Calls made while an answer streams wait in a queue and run in order. */
    ask: function (text, opts) {
      opts = opts || {};
      text = String(text == null ? "" : text).trim();
      var imgs = (opts.images || []).map(String).slice(0, MAX_ATTACH);
      if (!text && !imgs.length) return Promise.reject(new Error("ask() needs text or images"));
      if (opts.open !== false) toggle(true);
      return new Promise(function (resolve, reject) {
        queue.push({ text: text, imgs: imgs, ctx: { resolve: resolve, reject: reject } });
        pump();
      });
    },
    on: function (name, fn) {
      if (typeof fn !== "function") return function () {};
      (listeners[name] = listeners[name] || []).push(fn);
      if (name === "ready" && isReady) setTimeout(function () { fn({ base: cfg.base }); }, 0);
      return function () { api.off(name, fn); };
    },
    off: function (name, fn) {
      var l = listeners[name];
      if (!l) return;
      var k = l.indexOf(fn);
      if (k >= 0) l.splice(k, 1);
    },
    config: function (o) {
      o = o || {};
      if (o.provider !== undefined) { cfg.provider = o.provider; if (o.provider) el.prov.value = o.provider; }
      if (o.system !== undefined) cfg.system = o.system;
      if (o.title !== undefined) cfg.title = String(o.title || "Ask AI");
      if (o.accent !== undefined) cfg.accent = o.accent || "";
      if (o.theme !== undefined && /^(light|dark|auto)$/.test(o.theme)) cfg.theme = o.theme;
      if (o.position !== undefined) cfg.position = o.position === "bl" ? "bl" : "br";
      if (o.attach !== undefined) cfg.attach = !!o.attach && o.attach !== "0";
      var empty = !messages.length;
      if (o.greeting !== undefined) cfg.greeting = o.greeting;
      if (o.suggestions !== undefined) cfg.suggestions = splitSuggestions(o.suggestions);
      applyLook();
      if (empty && (o.greeting !== undefined || o.suggestions !== undefined)) render();
    },
    /* Take the widget off the page (a host that re-creates it, or a page leaving). */
    destroy: function () {
      turnSeq++;
      if (ac) ac.abort();
      queue.splice(0).forEach(function (job) { if (job.ctx) job.ctx.reject(new Error("destroyed")); });
      host.remove();
      listeners = {};
      try { delete window.BrowserLLMWidget; delete window.__browserLlmWidget; }
      catch (e) { window.BrowserLLMWidget = undefined; window.__browserLlmWidget = undefined; }
    },
  };
  window.__browserLlmWidget = true;
  window.BrowserLLMWidget = api;
  isReady = true;
  emit("ready", { base: cfg.base });
  try { window.dispatchEvent(new CustomEvent("browserllm:ready", { detail: api })); } catch (e) {}
})();
