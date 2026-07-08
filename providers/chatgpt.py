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

from .base import Provider

logger = logging.getLogger("gemini_server")


# Generated-image render status in the last assistant turn. Only images served
# from oaiusercontent/blob are counted (avoids counting UI icons/avatars, which
# would wrongly suppress plain text responses).
_IMG_STATUS_JS = """
(function(){
  // A generated image can render in the assistant turn OR in a separate
  // image-generation tile, so scan the whole conversation. Each request opens
  // a fresh chat, so there are no stale images to confuse this.
  const _isGenImg=(src,alt)=>{src=src||'';alt=(alt||'').trim().toLowerCase();
    return src.indexOf('backend-api/estuary')>=0||src.indexOf('backend-api/files')>=0
      ||src.indexOf('oaiusercontent')>=0||src.indexOf('/content?')>=0
      ||src.indexOf('blob:')===0||alt.indexOf('generated image')===0;};
  const imgs=Array.from(document.querySelectorAll('img'));
  let loaded=0,pending=0;
  imgs.forEach(im=>{
    if(!_isGenImg(im.currentSrc||im.src||'', im.alt||'')) return;
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
  const _isGenImg=(src,alt)=>{src=src||'';alt=(alt||'').trim().toLowerCase();
    return src.indexOf('backend-api/estuary')>=0||src.indexOf('backend-api/files')>=0
      ||src.indexOf('oaiusercontent')>=0||src.indexOf('/content?')>=0
      ||src.indexOf('blob:')===0||alt.indexOf('generated image')===0;};
  const imgs=Array.from(document.querySelectorAll('img'));
  const seen=new Set(); const out=[];
  for(const im of imgs){
    const src=im.currentSrc||im.src; if(!src) continue;
    if(!_isGenImg(src, im.alt||'')) continue;
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
                            var card=findCard(editor);
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
                    if(msg.length>=40 || msg.indexOf('```')>=0) return msg;
                    // Canvas side-panel fallback: big code/doc answers land in a side
                    // panel, not in .markdown, leaving the message body near-empty.
                    // Virtualized editors keep only visible lines, so this can be partial.
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
                    return canvasTxt.length>msg.length?canvasTxt:msg;
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
