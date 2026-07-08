"""
Unit tests for CompletionTracker — the provider-agnostic "is the answer done?"
decision logic. No browser required: each test feeds a synthetic poll timeline.

Run from the repo root:
    ./venv/bin/python -m unittest discover -s tests -v
"""
import unittest

from providers.base import CompletionTracker

NO_IMG = {"loaded": 0, "pending": 0, "creating": False}


class TextCompletion(unittest.TestCase):
    def test_streams_chunks_then_settles(self):
        t = CompletionTracker()
        chunks = []

        c, done = t.feed(0.0, "Hel", True, NO_IMG)
        chunks.append(c)
        self.assertIsNone(done)

        c, done = t.feed(1.0, "Hello wor", True, NO_IMG)
        chunks.append(c)
        self.assertIsNone(done)

        c, done = t.feed(2.0, "Hello world", False, NO_IMG)
        chunks.append(c)
        self.assertIsNone(done)  # text just changed — not settled yet

        # Unchanged text while not generating: done after SILENT_TEXT_DONE.
        c, done = t.feed(3.0, "Hello world", False, NO_IMG)
        self.assertIsNone(done)
        c, done = t.feed(2.0 + CompletionTracker.SILENT_TEXT_DONE + 0.1,
                         "Hello world", False, NO_IMG)
        self.assertEqual(done, "text")
        self.assertEqual("".join(chunks), "Hello world")

    def test_not_done_while_generating(self):
        t = CompletionTracker()
        for now in (0.0, 5.0, 30.0, 120.0):
            _, done = t.feed(now, "same text", True, NO_IMG)
            self.assertIsNone(done)

    def test_empty_answer_gives_up(self):
        t = CompletionTracker()
        _, done = t.feed(0.0, "", True, NO_IMG)   # generation happened
        self.assertIsNone(done)
        _, done = t.feed(1.0, "", False, NO_IMG)
        self.assertIsNone(done)
        _, done = t.feed(CompletionTracker.SILENT_EMPTY_DONE + 1.0, "", False, NO_IMG)
        self.assertEqual(done, "empty")


class ImageCompletion(unittest.TestCase):
    def test_placeholder_suppressed_and_image_completes(self):
        t = CompletionTracker()
        creating = {"loaded": 0, "pending": 0, "creating": True}

        # "Creating your image…" must never be surfaced as answer text.
        c, done = t.feed(0.0, "Creating your image", True, creating)
        self.assertEqual(c, "")
        self.assertIsNone(done)

        # Image rendered; still generating → wait for stability.
        one = {"loaded": 1, "pending": 0, "creating": False}
        c, done = t.feed(10.0, "", True, one)
        self.assertIsNone(done)

        # Stable for IMAGE_STABLE seconds → done "image" (even if a stop
        # button lingers — is_generating stays True here on purpose).
        c, done = t.feed(10.0 + CompletionTracker.IMAGE_STABLE + 0.1, "", True, one)
        self.assertEqual(done, "image")

    def test_waits_for_image_after_generation_ends(self):
        t = CompletionTracker()
        creating = {"loaded": 0, "pending": 0, "creating": True}
        # Generation ended but the image is still rendering → keep waiting
        # (no "empty"/"text" completion) well past SILENT_EMPTY_DONE.
        _, done = t.feed(0.0, "", True, creating)
        self.assertIsNone(done)
        _, done = t.feed(20.0, "", False, creating)
        self.assertIsNone(done)


class FalseCreatingGuard(unittest.TestCase):
    """A code-editor <canvas> once made image_status report creating=True
    forever; the guard un-suppresses the text after FALSE_CREATING_TIMEOUT."""

    def test_stuck_creating_falls_back_to_text(self):
        t = CompletionTracker()
        stuck = {"loaded": 0, "pending": 0, "creating": True}
        answer = "def main():\n    print('a big code answer')"

        _, done = t.feed(0.0, answer, True, stuck)
        self.assertIsNone(done)
        c, done = t.feed(5.0, answer, False, stuck)
        self.assertEqual(c, "")          # still suppressed
        self.assertIsNone(done)

        # Past the guard: text is released...
        after = 5.0 + CompletionTracker.FALSE_CREATING_TIMEOUT + 1.0
        c, done = t.feed(after, answer, False, stuck)
        self.assertEqual(c, answer)
        self.assertIsNone(done)

        # ...and then completes as a normal settled text answer.
        c, done = t.feed(after + CompletionTracker.SILENT_TEXT_DONE + 0.1,
                         answer, False, stuck)
        self.assertEqual(done, "text")


if __name__ == "__main__":
    unittest.main()
