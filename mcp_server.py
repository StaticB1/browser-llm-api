#!/usr/bin/env python3
"""
MCP (Model Context Protocol) server for the Browser LLM API — stdlib only.

Exposes the local browser-backed ChatGPT/Gemini sessions as native tools to any
MCP client (Claude Code, Claude Desktop, Cursor, Zed), so an agent can ask the
model, send it screenshots and generate real image assets without shelling out
to client.py and juggling temp files.

    claude mcp add browser-llm -- /path/to/venv/bin/python /path/to/mcp_server.py

Transport is stdio JSON-RPC 2.0, newline-delimited: stdout carries protocol
frames only, everything human-readable goes to stderr.

Tools:
  ask             prompt (+ images, + system) -> text
  generate_image  prompt (+ reference images, sizing) -> a written file path
  list_models     which providers are configured and reachable
  health          is the API up, is a request in flight, is the session alive

Requests are served on worker threads, so a 4-minute image generation never
blocks a ping or a second tool call. The upstream API is single-flight — calls
queue there, which is the intended behaviour, not a bug.

Env: BROWSER_LLM_API (default http://localhost:8081), BROWSER_LLM_MODEL,
     BROWSER_LLM_API_KEY, BROWSER_LLM_MCP_TIMEOUT (default 440).
"""
import json
import os
import sys
import threading
import traceback
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import ask as _ask  # noqa: E402  (stdlib-only sibling module)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "browser-llm"
BASE = os.environ.get("BROWSER_LLM_API", "http://localhost:8081").rstrip("/")
API_KEY = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
DEFAULT_TIMEOUT = int(os.environ.get("BROWSER_LLM_MCP_TIMEOUT", "440"))

try:
    from _version import __version__ as VERSION
except Exception:
    VERSION = "0"

_write_lock = threading.Lock()
_workers = []   # in-flight tools/call threads, joined on shutdown


def log(msg):
    print(f"[mcp] {msg}", file=sys.stderr, flush=True)


