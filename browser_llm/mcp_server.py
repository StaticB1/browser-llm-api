#!/usr/bin/env python3
"""
MCP (Model Context Protocol) server for the Browser LLM API — stdlib only.

Exposes the local browser-backed ChatGPT/Gemini sessions as native tools to any
MCP client (Claude Code, Claude Desktop, Cursor, Zed), so an agent can ask the
model, send it screenshots and generate real image assets without shelling out
to the CLI and juggling temp files.

    claude mcp add browser-llm -- /path/to/checkout/mcp.sh
    (or: /path/to/venv/bin/browser-llm mcp)

Transport is stdio JSON-RPC 2.0, newline-delimited: stdout carries protocol
frames only, everything human-readable goes to stderr.

Tools:
  ask             prompt (+ images, + system) -> text
  generate_image  prompt (+ reference images, sizing, design kind) -> a written file path
  design_set      brief + sections/screens -> one image each, one design system
                  (browser-llm design), with a manifest and a stacked preview
  design_tokens   design images -> tokens.json, tokens.css, DESIGN.md
  design_review   design image + build screenshot -> differences and fixes,
                  or one screenshot -> a critique
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

from . import design as _design_mod  # stdlib-only siblings
from .client import ask as _ask
from ._version import __version__ as VERSION

_KINDS = _design_mod.kind_names()

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "browser-llm"
BASE = os.environ.get("BROWSER_LLM_API", "http://localhost:8081").rstrip("/")
API_KEY = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
DEFAULT_TIMEOUT = int(os.environ.get("BROWSER_LLM_MCP_TIMEOUT", "440"))


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
            "Ask the browser-backed ChatGPT session (Plus; GPT-6 at High power by default) "
            "and get its reply as text. Attach images to have it look at screenshots, "
            "mockups or photos. Every call should demand a finished artifact (a complete "
            "HTML page, a hard critique with the replacement markup) rather than an "
            "opinion. Text replies take 15-60s at High; model_variant 'instant' is faster."
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
                    "description": "Provider id. Defaults to chatgpt-browser; "
                                   "gemini-browser is text only.",
                },
                "model_variant": {
                    "type": "string",
                    "description": "ChatGPT model and power for this call: 'instant', "
                                   "'medium', 'high', 'GPT-6/high', 'GPT-5.6 Sol'. "
                                   "Default: the server's setting (GPT-6, High).",
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
            "Generate a real raster asset (hero, icon, texture, illustration, og-image) with "
            "ChatGPT's image tool and write it to disk, cropped/converted to the exact size "
            "asked. Pass reference images to restyle an existing asset or match a set; "
            "variants=2 or 3 makes finished alternatives side by side plus a contact sheet. "
            "About 1 min per image, one at a time. Returns the output path(s) and the "
            "untouched original's path; open them and look before shipping. If ChatGPT's "
            "image limit is reached the call fails with the reset time: stop and say so."
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
                "model": {"type": "string",
                          "description": "Default chatgpt-browser, the only image provider."},
                "model_variant": {"type": "string",
                                  "description": "ChatGPT model/power, e.g. 'instant'. "
                                                 "Default: the server's setting."},
                "width": {"type": "integer",
                          "description": "Exact output width; the size is also asked of "
                                         "the image tool so the crop loses little."},
                "height": {"type": "integer"},
                "square": {"type": "integer", "description": "Center-crop to SIZExSIZE."},
                "variants": {"type": "integer",
                             "description": "Make N finished alternatives (2-3 for a main "
                                            "image): <name>-v1..vN plus <name>-variants.png."},
                "keep_original": {"type": "boolean",
                                  "description": "Also copy ChatGPT's untouched image beside "
                                                 "the output (<name>.original.png)."},
                "fit": {"type": "string", "enum": ["cover", "contain"]},
                "format": {"type": "string", "description": "Override the inferred format."},
                "favicon": {"type": "boolean", "description": "Write a multi-size .ico."},
                "knockout_bg": {
                    "type": "boolean",
                    "description": "Transparent background (png/webp/ico): asked of "
                                   "ChatGPT's tool, and a flat one is knocked out locally "
                                   "if none came back.",
                },
                "quality": {"type": "integer"},
                "timeout": {"type": "integer"},
                "kind": {"type": "string", "enum": _KINDS,
                         "description": "Design kind: the prompt becomes the brief inside "
                                        "the kind's rules (one website section, an app "
                                        "screen in a phone frame, a text-free hero image, "
                                        "an icon on transparency ...) and the kind's size "
                                        "and background apply."},
                "direction": {"type": "boolean",
                              "description": "With kind: add the art-direction block for "
                                             "the kind's group (otherwise ChatGPT picks "
                                             "the style)."},
                "theme": {"type": "string", "enum": ["light", "dark"]},
                "platform": {"type": "string", "enum": ["ios", "android", "neutral"],
                             "description": "kind mobile-screen only."},
                "system": {"type": "string",
                           "description": "Design system text to follow exactly: colours "
                                          "with hex, typefaces, radius."},
                "copy_space": {"type": "string",
                               "enum": ["left", "right", "center", "top", "bottom"],
                               "description": "kind hero-image only: where the HTML "
                                              "headline will sit."},
                "upscale": {"type": "number",
                            "description": "Enlarge the result by this factor (Lanczos + "
                                           "light sharpen)."},
            },
            "required": ["prompt", "out"],
        },
    },
    {
        "name": "design_set",
        "description": (
            "Design a whole page or app flow with ChatGPT's image tool, Codex-style: ONE "
            "large readable image per section or screen, made one after another, each "
            "carrying the first image and the one before it as references so the set "
            "keeps one design system. Writes NN-name.png files, manifest.json and "
            "preview.png into out_dir. About 1 to 1.5 min per image; the call refuses if "
            "the set does not fit in ChatGPT's remaining daily images (partial=true "
            "overrides). Calling it again resumes a stopped set. directions=3 first makes "
            "three candidates for section 1 and stops; call again with pick=K. Use "
            "dry_run=true to see every prompt without spending an image. For sets over "
            "6 images prefer the CLI (browser-llm design set) in the background: an MCP client "
            "may time out the call, although the set keeps going and resumes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "brief": {"type": "string",
                          "description": "The product, its audience, what the page or app "
                                         "must make them do, and facts to show."},
                "kind": {"type": "string", "enum": _KINDS,
                         "description": "Default section. mobile-screen for app flows."},
                "sections": {
                    "type": "array",
                    "items": {"type": ["string", "object"]},
                    "description": "In order. 'Name: content' strings or {name, content}. "
                                   "Content holds the real words: headline, buttons, "
                                   "items, numbers.",
                },
                "out_dir": {"type": "string", "description": "Absolute output folder."},
                "system": {"type": "string",
                           "description": "Design system text to follow exactly (or the "
                                          "text of a DESIGN.md)."},
                "theme": {"type": "string", "enum": ["light", "dark"]},
                "platform": {"type": "string", "enum": ["ios", "android", "neutral"]},
                "direction": {"type": "boolean",
                              "description": "Add art direction (otherwise ChatGPT picks "
                                             "the style)."},
                "copy_space": {"type": "string",
                               "enum": ["left", "right", "center", "top", "bottom"]},
                "refs": {"type": "array", "items": {"type": "string"},
                         "description": "Your own references for every image: logo, brand "
                                        "assets, a screenshot of the current site."},
                "lock": {"type": "string",
                         "description": "An approved image that sets the design system "
                                        "for every item."},
                "directions": {"type": "integer",
                               "description": "Make N candidates for item 1 first, then "
                                              "stop for pick."},
                "pick": {"type": "integer", "description": "Continue with direction K."},
                "redo": {"type": "array", "items": {"type": "string"},
                         "description": "Items (index or name) to make again."},
                "partial": {"type": "boolean"},
                "dry_run": {"type": "boolean"},
                "model_variant": {"type": "string"},
            },
            "required": ["brief", "out_dir"],
        },
    },
    {
        "name": "design_tokens",
        "description": (
            "Read the design system out of design images (up to 6): colours (checked "
            "against colours measured from the pixels), typefaces, type scale, spacing, "
            "radii, shadows, components and a per-section layout description. Writes "
            "tokens.json, tokens.css (custom properties) and DESIGN.md into out_dir for "
            "the coding step. One ChatGPT text call, a few minutes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "images": {"type": "array", "items": {"type": "string"},
                           "description": "Absolute paths, in page order."},
                "out_dir": {"type": "string"},
                "model_variant": {"type": "string"},
            },
            "required": ["images", "out_dir"],
        },
    },
    {
        "name": "design_review",
        "description": (
            "Compare a screenshot of the build with its design image and list every "
            "difference with the fix (match score, severity, CSS-level fix), or give a "
            "critique of one screenshot. Writes <out>.md and <out>.json. Take the build "
            "screenshot at the design's width (1440 for web sections)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "design": {"type": "string", "description": "The design image."},
                "build": {"type": "string", "description": "The build screenshot."},
                "critique": {"type": "string",
                             "description": "Instead of design+build: one image to critique."},
                "out": {"type": "string", "description": "Review file path (.md)."},
                "width": {"type": "integer", "description": "The screenshot's viewport width."},
                "notes": {"type": "string", "description": "Context for the reviewer."},
                "model_variant": {"type": "string"},
            },
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
        model_variant=args.get("model_variant") or None,
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
    from . import gen_asset  # imported lazily: needs Pillow, which `ask` does not

    out = os.path.abspath(os.path.expanduser(args["out"]))
    kw = dict(
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
        keep_original=bool(args.get("keep_original")),
        variant=args.get("model_variant") or None,
        kind=args.get("kind") or None,
        direction=bool(args.get("direction")),
        theme=args.get("theme") or None,
        platform=args.get("platform") or None,
        system=args.get("system") or None,
        copy_space=args.get("copy_space") or None,
        upscale=float(args["upscale"]) if args.get("upscale") else None,
    )

    def line(r):
        o = r.get("original_size") or ["?", "?"]
        s = r.get("size") or ["?", "?"]
        used = (r.get("model_used") or {}).get("model")
        return (f"wrote {r['out']} ({s[0]}x{s[1]}); original {o[0]}x{o[1]} at "
                f"{r.get('original') or '(not saved)'}" + (f"; drawn via {used}" if used else ""))

    n = int(args.get("variants") or 1)
    if n > 1:
        res = gen_asset.render_variants(args["prompt"], out, n, **kw)
        lines = [line(r) for r in res["variants"]]
        if res.get("sheet"):
            lines.append(f"contact sheet {res['sheet']}")
        return "\n".join(lines)
    return line(gen_asset.render_full(args["prompt"], out, **kw))


def tool_design_set(args):
    from . import design

    if not str(args.get("brief") or "").strip():
        raise ValueError("brief is required")
    spec = {k: args[k] for k in ("brief", "kind", "sections", "system", "theme", "platform",
                                 "direction", "copy_space", "refs", "lock") if args.get(k)}
    out_dir = os.path.abspath(os.path.expanduser(args["out_dir"]))
    res = design.run_set(spec, out_dir, dry_run=bool(args.get("dry_run")),
                         partial=bool(args.get("partial")),
                         directions=int(args.get("directions") or 0),
                         pick=args.get("pick") or None, redo=args.get("redo") or None,
                         variant=args.get("model_variant") or None,
                         timeout=DEFAULT_TIMEOUT, log=log)
    if res["status"] == "dry-run":
        parts = [f"[{st['state']}] {st['file']} (refs: "
                 f"{', '.join(os.path.basename(r) for r in st['refs']) or 'none'})\n"
                 f"{st['prompt']}" for st in res["steps"]]
        return "\n\n".join(parts) + f"\n\n{res['planned']} image(s) to make."
    if res["status"] == "pick":
        return ("Made the candidate directions; open the sheet and call design_set again "
                "with the same arguments plus pick=K.\n"
                + "\n".join(res["directions"])
                + (f"\nsheet {res['sheet']}" if res.get("sheet") else ""))
    lines = [f"made {res['made']} image(s) this call; manifest {res['manifest']}"]
    lines += res["files"]
    if res.get("preview"):
        lines.append(f"preview {res['preview']}")
    lines.append("Open the images and the preview before building from them; "
                 "design_tokens reads the design system out of them.")
    return "\n".join(lines)


def tool_design_tokens(args):
    from . import design

    images = [os.path.abspath(os.path.expanduser(p)) for p in args.get("images") or []]
    out_dir = os.path.abspath(os.path.expanduser(args["out_dir"]))
    res = design.extract_tokens(images, out_dir, variant=args.get("model_variant") or None,
                                log=log)
    t = res["tokens"]
    return (f"wrote {res['files']['md']}, {res['files']['css']} and {res['files']['json']}: "
            f"{len(t.get('colors') or [])} colours, {len(t.get('type_scale') or [])} type "
            f"styles, {len(t.get('components') or [])} components. Read DESIGN.md before "
            f"building; sizes are estimates.")


def tool_design_review(args):
    from . import design

    def path(k):
        v = args.get(k)
        return os.path.abspath(os.path.expanduser(v)) if v else None

    res = design.review(design=path("design"), build=path("build"),
                        critique=path("critique"), out=path("out"),
                        notes=args.get("notes"), width=args.get("width"),
                        variant=args.get("model_variant") or None, log=log)
    with open(res["files"]["md"]) as f:
        return f.read() + f"\n(also {res['files']['json']})"


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
    "design_set": tool_design_set,
    "design_tokens": tool_design_tokens,
    "design_review": tool_design_review,
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
