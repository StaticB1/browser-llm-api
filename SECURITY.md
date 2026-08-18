# Security

This server drives logged-in accounts. Anyone who can reach it can talk to your ChatGPT and
Gemini sessions as you, read what the model says back, and, if local-path attachments are open to
them, get files off the machine uploaded to a third party. Treat the port the way you'd treat an
unlocked browser, not the way you'd treat a stateless API.

## Reporting a vulnerability

Open a [security advisory](https://github.com/StaticB1/browser-llm-api/security/advisories/new), or
email bsiwonde@gmail.com. Please don't file a public issue for anything that lets a caller read
files, reach the session, or bypass the key.

There is no bounty and this is a personal project, so expect a reply in days rather than hours.

## What the code actually defends

| Surface | Rule |
|---|---|
| `/v1/*`, `/api/*` | Open from loopback. From anywhere else, `BROWSER_LLM_API_KEY` is required (`Authorization: Bearer` or `X-Api-Key`) when it's set. |
| Pages and assets (`/`, `/ui`, `/widget.js`, `/demo`, `/version`, `/images/*`) | Always public, because an image link has to work in a bare `<img>`. Filenames are unguessable UUID hex. |
| Local file paths as attachments | Loopback and same-origin only. `ALLOW_REMOTE_FILE_PATHS=1` opts out. |
| Attachment size and count | `MAX_ATTACHMENT_MB` (20), `MAX_ATTACHMENTS` (6). |
| Bind address | `127.0.0.1` by default. LAN exposure is opt-in via `BROWSER_LLM_HOST`. |

The decision logic lives in `authz.py`, on its own so it can be unit-tested without starting the
server: `tests/test_authz.py` and `tests/test_attachments.py`.

## Loopback is not the same as trusted

CORS is deliberately wide open, because the embeddable widget is meant to run on other pages. That
means **any website open in your browser can make your browser POST to this server**, and the
request arrives from `127.0.0.1` and passes a naive loopback check.

That was a real hole, not a theoretical one. Before it was fixed (2026-08-11), a page at
`Origin: https://evil.example` could name a local file path, get the server to upload that file to
ChatGPT, and read the model's description of it back: a file-read primitive for any path the page
could guess. `authz.origin_is_trusted()` now requires either no `Origin` header (a CLI or the
desktop app; a browser cannot omit it cross-origin) or one that matches the host being addressed.

If you're changing that path policy: don't simplify it back to a loopback check.

## Running it safely

- Keep the default `127.0.0.1` bind unless you need the LAN, and set `BROWSER_LLM_API_KEY` if you
  do. Firewall the port to your subnet.
- Never put it on the public internet. There is no rate limiting, no account separation, and the
  browser profile it drives is a real signed-in session.
- The `*_profile/` directories hold live session cookies. They're gitignored; keep it that way, and
  don't copy them around.
- `REMOTE_PROVIDERS` forwards prompts and attachments to another instance. Only point it at a host
  you control, and set `REMOTE_API_KEY`.

## Out of scope

Terms-of-service risk to your provider accounts is real, but it isn't a vulnerability in this code.
See the disclaimer in the README. Site breakage from a DOM change isn't one either.
