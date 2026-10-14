# MCP server

The MCP server lets an agent client (Claude Code, Claude Desktop, Cursor, Zed) use the signed-in sessions as native tools. It speaks MCP over stdio as JSON-RPC and is a thin client of the HTTP API, so the server must be running.

## Setting it up

Point the client at `mcp.sh` in the checkout. It makes sure the venv works before starting, so a rebuilt venv never breaks the handshake.

```bash
claude mcp add browser-llm -- /path/to/browser-llm-api/mcp.sh
```

A client with a JSON config:

```json
{
  "mcpServers": {
    "browser-llm": {"command": "/path/to/browser-llm-api/mcp.sh"}
  }
}
```

An installed package works too: `browser-llm mcp` or `browser-llm-mcp`. Point it at another machine with `BROWSER_LLM_API=http://host:8081`, and set `BROWSER_LLM_API_KEY` when that server has a key.

## Tools

| Tool | Does |
|---|---|
| `ask` | a prompt, with optional `images`, `system`, `model` and `model_variant`, returns the reply. `out` writes it to a file and returns the path, which keeps a large HTML answer out of the agent's context, and `strip_fences` drops code-fence lines. The conversation is deleted afterwards unless `keep_chat` is set. |
| `generate_image` | a prompt, with optional `refs` for image-to-image, writes a finished asset: exact size, format, favicon, transparency, `variants`. `kind`, `direction`, `theme`, `platform`, `system`, `copy_space` and `upscale` work as in the design tool. |
| `design_set` | a brief and `sections` make one image per section or screen in one design system, with `manifest.json` and `preview.png`. It takes `directions` and `pick`, `lock`, `refs`, `redo`, `partial` and `dry_run`, and resumes when called again. |
| `design_tokens` | design images become `tokens.json`, `tokens.css` and `DESIGN.md`, with colours checked against the pixels. |
| `design_review` | a design image and a build screenshot give the differences, each with a fix, and a match score; `critique` reviews one screenshot. |
| `list_models` | the provider ids the server can drive. |
| `health` | whether the server is reachable, each provider's state, and whether a request is in flight. |

## How it behaves

Tool calls run on worker threads, so a long image generation never blocks a ping or a second call. Requests still queue per provider on the server, which is where the browser lock is. The server's stdout carries protocol frames only, and every log line goes to stderr.

An image takes about a minute, and a design set takes about a minute per image. Many clients time out a tool call after a few minutes. A set of more than six images is better run from the command line in the background (`browser-llm design set ...`). If a call does time out, the set keeps running and resumes when called again with the same arguments.

When the site's image limit is reached, the image tools fail with the reset time. The agent should stop and report it, not retry.
