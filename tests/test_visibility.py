"""
Tests for the hidden-page guard (2026-09-18 incident): the Chrome flag that
keeps an off-screen or covered window visible, Provider.ensure_visible and
Provider.has_user_turn. No browser: a fake page answers the evaluate/send calls.

Run from the repo root:
    ./venv/bin/python -m unittest discover -s tests -v
"""
import types
import unittest
from unittest import mock

from providers.base import CHROME_ARGS
from providers.chatgpt import ChatGPTProvider
from providers.gemini import GeminiProvider


class FakePage:
    """Answers document.visibilityState from a script of states; records the CDP
    methods sent. `heal_on` names the method after which the page reads visible."""

    def __init__(self, states, heal_on=None, user_turn=False):
        self.states = list(states)
        self.heal_on = heal_on
        self.healed = False
        self.user_turn = user_turn
        self.sent = []
        self.target = types.SimpleNamespace(target_id="T1")

    async def evaluate(self, expr):
        if expr == "document.visibilityState":
            if self.healed:
                return "visible"
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if expr.startswith("!!document.querySelector("):
            return self.user_turn
        raise AssertionError(f"unexpected evaluate: {expr}")

    async def send(self, cmd):
        method = next(cmd)["method"]  # nodriver commands are generators
        self.sent.append(method)
        if method == self.heal_on:
            self.healed = True
        if method == "Browser.getWindowForTarget":
            return (42, None)
        return None


def _no_sleep():
    async def _sleep(_s):
        return None
    return mock.patch("providers.base.asyncio.sleep", new=_sleep)


class ChromeFlag(unittest.TestCase):
    def test_occluded_windows_stay_visible(self):
        # Without it, a window parked off-screen or under another window hides
        # its page and both providers answer nothing (measured on :98).
        self.assertIn("--disable-backgrounding-occluded-windows", CHROME_ARGS)


class EnsureVisible(unittest.IsolatedAsyncioTestCase):
    async def test_visible_page_is_left_alone(self):
        page = FakePage(["visible"])
        await GeminiProvider().ensure_visible(page)
        self.assertEqual(page.sent, [])

    async def test_unreadable_state_does_not_block_the_drive(self):
        page = FakePage([None])
        await GeminiProvider().ensure_visible(page)
        self.assertEqual(page.sent, [])

    async def test_hidden_page_is_restored_and_raised(self):
        page = FakePage(["hidden"], heal_on="Page.bringToFront")
        with _no_sleep():
            await ChatGPTProvider().ensure_visible(page)
        self.assertEqual(page.sent, [
            "Browser.getWindowForTarget",
            "Browser.setWindowBounds",   # windowState normal
            "Browser.setWindowBounds",   # left 0, top 0
            "Page.bringToFront",
        ])

    async def test_a_page_that_stays_hidden_raises_with_the_cause(self):
        page = FakePage(["hidden"])
        with _no_sleep(), self.assertRaises(RuntimeError) as cm:
            await GeminiProvider().ensure_visible(page)
        msg = str(cm.exception)
        self.assertIn("hidden", msg)
        self.assertIn("off-screen", msg)

    async def test_a_failed_restore_still_raises_rather_than_hanging(self):
        page = FakePage(["hidden"])

        async def broken_send(cmd):
            raise RuntimeError("Browser domain unavailable")
        page.send = broken_send
        with _no_sleep(), self.assertRaises(RuntimeError) as cm:
            await GeminiProvider().ensure_visible(page)
        self.assertIn("hidden", str(cm.exception))


class HasUserTurn(unittest.IsolatedAsyncioTestCase):
    async def test_reads_the_declared_selector(self):
        self.assertTrue(await ChatGPTProvider().has_user_turn(FakePage(["visible"], user_turn=True)))
        self.assertFalse(await GeminiProvider().has_user_turn(FakePage(["visible"], user_turn=False)))

    async def test_no_selector_means_unknown(self):
        p = GeminiProvider()
        p.user_turn_selector = ""
        self.assertIsNone(await p.has_user_turn(FakePage(["visible"])))


if __name__ == "__main__":
    unittest.main()
