"""
Interactive login helper.

Opens the SAME browser context the server uses (nodriver, --password-store=basic)
and holds it open so you can sign in to Google/Gemini manually in the window.
Because the login happens in the automation's own cookie store, the server will
be able to read the session afterwards (no keyring-backend mismatch).

Emits markers on stdout:
  WAITING_FOR_LOGIN   - browser open, sign in now
  STILL_OUT (Ns)      - periodic: still logged out
  LOGGED_IN_DETECTED  - composer + Send button present
  CLOSING             - flushing cookies and shutting down cleanly
  TIMEOUT             - gave up after the deadline
"""
import asyncio
import nodriver as uc
import nodriver.cdp.util as _cdp_util

_orig = _cdp_util.parse_json_event
def _safe(j):
    try:
        return _orig(j)
    except KeyError:
        return None
_cdp_util.parse_json_event = _safe

DEADLINE_S = 360      # 6 minutes to complete sign-in
POLL_S = 3


async def logged_in(page) -> bool:
    # Don't rely on the Send button (it only renders once text is typed).
    # Logged-in = no "Sign in" button, not the "Meet Gemini" landing page,
    # and ideally an account email present in a profile aria-label.
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


async def main():
    browser = await uc.start(user_data_dir="./gemini_profile")
    page = await browser.get("https://gemini.google.com/app")
    print("WAITING_FOR_LOGIN", flush=True)

    waited = 0
    ok = False
    while waited < DEADLINE_S:
        await asyncio.sleep(POLL_S)
        waited += POLL_S
        try:
            if await logged_in(page):
                print("LOGGED_IN_DETECTED", flush=True)
                ok = True
                break
        except Exception:
            pass
        if waited % 15 == 0:
            print(f"STILL_OUT ({waited}s)", flush=True)

    if not ok:
        print("TIMEOUT", flush=True)

    # Give Chrome a moment to persist cookies, then close gracefully.
    await asyncio.sleep(6)
    print("CLOSING", flush=True)
    browser.stop()
    await asyncio.sleep(1)


if __name__ == "__main__":
    uc.loop().run_until_complete(main())
