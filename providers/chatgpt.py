"""
ChatGPT provider — drives chatgpt.com.

Unlike Gemini, ChatGPT's UI is plain DOM (no shadow piercing needed). The last
assistant turn and the answer body inside it are found through the selector
lists below. Generated images (GPT-image / DALL·E) render as ``<img>`` served
from ``oaiusercontent.com`` (or a ``blob:`` URL mid-stream); we read them to
base64 inside the page, falling back to the remote URL if CORS blocks the fetch.

NOTE: ChatGPT changes its page without notice and sits behind Cloudflare/anti-bot
checks. Every selector here was measured on a real signed-in session (dates
below); when a request fails with "input box not found" or an empty answer,
read the live page over CDP before changing anything.
"""
import json
import logging
import re

from .base import Provider, RateLimited

logger = logging.getLogger("gemini_server")


# --- page selectors ---------------------------------------------------------
# On 11 Oct 2026 ChatGPT shipped a new page that dropped `#prompt-textarea`,
# `data-message-author-role` and nearly every `data-testid` at once, and every
# request failed at "input box not found" for a day and a half. Each selector is
# therefore a LIST that keeps the earlier layout and adds the one measured live
# on 12 Oct 2026 (signed-in Free account), so a page serving either one works:
#   composer  div.ProseMirror[contenteditable] inside form[data-chatgpt-composer],
#             aria-label "Ask ChatGPT", no id; CDP char events still type into it
#   send      the form's button[type=submit], aria-label "Send", only after typing
#   stop      the form's button[aria-label="Stop"] while an answer streams, and
#             the turn's [data-talvt-turn-state="in_progress"]
#   turns     [data-chatgpt-search-unit-key="<turn>:<n>:user|assistant"]; the
#             answer body is [data-markdown-text-style="assistant-message"]
#   upload    four hidden file inputs with aria-labels and no test ids; a chip is
#             button[aria-label="Remove <file name>"]; the sent image renders in
#             the user turn, OUTSIDE the bubble, as a blob: <img>
_COMPOSER_SEL = ", ".join([
    '#prompt-textarea',
    'form[data-chatgpt-composer] [contenteditable="true"]',
    'form .ProseMirror[contenteditable="true"]',
])
_USER_TURN_SEL = ", ".join([
    '[data-message-author-role="user"]',
    '[data-chatgpt-search-unit-key$=":user"]',
    '[data-content-search-unit-key$=":user"]',
    '[data-user-message-bubble]',
])

# In-page helpers shared by the reads below (ES5, pasted inside each IIFE):
# _turns() = the assistant turns in document order, _body(turn) = the answer's
# markdown root, _COMPOSER = the composer selector list.
_PAGE_HELPERS_JS = """
  var _COMPOSER=__COMPOSER__;
  var _turns=function(){
    var m=document.querySelectorAll('[data-message-author-role="assistant"]');
    if(!m.length) m=document.querySelectorAll(
      '[data-chatgpt-search-unit-key$=":assistant"], [data-content-search-unit-key$=":assistant"]');
    if(!m.length) m=Array.prototype.map.call(
      document.querySelectorAll('[data-conversation-role="assistant"]'),
      function(h){ return h.parentElement; });
    return m;
  };
  var _body=function(turn){
    return turn.querySelector('.markdown')
      ||turn.querySelector('[data-markdown-text-style="assistant-message"]')
      ||turn.querySelector('.prose')||turn;
  };
""".replace("__COMPOSER__", json.dumps(_COMPOSER_SEL))


# Shared image predicates. `_isGenImg` matches ChatGPT's generated-image URLs;
# `_isInput` excludes images WE uploaded as input (a vision request) — those
# render with the very same blob:/content? URLs, but inside the user's turn or
# the composer's file tile. Without this exclusion an uploaded image counts as
# "generated": the completion tracker fires image-stability immediately (cutting
# the text answer off) and the upload gets echoed back and saved to the gallery.
# Since the 11 Oct 2026 page the sent image sits in the user turn but outside
# the bubble, so the turn itself has to match, not only the bubble.
_IMG_PREDICATES_JS = """
  const _isGenImg=(src,alt)=>{src=src||'';alt=(alt||'').trim().toLowerCase();
    return src.indexOf('backend-api/estuary')>=0||src.indexOf('backend-api/files')>=0
      ||src.indexOf('oaiusercontent')>=0||src.indexOf('/content?')>=0
      ||src.indexOf('blob:')===0||alt.indexOf('generated image')===0;};
  const _isInput=(im)=>{ try{
    return !!(im.closest(__USER_TURN__)
      || im.closest('form') || im.closest('[class*="file-tile"]')
      || im.closest('[data-composer-attachments]'));
  }catch(e){ return false; } };
""".replace("__USER_TURN__", json.dumps(_USER_TURN_SEL))

