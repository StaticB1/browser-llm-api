#!/usr/bin/env python3
"""
Tiny client for the local Browser LLM API (OpenAI-compatible) — no deps, stdlib only.

CLI:
  ./client.py "Explain CORS in two sentences"
  ./client.py --model gemini-browser "One-line summary of TCP"
  ./client.py --system "You are terse." "hi"
  ./client.py --stream "tell me a short story"
  # write code/HTML straight to a file, dropping any ``` fences the model adds:
  ./client.py --out index.html --strip-fences \
      "Output ONLY a complete standalone HTML5 landing page for a bakery, inline CSS, no explanation"

Import from any project:
  from client import ask
  html = ask("Output ONLY an HTML page ...", model="chatgpt-browser")

Env: BROWSER_LLM_API (default http://localhost:8081), BROWSER_LLM_MODEL (default chatgpt-browser),
     BROWSER_LLM_API_KEY (sent as Bearer; required by a remote server that has a key configured)
"""
import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("BROWSER_LLM_API", "http://localhost:8081").rstrip("/")
DEFAULT_MODEL = os.environ.get("BROWSER_LLM_MODEL", "chatgpt-browser")
API_KEY = os.environ.get("BROWSER_LLM_API_KEY", "").strip()


def ask(prompt, model=None, system=None, timeout=440, stream=False, on_delta=None):
    """Send a chat completion and return the assistant text.
    If stream=True, on_delta(str) is called for each chunk as it arrives."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": model or DEFAULT_MODEL, "messages": messages, "stream": stream}
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
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
    ap.add_argument("--model", default=DEFAULT_MODEL, help="chatgpt-browser | gemini-browser")
    ap.add_argument("--system", help="optional system prompt")
    ap.add_argument("--out", help="write the response to this file instead of stdout")
    ap.add_argument("--stream", action="store_true", help="stream tokens as they arrive")
    ap.add_argument("--strip-fences", action="store_true",
                    help="drop ``` code-fence lines (handy with --out for code/HTML)")
    ap.add_argument("--timeout", type=int, default=440)
    a = ap.parse_args()

    live = a.stream and not a.out
    emit = (lambda d: (sys.stdout.write(d), sys.stdout.flush())) if live else None
    text = ask(a.prompt, model=a.model, system=a.system,
               timeout=a.timeout, stream=a.stream, on_delta=emit)

    if a.strip_fences:
        text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("```"))

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
