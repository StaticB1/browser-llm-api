"""
Tests for ChatGPT's page selectors after its redesign of 11 Oct 2026, which
dropped `#prompt-textarea`, `data-message-author-role` and nearly every
`data-testid` at once. Every selector list must keep the earlier layout AND the
one measured on 12 Oct 2026, and a missing composer must say whether the page
still holds an editable element (a redesign) or none (a sign-in wall).

No browser: a fake page records the expressions it is asked to evaluate. These
tests cannot tell whether the live site still matches; only a real request can.

Run from the repo root:
    ./venv/bin/python -m unittest discover -s tests -v
"""
import json
import unittest

from providers.chatgpt import ChatGPTProvider
from providers.gemini import GeminiProvider


class RecordingPage:
    """Returns `answer` for every evaluate and keeps each expression."""

    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.exprs = []

    async def evaluate(self, expr, *args, **kwargs):
        self.exprs.append(expr)
        if self.error:
            raise self.error
        return self.answer


class SelectorListsKeepBothLayouts(unittest.TestCase):
    def setUp(self):
        self.p = ChatGPTProvider()

    def test_composer(self):
        self.assertIn("#prompt-textarea", self.p.input_selector)
        self.assertIn('form[data-chatgpt-composer] [contenteditable="true"]', self.p.input_selector)

    def test_send_button_is_one_entry_with_every_layout(self):
        # base.open_and_send waits up to 3 s per entry, so the layouts share one.
        self.assertEqual(len(self.p.send_selectors), 1)
        sel = self.p.send_selectors[0]
        self.assertIn('button[data-testid="send-button"]', sel)
        self.assertIn('form[data-chatgpt-composer] button[type="submit"]', sel)

    def test_user_turn(self):
        self.assertIn('[data-message-author-role="user"]', self.p.user_turn_selector)
        self.assertIn('[data-chatgpt-search-unit-key$=":user"]', self.p.user_turn_selector)

    def test_attachment_chips(self):
        js = self.p.attachment_ready_js
        self.assertIn('button[aria-label^="Remove file"]', js)
        self.assertIn('button[aria-label^="Remove "]', js)


class InPageReads(unittest.IsolatedAsyncioTestCase):
    async def test_answer_read_knows_both_turn_markers(self):
        page = RecordingPage(answer="pong")
        self.assertEqual(await ChatGPTProvider().get_response_text(page), "pong")
        js = page.exprs[0]
        self.assertIn('[data-message-author-role="assistant"]', js)
        self.assertIn('[data-chatgpt-search-unit-key$=":assistant"]', js)
        self.assertIn('[data-markdown-text-style="assistant-message"]', js)
        self.assertNotIn("__COMPOSER__", js)

    async def test_generating_reads_the_new_stop_button(self):
        page = RecordingPage(answer=True)
        self.assertTrue(await ChatGPTProvider().is_generating(page))
        js = page.exprs[0]
        self.assertIn('[data-testid="stop-button"]', js)
        self.assertIn('form button[aria-label="Stop"]', js)
        self.assertIn('[data-talvt-turn-state="in_progress"]', js)

    async def test_logged_in_looks_for_the_composer_list(self):
        page = RecordingPage(answer=True)
        self.assertTrue(await ChatGPTProvider().logged_in(page))
        self.assertIn(json.dumps(ChatGPTProvider.input_selector + ', [data-testid="composer-send-button"]'),
                      page.exprs[0])

    async def test_uploaded_image_in_the_user_turn_is_input(self):
        page = RecordingPage(answer=json.dumps({"loaded": 0, "pending": 0, "creating": False}))
        await ChatGPTProvider().image_status(page)
        self.assertIn('[data-chatgpt-search-unit-key$=\\":user\\"]', page.exprs[0])
        self.assertNotIn("__USER_TURN__", page.exprs[0])


class ComposerHint(unittest.IsolatedAsyncioTestCase):
    async def test_an_editable_element_means_the_site_changed(self):
        page = RecordingPage(answer=json.dumps({"ce": 1, "ta": 0, "url": "https://chatgpt.com/"}))
        hint = await ChatGPTProvider().composer_hint(page)
        self.assertIn("1 contenteditable and 0 textarea", hint)
        self.assertIn("providers.chatgpt", hint)

    async def test_nothing_editable_means_a_wall(self):
        page = RecordingPage(answer=json.dumps({"ce": 0, "ta": 0, "url": "https://example.test/login"}))
        hint = await GeminiProvider().composer_hint(page)
        self.assertIn("no editable element", hint)
        self.assertIn("https://example.test/login", hint)

    async def test_an_unreadable_page_adds_nothing(self):
        page = RecordingPage(error=RuntimeError("target closed"))
        self.assertEqual(await ChatGPTProvider().composer_hint(page), "")


if __name__ == "__main__":
    unittest.main()
