"""
Access control + remote-upstream config for the API server (stdlib only).

Two independent knobs, both env-driven, both off by default:

BROWSER_LLM_API_KEY
    When set, requests from NON-loopback clients must present the key
    (``Authorization: Bearer <key>`` or ``X-Api-Key: <key>``) on API paths
    (/v1/*, /api/*). Loopback clients (the local web UI, desktop app, CLI)
    stay unauthenticated, and page/asset paths (/, /widget.js, /images/*, …)
    stay public so served image links and the UI shell keep working in a
    plain browser. This is what makes binding to 0.0.0.0 sane.

REMOTE_PROVIDERS / REMOTE_API_KEY
    ``REMOTE_PROVIDERS="chatgpt-browser=http://192.168.1.34:8081"`` makes this
    server PROXY requests for that model to another browser-llm-api instance
    instead of driving a local browser — e.g. a friend's install forwarding
    ChatGPT traffic to the one machine that has a logged-in ChatGPT session.
    Comma-separate multiple ``model=url`` pairs. REMOTE_API_KEY is sent as the
    Bearer key on proxied requests (the upstream's BROWSER_LLM_API_KEY).

``is_loopback`` has a second consumer beyond the API-key middleware: server.py
uses it to decide whether a caller may attach **server-side file paths** to a
request (see ALLOW_REMOTE_FILE_PATHS). Attaching a path makes the browser upload
that file, so remote callers must send bytes instead. That decision also needs
``origin_is_trusted``: a request from a *page on another site* reaches the server
over loopback like any local client, so loopback alone would let any website the
operator visits read files off this box (see that function).

Kept separate from server.py so tests can import these helpers without
server.py's import-time side effects (log-file truncation, CDP patch).
"""
import hmac
from typing import Optional

# Paths that stay public even for remote clients when an API key is set.
# Pages/assets only — everything under /v1/ and /api/ requires the key.
# /images/* is public on purpose: chat responses hand out plain <img>-able
# URLs (unguessable uuid-hex names), and a browser can't attach headers to
# an image tag.
_PUBLIC_EXACT = {"/", "/ui", "/version", "/demo", "/widget-demo", "/widget.js",
                 "/favicon.ico", "/docs", "/openapi.json"}
_PUBLIC_PREFIXES = ("/images/",)

# Spellings of "this machine" that name the same server in an Origin/Host pair.
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _split_hostport(netloc: str) -> tuple:
    """('localhost:8081') -> ('localhost', '8081'); IPv6-literal safe."""
    if netloc.startswith("["):
        end = netloc.find("]")
        if end != -1:
            host, rest = netloc[:end + 1], netloc[end + 1:]
            return host, rest[1:] if rest.startswith(":") else ""
    host, sep, port = netloc.partition(":")
    return host, port if sep else ""


def is_loopback(host: Optional[str]) -> bool:
    """True if the client address is the local machine (IPv4 127/8 or ::1,
    including IPv4-mapped ::ffff:127.x)."""
    if not host:
        return False
    h = host.lower()
    if h.startswith("::ffff:"):
        h = h[len("::ffff:"):]
    return h == "::1" or h == "localhost" or h.startswith("127.")


def origin_is_trusted(origin: Optional[str], host: Optional[str]) -> bool:
    """True when the request did NOT come from a page on some other origin.

    Loopback is not the same as trusted: CORS here is wide open (the embeddable
    widget has to work from any page on the LAN), so *any* website the operator
    visits can make their browser POST to http://localhost:8081 and read the
    answer. Such a request arrives from 127.0.0.1 and would otherwise pass every
    loopback check — including the one guarding server-side file-path
    attachments, which turns a drive-by into a read of any file the server user
    can open. The browser always labels those requests with an ``Origin``, so an
    Origin that does not match the host the request was addressed to means
    "another site is driving this".

    No Origin at all (curl, the CLI, the desktop app, a proxying server) is
    trusted: only browsers set it, and a browser cannot omit it cross-origin.
    """
    o = (origin or "").strip()
    if not o or o.lower() == "null":
        return True
    from urllib.parse import urlsplit
    o_host = urlsplit(o).netloc.lower()
    if not o_host:
        return False
    h = (host or "").strip().lower()
    if o_host == h:
        return True
    # Compare host and port separately: the Host header may omit the port, and
    # the loopback spellings all name the same server.
    o_bare, o_port = _split_hostport(o_host)
    h_bare, h_port = _split_hostport(h)
    if o_port and h_port and o_port != h_port:
        return False
    return (o_bare == h_bare) or (o_bare in _LOOPBACK_NAMES and h_bare in _LOOPBACK_NAMES)


def needs_key(path: str, method: str) -> bool:
    """Should this request require the API key (given a non-loopback client
    and a configured key)? CORS preflights (OPTIONS) carry no auth headers by
    spec, so they always pass — the follow-up real request is still checked."""
    if method.upper() == "OPTIONS":
        return False
    if path in _PUBLIC_EXACT:
        return False
    return not any(path.startswith(p) for p in _PUBLIC_PREFIXES)


def extract_key(authorization: Optional[str], x_api_key: Optional[str]) -> str:
    """Pull the client-supplied key out of ``Authorization: Bearer …`` (case-
    insensitive scheme) or the ``X-Api-Key`` header. Empty string if absent."""
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    if x_api_key:
        return x_api_key.strip()
    return ""


def key_matches(supplied: str, expected: str) -> bool:
    """Constant-time comparison; False for an empty supplied key."""
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def parse_remote_providers(raw: Optional[str]) -> dict[str, str]:
    """Parse ``model=url[,model=url…]`` into {model: url} (urls stripped of a
    trailing slash). Malformed entries are skipped rather than fatal — a typo
    in the env var shouldn't take the whole server down."""
    remotes: dict[str, str] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        model, url = entry.split("=", 1)
        model, url = model.strip(), url.strip().rstrip("/")
        if model and url.startswith(("http://", "https://")):
            remotes[model] = url
    return remotes