# Generated-image render status. Only images served from oaiusercontent/blob are
# counted (avoids counting UI icons/avatars, which would wrongly suppress plain
# text responses), and never our own uploaded input images.
_IMG_STATUS_JS = """
(function(){
  // A generated image can render in the assistant turn OR in a separate
  // image-generation tile, so scan the whole conversation. Each request opens
  // a fresh chat, so there are no stale images to confuse this.
""" + _IMG_PREDICATES_JS + """
  const imgs=Array.from(document.querySelectorAll('img'));
  let loaded=0,pending=0;
  imgs.forEach(im=>{
    if(!_isGenImg(im.currentSrc||im.src||'', im.alt||'')) return;
    if(_isInput(im)) return;
    if(im.naturalWidth>256) loaded++; else pending++;
  });
  // Detect the image-generation progress state. GPT-image renders on a <canvas>
  // (a dotted-grid "One last tweak…" tile) before committing the final <img>.
  // Only a LARGE, image-shaped canvas counts: ChatGPT's code/Canvas editors
  // (Monaco/CodeMirror) also use <canvas> for their minimap/gutter, but those
  // are narrow — treating them as "generating" suppressed text and hung the
  // request for big code answers. The image-render canvas is >=256px both ways.
  const main=(document.querySelector('main')||document.body);
  const txt=(main && main.innerText || '').toLowerCase();
  let bigCanvas=false;
  if(main){ for(const c of main.querySelectorAll('canvas')){
    const w=c.clientWidth||c.width||0, h=c.clientHeight||c.height||0;
    if(Math.min(w,h)>=256){ bigCanvas=true; break; }
  }}
  const creating=bigCanvas || /creating image|generating image|making the image|creating the image|creating your image|one last tweak/i.test(txt);
  return JSON.stringify({loaded:loaded,pending:pending,creating:creating});
})()
"""

# Read each generated image to base64 from inside the page. blob: URLs read
# fine; signed oaiusercontent URLs may be CORS-blocked — then we keep the
# remote src so the caller still gets a usable link.
_GET_IMAGES_JS = """
(async function(){
""" + _IMG_PREDICATES_JS + """
  const imgs=Array.from(document.querySelectorAll('img'));
  const seen=new Set(); const out=[];
  for(const im of imgs){
    const src=im.currentSrc||im.src; if(!src) continue;
    if(!_isGenImg(src, im.alt||'')) continue;
    if(_isInput(im)) continue;
    if(im.naturalWidth<=256) continue;
    if(seen.has(src)) continue; seen.add(src);
    const rec={mime:'image/png', alt:(im.alt||'').trim(), src:src};
    try{
      const r=await fetch(src); const b=await r.blob(); const buf=await b.arrayBuffer();
      const by=new Uint8Array(buf); let s=''; const CH=0x8000;
      for(let i=0;i<by.length;i+=CH){ s+=String.fromCharCode.apply(null, by.subarray(i,i+CH)); }
      rec.mime=b.type||'image/png'; rec.b64=btoa(s);
    }catch(e){ /* CORS or unreadable — fall back to remote src URL */ }
    out.push(rec);
  }
  return JSON.stringify(out);
})()
"""