def _get(path, timeout=15):
    req = urllib.request.Request(BASE + path)
    if API_KEY:
        req.add_header("Authorization", f"Bearer {API_KEY}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "ask",
        "description": (
            "Ask the browser-backed ChatGPT or Gemini session and get its reply as text. "
            "Attach images to have it look at screenshots, mockups or photos. Every call "
            "should demand a finished artifact (a complete HTML page, a hard critique with "
            "the replacement markup) rather than an opinion. Text replies take 10-40s."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The instruction to send."},
                "images": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute file paths, http(s) URLs or data: URLs to "
                                   "attach. Local paths are read and uploaded.",
                },
                "model": {
                    "type": "string",
                    "description": "Provider id, e.g. chatgpt-browser or gemini-browser. "
                                   "Defaults to the server's configured provider.",
                },
                "system": {"type": "string", "description": "Optional system prompt."},
                "strip_fences": {
                    "type": "boolean",
                    "description": "Drop ``` code-fence lines from the reply. Use when "
                                   "the answer is meant to be written straight to a file.",
                },
                "out": {
                    "type": "string",
                    "description": "Optional absolute path to write the reply to. The "
                                   "path is returned instead of the full text, which "
                                   "keeps a large HTML page out of the conversation.",
                },
                "timeout": {"type": "integer", "description": "Seconds to wait (default 440)."},
                "keep_chat": {
                    "type": "boolean",
                    "description": "Leave the conversation in the account's history. "
                                   "By default it is deleted once the reply is back, "
                                   "since the history belongs to the person, not the "
                                   "agent. Set this only when they asked to see it.",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "generate_image",
        "description": (
            "Generate a real raster asset (hero, icon, texture, illustration, og-image) and "
            "write it to disk, resized/cropped/converted as asked. Pass reference images to "
            "restyle an existing asset or match a set. Takes 30s-4min. Returns the output "
            "path — open it and look before shipping."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to draw."},
                "out": {
                    "type": "string",
                    "description": "Absolute output path. Format is inferred from the "
                                   "extension (.webp/.png/.jpg/.ico).",
                },
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Reference images for image-to-image (paths or URLs). "
                                   "Holds a layout far better than describing one.",
                },
                "model": {"type": "string", "description": "chatgpt-browser | gemini-browser."},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "square": {"type": "integer", "description": "Center-crop to SIZExSIZE."},
                "fit": {"type": "string", "enum": ["cover", "contain"]},
                "format": {"type": "string", "description": "Override the inferred format."},
                "favicon": {"type": "boolean", "description": "Write a multi-size .ico."},
                "knockout_bg": {
                    "type": "boolean",
                    "description": "Make a flat background transparent (png/webp/ico). "
                                   "Prompt a 'solid <colour> background' for a clean cut.",
                },
                "quality": {"type": "integer"},
                "timeout": {"type": "integer"},
            },
            "required": ["prompt", "out"],
        },
    },
    {
        "name": "list_models",
        "description": "List the provider ids this server can drive.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "health",
        "description": (
            "Check the API: reachable, which providers are logged in, whether a request "
            "is in flight. Call this first when replies come back empty — an expired "
            "browser session is the usual cause and needs a human to re-login."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def tool_ask(args):
    prompt = args.get("prompt") or ""
    if not prompt.strip():
        raise ValueError("prompt is required")
    text = _ask(
        prompt,
        model=args.get("model"),
        system=args.get("system"),
        timeout=int(args.get("timeout") or DEFAULT_TIMEOUT),
        images=args.get("images") or None,
        # An agent's question is tooling, not conversation: it has no business
        # sitting in the account's chat history afterwards.
        ephemeral=not args.get("keep_chat"),
    )
    if args.get("strip_fences"):
        text = "\n".join(ln for ln in text.splitlines()
                         if not ln.lstrip().startswith("```"))
    out = args.get("out")
    if out:
        out = os.path.abspath(os.path.expanduser(out))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            f.write(text.rstrip("\n") + "\n")
        return f"wrote {out} ({len(text)} chars)"
    return text


def tool_generate_image(args):
    import gen_asset  # imported lazily: needs Pillow, which `ask` does not

    out = os.path.abspath(os.path.expanduser(args["out"]))
    path = gen_asset.render(
        prompt=args["prompt"],
        out=out,
        model=args.get("model"),
        refs=args.get("refs") or None,
        timeout=int(args.get("timeout") or DEFAULT_TIMEOUT),
        width=args.get("width"),
        height=args.get("height"),
        square_size=args.get("square"),
        fit=args.get("fit") or "cover",
        fmt=args.get("format"),
        favicon=bool(args.get("favicon")),
        knockout=bool(args.get("knockout_bg")),
        quality=int(args.get("quality") or 88),
    )
    return f"wrote {path}"


def tool_list_models(_args):
    d = _get("/v1/models")
    return "\n".join(m["id"] for m in d.get("data", [])) or "(none configured)"


def tool_health(_args):
    try:
        status = _get("/api/status")
    except Exception as e:
        return (f"UNREACHABLE: {BASE} ({e}). Start it with "
                f"`systemctl --user start browser-llm-api`.")
    return json.dumps(status, indent=2)


HANDLERS = {
    "ask": tool_ask,
    "generate_image": tool_generate_image,
    "list_models": tool_list_models,
    "health": tool_health,
}


# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------

def send(msg):
    with _write_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def reply(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def error(rid, code, message):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle_tools_call(rid, params):
    name = params.get("name")
    args = params.get("arguments") or {}
    fn = HANDLERS.get(name)
    if fn is None:
        error(rid, -32602, f"unknown tool: {name}")
        return
    try:
        out = fn(args)
        reply(rid, {"content": [{"type": "text", "text": str(out)}], "isError": False})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:500]
        reply(rid, {"content": [{"type": "text", "text": f"HTTP {e.code}: {body}"}],
                    "isError": True})
    except Exception as e:
        log(f"{name} failed: {traceback.format_exc()}")
        reply(rid, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                    "isError": True})


def handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        reply(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": str(VERSION)},
        })
    elif method in ("notifications/initialized", "notifications/cancelled"):
        pass  # notifications carry no id and take no response
    elif method == "ping":
        reply(rid, {})
    elif method == "tools/list":
        reply(rid, {"tools": TOOLS})
    elif method == "tools/call":
        # Off the read loop: image generation can run for minutes and must not
        # stall pings or a concurrent call.
        t = threading.Thread(target=handle_tools_call, args=(rid, params), daemon=True)
        _workers.append(t)
        t.start()
    elif rid is not None:
        error(rid, -32601, f"method not found: {method}")


def main():
    log(f"serving {SERVER_NAME} v{VERSION} against {BASE}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log(f"dropping non-JSON line: {line[:120]}")
            continue
        try:
            handle(msg)
        except Exception:
            log(f"handler crashed: {traceback.format_exc()}")
            if msg.get("id") is not None:
                error(msg["id"], -32603, "internal error")

    # stdin closed. A tool call already running still owes its caller a reply,
    # and an image generation can be minutes from done — drain before exiting.
    live = [t for t in _workers if t.is_alive()]
    if live:
        log(f"stdin closed; waiting for {len(live)} in-flight call(s)")
        for t in live:
            t.join()


if __name__ == "__main__":
    main()
