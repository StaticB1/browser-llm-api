"""
ChatGPT provider — drives chatgpt.com.

Unlike Gemini, ChatGPT's UI is plain DOM (no shadow piercing needed): the last
assistant turn is ``[data-message-author-role="assistant"]`` and its text lives
in a ``.markdown`` container. Generated images (GPT-image / DALL·E) render as
``<img>`` served from ``oaiusercontent.com`` (or a ``blob:`` URL mid-stream);
we read them to base64 inside the page, falling back to the remote URL if CORS
blocks the fetch.

NOTE: These selectors are best-guess against the live ChatGPT web UI, which
changes often and sits behind Cloudflare/anti-bot checks. Expect to tweak the
selectors and timing after verifying against a real signed-in session.
"""
import json
import logging
import re

from .base import Provider, RateLimited

logger = logging.getLogger("gemini_server")


# Shared image predicates. `_isGenImg` matches ChatGPT's generated-image URLs;
# `_isInput` excludes images WE uploaded as input (a vision request) — those
# render with the very same blob:/content? URLs, but inside the user's turn or
# the composer's file tile. Without this exclusion an uploaded image counts as
# "generated": the completion tracker fires image-stability immediately (cutting
# the text answer off) and the upload gets echoed back and saved to the gallery.
_IMG_PREDICATES_JS = """
  const _isGenImg=(src,alt)=>{src=src||'';alt=(alt||'').trim().toLowerCase();
    return src.indexOf('backend-api/estuary')>=0||src.indexOf('backend-api/files')>=0
      ||src.indexOf('oaiusercontent')>=0||src.indexOf('/content?')>=0
      ||src.indexOf('blob:')===0||alt.indexOf('generated image')===0;};
  const _isInput=(im)=>{ try{
    return !!(im.closest('[data-message-author-role="user"]')
      || im.closest('form') || im.closest('[class*="file-tile"]'));
  }catch(e){ return false; } };
"""

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
    # ProseMirror contenteditable composer.
    input_selector = '#prompt-textarea'
    send_selectors = [
        'button[data-testid="send-button"]',
        'button[data-testid="composer-send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Send message"]',
    ]
    load_wait = 7.0  # ChatGPT can be slow to hydrate / may show a bot check
    # --- image input (upload). Verified live 2026-07-28: chatgpt.com keeps a
    # hidden `input[data-testid="upload-photos-input"]` (accept=image/*) in the
    # DOM at rest, so the generic input path attaches without touching the "+"
    # menu; the click path below is only a fallback if that input disappears.
    supports_history = True
    site_origin = "https://chatgpt.com"
    supports_upload = True
    attach_click_path = [
        ['button[data-testid="composer-plus-btn"]', 'text:add photos & files',
         'text:add files and more'],
        ['text:add photos & files', 'text:upload from computer', 'text:add photos'],
    ]
    # Each accepted file renders a 144px "file tile" with a
    # `button[aria-label="Remove file N: <name>"]`; that button is the reliable
    # per-file chip count. Busy = the tile still shows a progress indicator.
    attachment_ready_js = """
    (function(){
      var chips=document.querySelectorAll('button[aria-label^="Remove file"]').length;
      if(!chips) chips=document.querySelectorAll('[class*="file-tile"] img').length;
      var form=document.querySelector('form')||document.body;
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
                (function(){
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
                        }catch(e){}
                        return '';
                    }
                    var msgs=document.querySelectorAll('[data-message-author-role="assistant"]');
                    var msg='';
                    if(msgs.length){
                        var last=msgs[msgs.length-1];
                        var md=last.querySelector('.markdown')||last.querySelector('.prose')||last;
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
                    var composer=document.querySelector('#prompt-textarea');
                    var isComposer=function(el){ return composer&&(el===composer||el.contains(composer)
                        ||composer.contains(el)||el.id==='prompt-textarea'); };
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
                (function(){
                    if(document.querySelector('[data-testid="stop-button"]')) return true;
                    if(document.querySelector('button[aria-label="Stop streaming"]')) return true;
                    if(document.querySelector('button[aria-label="Stop generating"]')) return true;
                    if(document.querySelector('.result-streaming')) return true;
                    // Still working: the assistant bubble is showing shimmering
                    // status text ("Analyzing image" on a vision request). The
                    // stop button can be absent during that phase, which used to
                    // look like "settled" and ended the poll before the answer.
                    var msgs=document.querySelectorAll('[data-message-author-role="assistant"]');
                    if(msgs.length){
                        var last=msgs[msgs.length-1];
                        var md=last.querySelector('.markdown')||last.querySelector('.prose')||last;
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
                    return !!document.querySelector('#prompt-textarea, [data-testid="composer-send-button"]');
                })()
            """)
            return bool(v)
        except Exception:
            return False
