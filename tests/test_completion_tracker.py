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


class BufferedText(unittest.TestCase):
    """`tracker.text` holds the last non-empty full text — what buffered_stream
    providers (ChatGPT) emit once at completion, since their extracted text
    reshapes near the end and can't be streamed as append-only deltas."""

    def test_text_tracks_last_nonempty_and_survives_reshape(self):
        t = CompletionTracker()
        t.feed(0.0, "Python\nRun\ndef add(a, b):", True, NO_IMG)   # flattened, streaming
        self.assertEqual(t.text, "Python\nRun\ndef add(a, b):")
        # reshape to the final fenced form (NOT a prefix-extension)
        final = "```python\ndef add(a, b):\n    return a + b\n```"
        t.feed(1.0, final, False, NO_IMG)
        self.assertEqual(t.text, final)
        # a transient empty read must not clobber the captured text
        _, done = t.feed(1.0 + CompletionTracker.SILENT_TEXT_DONE + 0.1, "", False, NO_IMG)
        self.assertEqual(done, "text")
        self.assertEqual(t.text, final)


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


class StatusPlaceholder(unittest.TestCase):
    """A vision request makes ChatGPT show "Analyzing image" in the assistant
    bubble, sometimes with no stop button — that settled short text used to be
    returned AS the answer (seen 2026-07-28). It must never be the reply, and it
    must not end the poll early."""

    def test_analyzing_placeholder_never_becomes_the_answer(self):
        t = CompletionTracker()
        answer = "A blue circle beside the words SEVEN ZEBRAS."

        # placeholder showing, stop button gone → looks "settled" but isn't
        c, done = t.feed(0.0, "Analyzing image", False, NO_IMG)
        self.assertEqual(c, "")
        self.assertIsNone(done)
        c, done = t.feed(CompletionTracker.SILENT_TEXT_DONE + 1.0,
                         "Analyzing image", False, NO_IMG)
        self.assertEqual(c, "")
        self.assertIsNone(done, "placeholder must not complete the request")

        # the real answer arrives and completes normally
        c, done = t.feed(6.0, answer, True, NO_IMG)
        self.assertEqual(c, answer)
        _, done = t.feed(6.0 + CompletionTracker.SILENT_TEXT_DONE + 0.1,
                         answer, False, NO_IMG)
        self.assertEqual(done, "text")
        self.assertEqual(t.text, answer)

    def test_long_answer_opening_with_a_placeholder_word_is_kept(self):
        # "Analyzing the image, I can see …" is a real answer, not a status line:
        # only short text counts as a placeholder.
        t = CompletionTracker()
        answer = ("Analyzing the image, I can see a solid blue circle in the upper "
                  "left and two lines of text.")
        c, done = t.feed(0.0, answer, False, NO_IMG)
        self.assertEqual(c, answer)
        _, done = t.feed(CompletionTracker.SILENT_TEXT_DONE + 0.1, answer, False, NO_IMG)
        self.assertEqual(done, "text")

    def test_placeholder_extends_the_empty_give_up_window(self):
        t = CompletionTracker()
        _, done = t.feed(0.0, "Analyzing image", True, NO_IMG)   # generation seen
        self.assertIsNone(done)
        # past the normal empty deadline, still patient because a status line shows
        _, done = t.feed(CompletionTracker.SILENT_EMPTY_DONE + 2.0,
                         "Analyzing image", False, NO_IMG)
        self.assertIsNone(done)
        # but it does eventually give up rather than hang forever
        _, done = t.feed(CompletionTracker.SILENT_PLACEHOLDER_DONE + 1.0,
                         "Analyzing image", False, NO_IMG)
        self.assertEqual(done, "empty")


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
