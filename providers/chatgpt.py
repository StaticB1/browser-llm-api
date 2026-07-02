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
  // (a dotted-grid "One last tweak…" tile) before committing the final <img>;
  // the canvas disappears once done, so treating it as "creating" is safe.
  const main=(document.querySelector('main')||document.body);
  const txt=(main && main.innerText || '').toLowerCase();
  const canvas=!!(main && main.querySelector('canvas'));
  const creating=canvas || /creating image|generating image|making the image|creating the image|creating your image|one last tweak/i.test(txt);
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
    supports_images = True
    # The chatgpt.com app session cookie — its presence is the definitive
    # signed-in signal (set only after the OAuth callback fully completes).
    session_cookie = ("__Secure-next-auth.session-token", "chatgpt.com")
    image_text_is_caption = True  # ChatGPT's prose alongside an image is a real caption
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
        try:
            result = await page.evaluate("""
                (function(){
                    const msgs = document.querySelectorAll('[data-message-author-role="assistant"]');
                    if(!msgs.length) return '';
                    const last = msgs[msgs.length-1];
                    const md = last.querySelector('.markdown') || last.querySelector('.prose') || last;
                    return (md.innerText || md.textContent || '').trim();
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
