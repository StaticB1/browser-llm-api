"""
Interactive login helper (generic across providers).

    python login.py gemini      # or: gemini-browser
    python login.py chatgpt     # or: chatgpt-browser

Opens the SAME browser context the server uses (nodriver, --password-store=basic)
and holds it open so you can sign in manually in the window. Because the login
happens in the automation's own cookie store, the server can read the session
afterwards (no keyring-backend mismatch).

The server's Chrome is invisible (Xvfb), so sign-in must be done here on a real
display, e.g.:  DISPLAY=:1 python login.py chatgpt

Emits markers on stdout:
  WAITING_FOR_LOGIN   - browser open, sign in now
  STILL_OUT (Ns)      - periodic: still logged out
  LOGGED_IN_DETECTED  - signed-in session detected
  CLOSING             - flushing cookies and shutting down cleanly
  TIMEOUT             - gave up after the deadline
"""
import asyncio
import sys

import nodriver as uc

from providers import PROVIDERS, get_provider, patch_cdp, CHROME_ARGS

patch_cdp()

DEADLINE_S = 360      # 6 minutes to complete sign-in
POLL_S = 3


def _resolve(arg: str):
    # Accept "gemini" / "chatgpt" shorthands as well as full provider names.
    if arg in PROVIDERS:
        return PROVIDERS[arg]
    for name, prov in PROVIDERS.items():
        if name.startswith(arg):
            return prov
    return None


async def _authed(browser, page, provider) -> bool:
    """Definitive login check. If the provider names a session cookie, require
    THAT to be present (only set after the sign-in flow fully completes);
    otherwise fall back to the DOM check. The cookie test avoids closing the
    browser mid-redirect, before the real session cookie is written."""
    sc = getattr(provider, "session_cookie", None)
    if sc:
        name, domain = sc
        try:
            # Prefix match: ChatGPT splits large session tokens into chunked
            # cookies (__Secure-next-auth.session-token.0 / .1), so match the
            # configured name as a prefix rather than an exact string.
            for c in await browser.cookies.get_all():
                if getattr(c, "name", "").startswith(name) and domain in (getattr(c, "domain", "") or ""):
                    return True
            return False
        except Exception:
            pass  # fall through to DOM check if the cookie API hiccups
    try:
        return await provider.logged_in(page)
    except Exception:
        return False


async def main(provider):
    browser = await uc.start(user_data_dir=provider.profile_dir, browser_args=list(CHROME_ARGS))
    page = await browser.get(provider.chat_url)
    print("WAITING_FOR_LOGIN", flush=True)

    waited = 0
    ok = False
    while waited < DEADLINE_S:
        await asyncio.sleep(POLL_S)
        waited += POLL_S
        if await _authed(browser, page, provider):
            print("LOGGED_IN_DETECTED", flush=True)
            ok = True
            break
        if waited % 15 == 0:
            print(f"STILL_OUT ({waited}s)", flush=True)

    if not ok:
        print("TIMEOUT", flush=True)
    else:
        # Reload the app so the session cookie is exercised + committed, then
        # give Chrome time to flush it to the profile before we close. Confirm
        # it survives a reload so we don't persist a half-finished session.
        try:
            page = await browser.get(provider.chat_url)
            await asyncio.sleep(6)
            print("PERSISTED" if await _authed(browser, page, provider) else "PERSIST_UNCONFIRMED", flush=True)
        except Exception:
            pass
        await asyncio.sleep(8)  # let Chrome's cookie store flush to disk

    print("CLOSING", flush=True)
    browser.stop()
    await asyncio.sleep(2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"usage: python login.py <{'|'.join(PROVIDERS)}>", file=sys.stderr)
        sys.exit(2)
    prov = _resolve(sys.argv[1])
    if prov is None:
        print(f"unknown provider {sys.argv[1]!r}; choices: {', '.join(PROVIDERS)}", file=sys.stderr)
        sys.exit(2)
    uc.loop().run_until_complete(main(prov))