class ChatGPTProvider(Provider):
    name = "chatgpt-browser"
    chat_url = "https://chatgpt.com/"
    profile_dir = "./chatgpt_profile"
    stream_url_fragments = ["backend-api/conversation", "/f/conversation"]
    # ChatGPT streams tokens over a WebSocket (ws.chatgpt.com), so the HTTP
    # stream-done signal never fires. We can't read completion out of the
    # multiplexed frames, but their arrival is a "still streaming" heartbeat that
    # keeps the deadline from truncating a long answer mid-generation.
    ws_url_fragments = ["chatgpt.com"]
    supports_images = True
    # The chatgpt.com app session cookie — its presence is the definitive
    # signed-in signal (set only after the OAuth callback fully completes).
    session_cookie = ("__Secure-next-auth.session-token", "chatgpt.com")
    image_text_is_caption = True  # ChatGPT's prose alongside an image is a real caption
    # Code answers reshape at the end (flattened while streaming → ```fenced``` once
    # the CodeMirror card finalizes), which append-only delta streaming can't represent
    # correctly. Buffer and emit the final text once. See Provider.buffered_stream.
    buffered_stream = True
    # ProseMirror contenteditable composer (selector list: see the top of the file).
    input_selector = _COMPOSER_SEL
    # ONE entry holding every layout's send button: base.open_and_send waits up
    # to 3 s per entry, so separate entries would cost 3 s per layout not shown.
    send_selectors = [", ".join([
        'form[data-chatgpt-composer] button[type="submit"]',  # 11 Oct 2026: "Send"
        'form button[aria-label="Send"]',
        'button[data-testid="send-button"]',                   # earlier layouts
        'button[data-testid="composer-send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Send message"]',
    ])]
    load_wait = 7.0  # ChatGPT can be slow to hydrate / may show a bot check
    user_turn_selector = _USER_TURN_SEL
    # --- image input (upload). Verified live 2026-07-28 and again 2026-10-12:
    # chatgpt.com keeps hidden file inputs in the DOM at rest (until 11 Oct 2026
    # `input[data-testid="upload-photos-input"]`, since then four inputs with
    # aria-labels only: "Take a photo", "Attach photos or videos", "Attach
    # photos", "Attach files"). The generic input path ranks image inputs first;
    # the top-ranked one took the file on 12 Oct. The click path below is only a
    # fallback if every input disappears, and its menu labels are unverified
    # since the 11 Oct page.
    supports_history = True
    site_origin = "https://chatgpt.com"
    supports_upload = True
    attach_click_path = [
        ['button[aria-label="Add files and more"]', 'button[data-testid="composer-plus-btn"]',
         'text:add photos & files', 'text:add files and more'],
        ['text:add photos & files', 'text:upload from computer', 'text:add photos'],
    ]
    # One chip per accepted file. Earlier layouts: a 144px "file tile" with
    # `button[aria-label="Remove file N: <name>"]`. Since 11 Oct 2026: a tile in
    # `[data-composer-attachments]` with `button[aria-label="Remove <name>"]`.
    # Busy = the form still shows a progress indicator (true on both layouts).
    # A wrong chip count is not harmless: _attach_via_input reads "no chip" as
    # "wrong input" and attaches the same file to the next input, up to four.
    attachment_ready_js = """
    (function(){
      var form=document.querySelector('form[data-chatgpt-composer]')
        ||document.querySelector('form')||document.body;
      var chips=document.querySelectorAll('button[aria-label^="Remove file"]').length;
      if(!chips) chips=form.querySelectorAll('button[aria-label^="Remove "]').length;
      if(!chips) chips=document.querySelectorAll('[class*="file-tile"] img').length;
      if(!chips) chips=document.querySelectorAll('[data-composer-attachments] img').length;
      var busy=!!form.querySelector('[role="progressbar"], progress');
      return JSON.stringify({ready:chips, busy:busy});
    })()
    """

    async def get_response_text(self, page) -> str:
        # ChatGPT renders code blocks as CodeMirror editors (.cm-editor / .cm-content),
        # NOT plain <pre><code class="language-x">. A naive innerText read flattens the
        # code card's toolbar (the "Python" language pill + Copy/Run buttons) in with the
        # code and drops the markdown fence, so answers with code come back as
        # "Python\nRun\ndef ...". This serializer walks the assistant .markdown read-only,
        # emitting prose as text and each code editor as a ``` fenced block (language from
        # the toolbar), skipping the toolbar chrome. Verified against the live DOM 2026-07-08.
        try:
            result = await page.evaluate(r"""
                (function(){""" + _PAGE_HELPERS_JS + r"""
                    // Extract the real code from a CodeMirror editor (or plain <pre>).
                    function codeText(el){
                        var content=(el.classList&&el.classList.contains('cm-content'))
                            ? el : el.querySelector('.cm-content');
                        if(content){
                            var lines=content.querySelectorAll('.cm-line');
                            if(lines.length) return Array.prototype.map.call(lines,
                                function(l){return l.textContent;}).join('\n');
                            return content.textContent||'';
                        }
                        var code=el.querySelector('code');
                        return (code?code.textContent:el.textContent)||'';
                    }
                    // Smallest ancestor of the editor that also holds the toolbar
                    // (its innerText == the flattened "Python\nRun\n<code>" chunk in msg).
                    function findCard(editor){
                        // Since 11 Oct 2026 the whole card (language label, Copy
                        // and Run toolbar, editor) is one element, and the editor
                        // pane holds its own "Scroll to bottom" button, so the
                        // button climb below would stop inside the pane and leave
                        // "Python\nRun" in the prose (measured 12 Oct).
                        var block=editor.closest&&editor.closest('[data-markdown-copy="code-block"]');
                        if(block) return block;
                        var card=editor, n=0;
                        while(card.parentElement && n<8
                              && !(card.querySelector && card.querySelector('button'))){
                            card=card.parentElement; n++;
                        }
                        return card;
                    }
                    // The language pill sometimes sits in a header ABOVE the first
                    // ancestor that holds a button, so findCard stopped short: the fence
                    // came out with no language and the bare label ("Bash") stayed behind
                    // in the prose. Climb while the only extra text is that one short
                    // label line, so the fence swallows it.
                    function widen(card){
                        var node=card, n=0;
                        while(node.parentElement && n<3){
                            var p=node.parentElement;
                            var inner=(node.innerText||'').replace(/\s+$/,'');
                            var outer=(p.innerText||'').replace(/\s+$/,'');
                            if(!inner || !outer) break;
                            if(outer===inner){ node=p; n++; continue; }
                            if(outer.slice(-inner.length)!==inner) break;
                            var extra=outer.slice(0,outer.length-inner.length).trim();
                            if(/^[a-z0-9+#.\-]{1,15}$/i.test(extra)) return p;
                            break;
                        }
                        return card;
                    }
                    // Language pill is toolbar text (no language-* class on cm <code>).
                    function langOf(card){
                        try{
                            var first=(((card.innerText||'').trim().split('\n')[0])||'').trim().toLowerCase();
                            if(/^[a-z0-9+#.\-]{1,15}$/.test(first)
                               && !/^(copy|copy code|run|edit|share|preview|code)$/.test(first)) return first;
                            // 11 Oct 2026 page: the editor names its language too.
                            var tagged=card.querySelector&&card.querySelector('.cm-content[data-language]');
                            var lang=tagged?(tagged.getAttribute('data-language')||'').trim().toLowerCase():'';
                            if(/^[a-z0-9+#.\-]{1,15}$/.test(lang)) return lang;
                        }catch(e){}
                        return '';
                    }
                    var msgs=_turns();
                    var msg='';
                    if(msgs.length){
                        var last=msgs[msgs.length-1];
                        var md=_body(last);
                        // Transient status text ("Analyzing image" while a vision
                        // request is processed) lives in the SAME .markdown node,
                        // marked with loading-shimmer/aria-busy. It is not the
                        // answer: returning it made short vision requests complete
                        // early with the placeholder as the whole reply.
                        if(/loading-shimmer|aria-busy/.test(md.className||'')
                           || md.getAttribute('aria-busy')==='true') return '';
                        // Base text = innerText (clean prose, correct list markers). Then
                        // splice each flattened code card into a ``` fenced block. Matching
                        // innerText-to-innerText keeps the substitution reliable and leaves
                        // all prose untouched.
                        msg=(md.innerText||md.textContent||'').trim();
                        var editors=md.querySelectorAll('.cm-editor, #code-block-viewer');
                        if(!editors.length){
                            editors=Array.prototype.filter.call(md.querySelectorAll('pre'),
                                function(p){var c=p.querySelector('code');
                                    return c && (c.textContent||'').trim().length>0;});
                        }
                        var seen=[];
                        for(var e=0;e<editors.length;e++){
                            var editor=editors[e];
                            var card=widen(findCard(editor));
                            if(seen.indexOf(card)>=0) continue; seen.push(card);
                            var clean=codeText(editor).replace(/\s+$/,'');
                            if(!clean) continue;
                            var fence='```'+langOf(card)+'\n'+clean+'\n```';
                            var chunk=(card.innerText||'').replace(/\s+$/,'');
                            var idx=chunk?msg.indexOf(chunk):-1;
                            if(idx>=0){ msg=msg.slice(0,idx)+fence+msg.slice(idx+chunk.length); }
                            else { var ci=msg.indexOf(clean);   // context mismatch: fence code in place
                                   if(ci>=0) msg=msg.slice(0,ci)+fence+msg.slice(ci+clean.length); }
                        }
                        msg=msg.replace(/\n{3,}/g,'\n\n').trim();
                    }
                    // Canvas / textdoc side-panel: large code/doc answers render in a
                    // side editor (CodeMirror/Monaco/ProseMirror), NOT in .markdown — the
                    // message body is then just a short intro ("Here's the file:") or a
                    // deferral. The old `msg.length>=40` gate returned that intro verbatim
                    // and never looked at the canvas, so big answers came back as a stub.
                    // Each request opens a FRESH chat (open_and_send), so any editor on the
                    // page belongs to THIS answer — never a prior turn — so it's safe to
                    // always read it and return whichever is the larger, real payload.
                    // (CodeMirror virtualizes offscreen lines, so a very long canvas can
                    // still come back partial — a known limitation, not fixed here.)
                    // The composer is a ProseMirror too; never read it as a canvas.
                    var composer=document.querySelector(_COMPOSER);
                    var isComposer=function(el){ return (composer&&(el===composer||el.contains(composer)
                        ||composer.contains(el)))||el.id==='prompt-textarea'
                        ||!!(el.closest&&el.closest('form')); };
                    var canvasTxt='';
                    var consider=function(el){ if(!el||isComposer(el))return;
                        var t=(el.innerText||el.textContent||'').trim();
                        if(t.length>canvasTxt.length)canvasTxt=t; };
                    document.querySelectorAll('.cm-content').forEach(consider);
                    document.querySelectorAll('.monaco-editor .view-lines').forEach(consider);
                    document.querySelectorAll('.ProseMirror').forEach(consider);
                    // Return the larger of {message body, canvas payload}. Inline code
                    // blocks live INSIDE .markdown and are already fenced into `msg`, so
                    // msg is a superset of them and always wins — only a genuine SIDE
                    // canvas (a separate editor, not in .markdown) can exceed msg. Require
                    // >40 chars so a stray/empty editor never beats a short prose reply.
                    return (canvasTxt.length>40 && canvasTxt.length>msg.length) ? canvasTxt : msg;
                })()
            """)
            return result if isinstance(result, str) else ""
        except Exception:
            return ""

    async def is_generating(self, page) -> bool:
        try:
            v = await page.evaluate("""
                (function(){""" + _PAGE_HELPERS_JS + """
                    if(document.querySelector('[data-testid="stop-button"]')) return true;
                    if(document.querySelector('button[aria-label="Stop streaming"]')) return true;
                    if(document.querySelector('button[aria-label="Stop generating"]')) return true;
                    // Since 11 Oct 2026 the composer's send button turns into a
                    // plain "Stop" and the turn says in_progress. Both cleared at
                    // the end of every answer measured on 12 Oct; a signal that
                    // stuck would hold every request to the deadline, so add one
                    // here only after watching it clear.
                    if(document.querySelector('form button[aria-label="Stop"]')) return true;
                    if(document.querySelector('[data-talvt-turn-state="in_progress"]')) return true;
                    if(document.querySelector('.result-streaming')) return true;
                    // Still working: the assistant bubble is showing shimmering
                    // status text ("Analyzing image" on a vision request). The
                    // stop button can be absent during that phase, which used to
                    // look like "settled" and ended the poll before the answer.
                    var msgs=_turns();
                    if(msgs.length){
                        var last=msgs[msgs.length-1];
                        var md=_body(last);
                        if(/loading-shimmer|aria-busy/.test(md.className||'')
                           || md.getAttribute('aria-busy')==='true') return true;
                    }
                    return false;
                })()
            """)
            return bool(v)
        except Exception:
            return False

    async def image_status(self, page) -> dict:
        try:
            raw = await page.evaluate(_IMG_STATUS_JS)
            if isinstance(raw, str):
                return json.loads(raw)
        except Exception:
            pass
        return {"loaded": 0, "pending": 0, "creating": False}

    async def get_images(self, page) -> list:
        try:
            raw = await page.evaluate(_GET_IMAGES_JS, await_promise=True, return_by_value=True)
            if isinstance(raw, str):
                # Keep records with either inline b64 or a remote src URL.
                return [d for d in json.loads(raw) if d.get("b64") or d.get("src")]
        except Exception as e:
            logger.warning(f"[{self.name}] image extraction failed: {e}")
        return []

    # ---- account-side history ---------------------------------------------
    # All of it goes through chatgpt.com's own backend API rather than the
    # sidebar: driving the menu means a click path per row, it breaks on every
    # sidebar redesign, and the sidebar only holds what has been scrolled into
    # view. The page's session cookie authorises the fetch and
    # /api/auth/session hands out the bearer token the API wants.
    _CONV_ID_RE = re.compile(r"^[0-9a-f-]{36}$", re.I)

    _CONV_ID_JS = r"""
    (function(){
      const m = location.pathname.match(/\/c\/([0-9a-f-]{36})/i);
      return m ? m[1] : '';
    })()
    """

    # Delete = PATCH is_visible:false, exactly what the sidebar's own Delete
    # does (a soft delete, recoverable by support and by nothing in the UI).
    _DELETE_JS = """
    (async () => {
      const s = await (await fetch('/api/auth/session', {cache: 'no-store'})).json();
      if (!s || !s.accessToken) return 'no access token';
      const r = await fetch('/backend-api/conversation/__ID__', {
        method: 'PATCH',
        headers: {Authorization: 'Bearer ' + s.accessToken,
                  'Content-Type': 'application/json'},
        body: JSON.stringify({is_visible: false}),
      });
      return r.ok ? 'ok' : 'HTTP ' + r.status;
    })()
    """

    # The whole list in ONE page call, with the PATCHes in flight together.
    # One id per call was the naive shape and it cost ~4.2s each: a real
    # 196-chat clear-out on 2026-08-20 ran 13 minutes, and the browser gave up
    # waiting long before the end, so the operator saw no result at all.
    #
    # Eight lanes, measured, not guessed: 36 ids took 34.6s at four, 17.5s at
    # eight, and 17.2s at sixteen — with an HTTP 500 appearing at sixteen. The
    # site stops rewarding parallelism around eight, so that is where this sits.
    # A 429 or a 5xx gets a pause and a retry (the sibling per-thread READ
    # endpoint does rate-limit); a 404 means the thread is already gone and is
    # reported as-is rather than retried.
    _DELETE_MANY_JS = r"""
    (async () => {
      const IDS = __IDS__, LANES = 8, RETRIES = 2;
      const out = {ok: [], failed: {}, error: null};
      if (!IDS.length) return JSON.stringify(out);
      let tok = '';
      try {
        const s = await (await fetch('/api/auth/session', {cache: 'no-store'})).json();
        tok = (s && s.accessToken) || '';
      } catch (e) { out.error = 'session read failed: ' + e; return JSON.stringify(out); }
      if (!tok) { out.error = 'not signed in (no access token)'; return JSON.stringify(out); }
      const H = {Authorization: 'Bearer ' + tok, 'Content-Type': 'application/json'};
      const body = JSON.stringify({is_visible: false});
      const nap = (ms) => new Promise(r => setTimeout(r, ms));
      let next = 0;
      const lane = async () => {
        while (next < IDS.length) {
          const id = IDS[next++];
          let why = '';
          for (let attempt = 0; attempt <= RETRIES; attempt++) {
            try {
              const r = await fetch('/backend-api/conversation/' + id,
                                    {method: 'PATCH', headers: H, body});
              if (r.ok) { out.ok.push(id); why = ''; break; }
              why = 'HTTP ' + r.status;
              if (r.status !== 429 && r.status < 500) break;
            } catch (e) { why = String(e); }
            await nap(1200 * (attempt + 1));
          }
          if (why) out.failed[id] = why;
        }
      };
      await Promise.all(Array.from({length: Math.min(LANES, IDS.length)}, lane));
      return JSON.stringify(out);
    })()
    """

    # Shared by the list and the single-thread read.
    _TIME_JS = """
      // The list endpoint hands out ISO strings and the conversation payload
      // epoch seconds. Take either, give back epoch ms.
      const ms = (v) => {
        if (typeof v === 'number' && v > 0) return Math.round(v * 1000);
        if (typeof v === 'string' && v) return Date.parse(v) || null;
        return null;
      };
      const token = async () => {
        const s = await (await fetch('/api/auth/session', {cache:'no-store'})).json();
        return (s && s.accessToken) || '';
      };
    """

    # Titles only, paged. This endpoint is not rate-limited (verified
    # 2026-08-20: five pages back to back, all 200) — unlike the per-thread
    # read below, which is, so an import must never walk the whole account
    # through that one.
    #
    # Do NOT trust the payload's `total`: it comes back as offset + page + 1
    # (29, 57, 85 … as you page), so it describes the request, not the account.
    # A short page is the only honest end-of-list signal.
    _LIST_JS = r"""
    (async () => {
      const LIMIT = __LIMIT__, OFFSET = __OFFSET__;
      const out = {conversations: [], has_more: false, error: null};
      __TIME__
      let tok = '';
      try { tok = await token(); }
      catch (e) { out.error = 'session read failed: ' + e; return JSON.stringify(out); }
      if (!tok) { out.error = 'not signed in (no access token)'; return JSON.stringify(out); }
      const H = {Authorization: 'Bearer ' + tok};

      let offset = OFFSET;
      while (out.conversations.length < LIMIT) {
        const want = Math.min(28, LIMIT - out.conversations.length);
        let j = null;
        try {
          const r = await fetch('/backend-api/conversations?offset=' + offset +
                                '&limit=' + want + '&order=updated', {headers: H});
          if (!r.ok) { out.error = 'list HTTP ' + r.status; break; }
          j = await r.json();
        } catch (e) { out.error = 'list failed: ' + e; break; }
        const items = j.items || [];
        for (const it of items) {
          out.conversations.push({
            id: it.id,
            title: String(it.title || '').trim() || 'Untitled',
            created: ms(it.create_time),
            updated: ms(it.update_time) || ms(it.create_time),
          });
        }
        offset += items.length;
        if (items.length < want) break;      // short page = end of the list
        out.has_more = true;
      }
      return JSON.stringify(out);
    })()
    """

    # One thread's messages. Rate-limited by the site (429 "Too many requests"
    # after a burst, no Retry-After, and the block outlasts a 36s wait), which
    # is why nothing here loops over the account.
    _ONE_JS = r"""
    (async () => {
      const ID = '__ID__';
      const out = {conversation: null, error: null, status: 0};
      __TIME__
      let tok = '';
      try { tok = await token(); }
      catch (e) { out.error = 'session read failed: ' + e; return JSON.stringify(out); }
      if (!tok) { out.error = 'not signed in (no access token)'; return JSON.stringify(out); }

      let j = null;
      try {
        const r = await fetch('/backend-api/conversation/' + ID,
                              {headers: {Authorization: 'Bearer ' + tok}});
        out.status = r.status;
        if (!r.ok) {
          out.error = r.status === 429
            ? 'the site is rate-limiting reads of your history; wait a minute and try again'
            : 'HTTP ' + r.status;
          return JSON.stringify(out);
        }
        j = await r.json();
      } catch (e) { out.error = 'read failed: ' + e; return JSON.stringify(out); }

      // A conversation is a tree of nodes; the thread a person actually sees is
      // the path from current_node back to the root, so walk parents and
      // reverse. Regenerated branches hang off that path and stay out.
      const map = j.mapping || {};
      const chain = [];
      const seen = {};
      let node = j.current_node;
      while (node && map[node] && !seen[node]) {
        seen[node] = 1;
        chain.push(map[node]);
        node = map[node].parent;
      }
      chain.reverse();
      const msgs = [];
      for (const n of chain) {
        const m = n.message;
        if (!m) continue;
        const role = m.author && m.author.role;
        if (role !== 'user' && role !== 'assistant') continue;
        if ((m.metadata || {}).is_visually_hidden_from_conversation) continue;
        const c = m.content || {};
        let text = '';
        if (c.content_type === 'text') {
          text = (c.parts || []).join('\n');
        } else if (c.content_type === 'multimodal_text') {
          text = (c.parts || []).map(p =>
            typeof p === 'string' ? p : (p && p.asset_pointer ? '[image]' : '')
          ).join('\n');
        } else {
          continue;   // reasoning, tool calls, code interpreter output
        }
        // ChatGPT wraps canvas-style blocks in its own directive syntax
        // (`:::writing{variant="chat_message"}` … `:::`). Those lines are
        // markup for its renderer, not something a person wrote, so they read
        // as stray punctuation anywhere else.
        text = (text || '')
          .split('\n')
          .filter(ln => !/^\s*:::\s*([a-z_-]+\s*(\{[^}]*\})?)?\s*$/i.test(ln))
          .join('\n')
          .trim();
        if (!text) continue;
        msgs.push({role: role, content: text, ts: ms(m.create_time)});
      }
      out.conversation = {
        id: ID,
        title: String(j.title || '').trim() || 'Untitled',
        created: ms(j.create_time),
        updated: ms(j.update_time) || ms(j.create_time),
        messages: msgs,
      };
      return JSON.stringify(out);
    })()
    """

    async def _read_json(self, page, js: str) -> dict:
        raw = await page.evaluate(js, await_promise=True, return_by_value=True)
        try:
            return json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as e:
            raise RuntimeError(f"could not read the history payload: {e}") from None

    async def list_conversations(self, page, limit: int = 200,
                                 offset: int = 0) -> dict:
        js = (self._LIST_JS
              .replace("__TIME__", self._TIME_JS)
              .replace("__LIMIT__", str(max(1, int(limit))))
              .replace("__OFFSET__", str(max(0, int(offset)))))
        data = await self._read_json(page, js)
        if data.get("error") and not data.get("conversations"):
            raise RuntimeError(data["error"])
        convs = data.get("conversations") or []
        logger.info(f"[{self.name}] listed {len(convs)} conversation(s) from the account")
        return {"conversations": convs, "has_more": bool(data.get("has_more")),
                "warning": data.get("error")}

    async def fetch_conversation(self, page, conv_id: str) -> dict:
        conv_id = (conv_id or "").strip()
        if not self._CONV_ID_RE.match(conv_id):
            raise ValueError("malformed conversation id")
        js = self._ONE_JS.replace("__TIME__", self._TIME_JS).replace("__ID__", conv_id)
        data = await self._read_json(page, js)
        conv = data.get("conversation")
        if not conv:
            err = data.get("error") or "the conversation could not be read"
            if data.get("status") == 429:
                raise RateLimited(err)
            raise RuntimeError(err)
        logger.info(f"[{self.name}] read conversation {conv_id[:8]}… "
                    f"({len(conv.get('messages') or [])} messages)")
        return conv

    async def conversation_id(self, page) -> str:
        try:
            v = await page.evaluate(self._CONV_ID_JS, return_by_value=True)
        except Exception as e:
            logger.warning(f"[{self.name}] conversation id read failed: {e}")
            return ""
        if isinstance(v, str):
            return v
        logger.warning(f"[{self.name}] conversation id came back as {type(v).__name__}: {v!r}")
        return ""

    async def delete_conversation(self, page, conv_id: str) -> bool:
        conv_id = (conv_id or "").strip()
        if not self._CONV_ID_RE.match(conv_id):
            logger.warning(f"[{self.name}] refusing to delete a malformed id")
            return False
        try:
            v = await page.evaluate(self._DELETE_JS.replace("__ID__", conv_id),
                                    await_promise=True, return_by_value=True)
        except Exception as e:
            logger.warning(f"[{self.name}] could not delete the conversation: {e}")
            return False
        if v == "ok":
            logger.info(f"[{self.name}] conversation {conv_id[:8]}… deleted")
            return True
        logger.warning(f"[{self.name}] could not delete {conv_id[:8]}…: {v}")
        return False

    async def delete_conversations(self, page, ids: list) -> tuple:
        clean, failed = [], {}
        for conv_id in ids:
            conv_id = (conv_id or "").strip()
            if self._CONV_ID_RE.match(conv_id):
                clean.append(conv_id)
            elif conv_id:
                failed[conv_id] = "malformed id"
        if not clean:
            return [], failed
        js = self._DELETE_MANY_JS.replace("__IDS__", json.dumps(clean))
        data = await self._read_json(page, js)
        if data.get("error"):
            raise RuntimeError(data["error"])
        done = [i for i in (data.get("ok") or []) if isinstance(i, str)]
        failed.update(data.get("failed") or {})
        logger.info(f"[{self.name}] deleted {len(done)}/{len(clean)} conversation(s) "
                    f"from the account")
        for conv_id, why in (data.get("failed") or {}).items():
            logger.warning(f"[{self.name}] could not delete {conv_id[:8]}…: {why}")
        return done, failed

    async def discard_conversation(self, page) -> bool:
        conv_id = await self.conversation_id(page)
        if not conv_id:
            logger.warning(f"[{self.name}] no conversation in the URL to delete")
            return False
        ok = await self.delete_conversation(page, conv_id)
        if ok:
            logger.info(f"[{self.name}] conversation deleted (ephemeral request)")
        return ok

    async def logged_in(self, page) -> bool:
        # ChatGPT's logged-OUT page is deceptive: it renders a full sidebar and
        # an account-ish button, and lets you type in a composer. The reliable
        # signal is the *absence* of logged-out affordances — a "Log in" button,
        # "Continue with Google/Apple/…" buttons, or a "Sign up or log in" panel.
        # The sign-in flow (auth.openai.com / accounts.google) also reads as out.
        try:
            v = await page.evaluate("""
                (function(){
                    const url = location.href;
                    const onAuth = /auth\\.openai\\.com|accounts\\.google|appleid\\.apple|\\/auth\\/login|\\/authorize|\\/u\\/login|identifier|challenge/i.test(url);
                    if (onAuth) return false;
                    const norm = e => (e.innerText||e.textContent||'').trim().toLowerCase();
                    const ctrls = Array.from(document.querySelectorAll('a,button'));
                    const loginBtn = ctrls.some(e => /^(log ?in|sign ?in)$/.test(norm(e)));
                    const oauthBtn = ctrls.some(e => /continue with (google|apple|phone|microsoft)/i.test(norm(e)));
                    const bodyTxt = (document.body ? document.body.innerText : '').toLowerCase();
                    const authPrompt = /sign up or log in|log in to get|log in to save/i.test(bodyTxt);
                    if (loginBtn || oauthBtn || authPrompt) return false;   // logged OUT
                    // No logged-out affordance + a composer present => signed in.
                    return !!document.querySelector(__COMPOSER__);
                })()
            """.replace("__COMPOSER__", json.dumps(
                _COMPOSER_SEL + ', [data-testid="composer-send-button"]')))
            return bool(v)
        except Exception:
            return False
