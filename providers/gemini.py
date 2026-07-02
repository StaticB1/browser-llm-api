"""
Gemini provider — drives gemini.google.com.

Text lives inside Web Components, so extraction pierces shadow roots. Generated
images render as <img alt*="generated"> whose blob: src must be read to base64
from inside the page (blob URLs aren't fetchable over HTTP externally). This is
the original server.py logic, moved behind the Provider interface unchanged.
"""
import json
import logging
import re

from .base import Provider

logger = logging.getLogger("gemini_server")


# Count AI-generated images in the last response: rendered (loaded), still-blank
# (pending), and whether the "Creating your image…" placeholder is showing.
_IMG_STATUS_JS = """
(function(){
  function deep(root,sel,out){if(!root)return;root.querySelectorAll(sel).forEach(e=>out.push(e));root.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)deep(e.shadowRoot,sel,out);});}
  const rs=[];deep(document,'model-response',rs);
  const last=rs[rs.length-1];
  if(!last) return JSON.stringify({loaded:0,pending:0,creating:false});
  const root=last.shadowRoot||last;
  const imgs=[];deep(root,'img',imgs);
  let loaded=0,pending=0;
  imgs.forEach(im=>{const ai=(im.alt||'').toLowerCase().includes('generated');if(!ai)return;if(im.naturalWidth>64)loaded++;else pending++;});
  const txt=(last.innerText||last.textContent||'');
  const creating=/creating your image|generating image|creating image/i.test(txt);
  return JSON.stringify({loaded:loaded,pending:pending,creating:creating});
})()
"""

# Read each rendered AI image (a blob: URL) into base64 from inside the page.
_GET_IMAGES_JS = """
(async function(){
  function deep(root,sel,out){if(!root)return;root.querySelectorAll(sel).forEach(e=>out.push(e));root.querySelectorAll('*').forEach(e=>{if(e.shadowRoot)deep(e.shadowRoot,sel,out);});}
  const rs=[];deep(document,'model-response',rs);
  const last=rs[rs.length-1]; if(!last) return '[]';
  const root=last.shadowRoot||last;
  const imgs=[];deep(root,'img',imgs);
  const seen=new Set(); const out=[];
  for(const im of imgs){
    const ai=(im.alt||'').toLowerCase().includes('generated');
    if(!ai||im.naturalWidth<=64) continue;
    const src=im.currentSrc||im.src; if(!src||seen.has(src)) continue; seen.add(src);
    try{
      const r=await fetch(src); const b=await r.blob(); const buf=await b.arrayBuffer();
      const by=new Uint8Array(buf); let s=''; const CH=0x8000;
      for(let i=0;i<by.length;i+=CH){ s+=String.fromCharCode.apply(null, by.subarray(i,i+CH)); }
      out.push({mime:b.type||'image/jpeg', b64:btoa(s), alt:(im.alt||'').replace(/^[,\\s]+/,'').trim()});
    }catch(e){ /* skip unreadable image */ }
  }
  return JSON.stringify(out);
})()
"""


class GeminiProvider(Provider):
    name = "gemini-browser"
    chat_url = "https://gemini.google.com/app"
    profile_dir = "./gemini_profile"
    stream_url_fragments = ["streamGenerateContent", "GenerateContent", "BardFrontendService"]
    supports_images = True
    image_text_is_caption = False  # Gemini's image-prompt prose is "thinking" chrome
    input_selector = 'div[contenteditable="true"]'
    send_selectors = ['button[aria-label="Send message"]']
    load_wait = 6.0

    async def get_response_text(self, page) -> str:
        """Shadow-DOM-piercing text extractor for the last model-response."""
        try:
            result = await page.evaluate("""
                (function() {
                    const SKIP = new Set([
                        'script','style','button','svg','path','img','picture',
                        'source','nav','header','footer','aside','dialog',
                        'mat-icon','iron-icon','tp-yt-paper-tooltip',
                        'thinking-overlay','model-thoughts'  // Gemini's "thinking" summary
                    ]);
                    function collectText(root) {
                        if (!root) return '';
                        let out = '';
                        for (const node of root.childNodes) {
                            if (node.nodeType === 3) {
                                out += node.textContent;
                            } else if (node.nodeType === 1) {
                                const tag = node.tagName.toLowerCase();
                                if (SKIP.has(tag)) continue;
                                if (node.getAttribute && node.getAttribute('aria-hidden') === 'true') continue;
                                const cls = (node.getAttribute && node.getAttribute('class')) || '';
                                if (/cdk-visually-hidden|screen-reader/.test(cls)) continue;
                                if (node.shadowRoot) {
                                    out += collectText(node.shadowRoot);
                                } else {
                                    out += collectText(node);
                                }
                            }
                        }
                        return out;
                    }
                    const responses = document.querySelectorAll('model-response');
                    if (!responses.length) return '';
                    const last = responses[responses.length - 1];
                    return collectText(last.shadowRoot || last)
                        .replace(/\\s+/g, ' ').trim();
                })()
            """)
            if not isinstance(result, str):
                return ""
            result = re.sub(r'^(Show thinking\s+)?(Gemini said\s*)', '', result)
            result = re.sub(r'\s*Sources\s*$', '', result).strip()
            return result
        except Exception:
            return ""

    async def is_generating(self, page) -> bool:
        try:
            btn = await page.select('button[aria-label="Stop generating"]', timeout=1)
            return btn is not None
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
                return [d for d in json.loads(raw) if d.get("b64")]
        except Exception as e:
            logger.warning(f"[{self.name}] image extraction failed: {e}")
        return []

    async def logged_in(self, page) -> bool:
        # Logged-in = no "Sign in" button, not the "Meet Gemini" landing page,
        # or an account email present in a profile aria-label.
        try:
            v = await page.evaluate("""
                (function(){
                    const txt = document.body ? document.body.innerText : '';
                    const signIn = Array.from(document.querySelectorAll('a,button'))
                        .some(e => (e.innerText||'').trim().toLowerCase() === 'sign in');
                    const landing = /Meet Gemini/i.test(txt);
                    const acct = Array.from(document.querySelectorAll('[aria-label]'))
                        .some(e => (e.getAttribute('aria-label')||'').includes('@'));
                    return (!signIn && !landing) || acct;
                })()
            """)
            return bool(v)
        except Exception:
            return False
