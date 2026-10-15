#!/usr/bin/env python3
"""
Tiny client for the local Browser LLM API (OpenAI-compatible) — no deps, stdlib only.

CLI:
  browser-llm ask "Explain CORS in two sentences"          # ChatGPT, GPT-6 at High by default
  browser-llm ask --variant instant "One-line summary of TCP"   # a faster ChatGPT setting
  browser-llm ask --system "You are terse." "hi"
  browser-llm ask --stream "tell me a short story"
  # send images along with the prompt (vision) — repeat --image for several:
  browser-llm ask --image shot.png "What's wrong with this UI?"
  # write code/HTML straight to a file, dropping any ``` fences the model adds:
  browser-llm ask --out index.html --strip-fences \
      "Output ONLY a complete standalone HTML5 landing page for a bakery, inline CSS, no explanation"

Import from any project:
  from browser_llm.client import ask
  html = ask("Output ONLY an HTML page ...", model="chatgpt-browser")
  desc = ask("Describe this", images=["/path/to/shot.png"])

Env: BROWSER_LLM_API (default http://localhost:8081), BROWSER_LLM_MODEL (default chatgpt-browser),
     BROWSER_LLM_API_KEY (sent as Bearer; required by a remote server that has a key configured)
     BROWSER_LLM_FALLBACK_API (+ _KEY, _MODEL, default gemini-browser): a second server and
     model to ask when the first fails or answers nothing. Chat only; images never fall back.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BROWSER_LLM_API", "http://localhost:8081").rstrip("/")
DEFAULT_MODEL = os.environ.get("BROWSER_LLM_MODEL", "chatgpt-browser")
API_KEY = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
# A second server and model to try when the first one fails. Unset = no fallback.
FALLBACK_BASE = os.environ.get("BROWSER_LLM_FALLBACK_API", "").strip().rstrip("/")
FALLBACK_API_KEY = os.environ.get("BROWSER_LLM_FALLBACK_API_KEY", "").strip()
FALLBACK_MODEL = os.environ.get("BROWSER_LLM_FALLBACK_MODEL", "gemini-browser").strip()


def _image_spec(path_or_url):
    """A local path is sent as a data: URL so it works against a remote server
    too (which can't read this machine's disk); URLs pass through."""
    s = str(path_or_url)
    if s[:5].lower() in ("http:", "https", "data:") or s[:7].lower() == "file://":
        return s
    if not os.path.isfile(s):
        raise SystemExit(f"[client] image not found: {s}")
    import base64
    import mimetypes
    mime = mimetypes.guess_type(s)[0] or "image/png"
    with open(s, "rb") as f:
        return f"data:{mime};base64," + base64.b64encode(f.read()).decode()


def ask(prompt, model=None, system=None, timeout=440, stream=False, on_delta=None,
        images=None, ephemeral=False, model_variant=None, fallback=True):
    """Send a chat completion and return the assistant text.
    If stream=True, on_delta(str) is called for each chunk as it arrives.
    ``images`` (paths, data: URLs or http URLs) are uploaded into the provider's
    chat alongside the prompt.
    ``ephemeral`` deletes the conversation from the account once the answer is
    back, for prompts that are tooling rather than conversation.
    ``model_variant`` picks ChatGPT's model and power for this request
    ("instant", "GPT-6/medium"); unset uses the server's setting (GPT-6, High).
    ``fallback`` (default True) lets a failed or empty answer be retried once on
    ``BROWSER_LLM_FALLBACK_API`` with ``BROWSER_LLM_FALLBACK_MODEL``, when that
    is configured. A request the server rejected as malformed (HTTP 400) is not
    retried, and neither is a stream that has already delivered text."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    if images:
        content = [{"type": "text", "text": prompt}] if prompt else []
        content += [{"type": "image_url", "image_url": {"url": _image_spec(i)}}
                    for i in images]
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": prompt})
    payload = {"model": model or DEFAULT_MODEL, "messages": messages, "stream": stream}
    if ephemeral:
        payload["ephemeral"] = True
    if model_variant:
        payload["model_variant"] = model_variant

    emitted = [False]
    def _on_delta(d):
        emitted[0] = True
        if on_delta:
            on_delta(d)

    try:
        text = _ask_once(BASE, API_KEY, payload, timeout, stream, _on_delta)
        reason = "" if text.strip() else "empty answer"
    except urllib.error.HTTPError as e:
        if e.code == 400:
            raise
        text, reason = "", f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        text, reason = "", str(e) or type(e).__name__
    if not reason:
        return text
    if not (fallback and FALLBACK_BASE) or emitted[0]:
        if text:
            return text
        raise RuntimeError(f"{payload['model']} at {BASE} failed: {reason}")

    fb = dict(payload, model=FALLBACK_MODEL)
    fb.pop("model_variant", None)   # a ChatGPT setting; the fallback is usually not ChatGPT
    print(f"[client] {payload['model']} at {BASE} failed ({reason}); "
          f"falling back to {FALLBACK_MODEL} at {FALLBACK_BASE}", file=sys.stderr, flush=True)
    return _ask_once(FALLBACK_BASE, FALLBACK_API_KEY, fb, timeout, stream, _on_delta)


def _ask_once(base, api_key, payload, timeout, stream, on_delta):
    """One chat completion against one server. Returns the assistant text."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if not stream:
            return json.load(r)["choices"][0]["message"]["content"]
        text = ""
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
                text += delta
                if on_delta:
                    on_delta(delta)
        return text


def main():
    ap = argparse.ArgumentParser(description="Chat with the local Browser LLM API.")
    ap.add_argument("prompt")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help="chatgpt-browser (default) | gemini-browser (text only)")
    ap.add_argument("--variant", dest="model_variant",
                    help="ChatGPT model/power, e.g. 'instant', 'medium' or 'GPT-6/high' "
                         "(default: the server's setting, GPT-6 at High)")
    ap.add_argument("--system", help="optional system prompt")
    ap.add_argument("--image", action="append", metavar="PATH|URL", dest="images",
                    help="attach an image to the prompt (repeatable)")
    ap.add_argument("--out", help="write the response to this file instead of stdout")
    ap.add_argument("--stream", action="store_true", help="stream tokens as they arrive")
    ap.add_argument("--strip-fences", action="store_true",
                    help="drop ``` code-fence lines (handy with --out for code/HTML)")
    ap.add_argument("--timeout", type=int, default=440)
    a = ap.parse_args()

    live = a.stream and not a.out
    emit = (lambda d: (sys.stdout.write(d), sys.stdout.flush())) if live else None
    text = ask(a.prompt, model=a.model, system=a.system,
               timeout=a.timeout, stream=a.stream, on_delta=emit, images=a.images,
               model_variant=a.model_variant)

    if a.strip_fences:
        text = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("```"))

    if a.out:
        with open(a.out, "w") as f:
            f.write(text.rstrip("\n") + "\n")
        print(f"[client] wrote {a.out}", file=sys.stderr)
    elif live:
        print()  # trailing newline after streamed output
    else:
        print(text)


if __name__ == "__main__":
    main()
