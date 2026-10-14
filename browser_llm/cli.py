"""The ``browser-llm`` command: one entry point for every tool in the package.

    browser-llm serve                    run the API server (what the service runs)
    browser-llm login chatgpt|gemini     sign in once per provider, in a visible window
    browser-llm ask "prompt" [options]   one chat completion; --image FILE attaches images
    browser-llm image --prompt ... --out FILE [options]    one image asset
    browser-llm design kinds|prompt|set|tokens|review ...  designs as images
    browser-llm mcp                      the MCP stdio server for agent clients
    browser-llm health                   the running server's state, in short
    browser-llm version

``serve`` and ``login`` use the browser profiles in the current directory, so
run them from the checkout (the service does). Each subcommand takes the same
options as the script it replaces; ``browser-llm <command> --help`` lists them.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from ._version import __version__

COMMANDS = {
    "serve": "run the API server",
    "login": "sign in to a provider in a visible browser window",
    "ask": "send one chat completion",
    "image": "make one image asset (resize, crop, favicon, transparency)",
    "design": "design sections, screens and brand images; tokens; review",
    "mcp": "run the MCP stdio server",
    "health": "show the running server's state",
    "version": "print the version",
}


def _usage() -> str:
    width = max(map(len, COMMANDS))
    lines = [f"browser-llm {__version__}: an OpenAI-compatible API over signed-in "
             f"ChatGPT and Gemini sessions", "", "usage: browser-llm <command> [options]", ""]
    lines += [f"  {name:<{width}}  {text}" for name, text in COMMANDS.items()]
    lines += ["", "browser-llm <command> --help shows that command's options."]
    return "\n".join(lines)


def _run_main(module, prog, rest, *, takes_argv=False):
    """Run a module's main() as if it were its own script."""
    if takes_argv:
        return module.main(rest)
    saved = sys.argv
    sys.argv = [prog] + rest
    try:
        return module.main()
    finally:
        sys.argv = saved


def health(base=None) -> int:
    base = (base or os.environ.get("BROWSER_LLM_API") or "http://localhost:8081").rstrip("/")
    req = urllib.request.Request(base + "/api/status")
    key = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            st = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"unreachable: {base} ({e})", file=sys.stderr)
        return 1
    print(f"browser-llm {st.get('version')} at {base}, up {int(st.get('uptime_seconds') or 0)} s, "
          f"default {st.get('default_provider')}, images from "
          f"{', '.join(st.get('image_providers') or []) or 'none'}")
    for name, p in (st.get("providers") or {}).items():
        quota = p.get("image_quota") or {}
        bits = ["browser up" if p.get("browser_running") else "browser not started",
                "busy" if p.get("busy") else "idle",
                f"{p.get('requests', 0)} requests, {p.get('errors', 0)} errors"]
        if p.get("model_setting"):
            bits.append(f"model {p['model_setting']}")
        if quota.get("remaining") is not None:
            bits.append(f"site counter {quota['remaining']} images")
        if p.get("last_error"):
            bits.append(f"last error: {str(p['last_error'])[:80]}")
        print(f"  {name}: " + "; ".join(bits))
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    cmd, rest = args[0], args[1:]
    prog = f"browser-llm {cmd}"
    if cmd in ("version", "--version", "-V"):
        print(__version__)
        return 0
    if cmd == "serve":
        from . import server
        return _run_main(server, prog, rest) or 0
    if cmd == "login":
        from . import login
        return login.cli(rest, prog=prog)
    if cmd == "ask":
        from . import client
        return _run_main(client, prog, rest) or 0
    if cmd == "image":
        from . import gen_asset
        return _run_main(gen_asset, prog, rest) or 0
    if cmd == "design":
        from . import design
        return design.main(rest, prog=prog)
    if cmd == "mcp":
        from . import mcp_server
        return _run_main(mcp_server, prog, rest) or 0
    if cmd == "health":
        return health(rest[0] if rest else None)
    print(f"browser-llm: unknown command {cmd!r}\n\n{_usage()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
