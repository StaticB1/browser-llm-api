"""
Tests for the ChatGPT Plus upgrade (EBE-2054, 13 Oct 2026): the image limit
read from the site's counter or its refusal text, the model choice a request
can make, the size and background words an image request types, the pixel
size reported back, the 403/429 answers of the image endpoint, gen_asset's
exact-size output, kept original and variants, and the draft the composer
restores after a failed send.

No browser and no network beyond a loopback server started here. These tests
cannot tell whether chatgpt.com still words its refusal this way; the live
check in CLAUDE.md does that.

Run from the repo root:
    ./venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402
from PIL import Image  # noqa: E402

import gen_asset  # noqa: E402
import server  # noqa: E402
from providers.base import StreamMonitor, parse_size, size_instruction  # noqa: E402
from providers.chatgpt import (  # noqa: E402
    ChatGPTProvider, effort_matches, model_version, parse_variant)
from providers.gemini import GeminiProvider  # noqa: E402
from providers.limits import (  # noqa: E402
    UsageLimitReached, describe_reset, parse_limit_message, quota_refusal,
    refusal_from_message)

NOW = datetime(2026, 10, 13, 14, 7, tzinfo=timezone.utc)


def size_of(path):
    with Image.open(path) as im:
        return im.size


def png_bytes(w, h, mode="RGB", color=(200, 120, 40)):
    buf = io.BytesIO()
    Image.new(mode, (w, h), color if mode == "RGB" else color + (255,)).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# The site's counter
# ---------------------------------------------------------------------------
class QuotaRefusal(unittest.TestCase):
    RESET = "2026-10-14T14:07:00+00:00"

    def progress(self, remaining):
        # The shape POST /backend-api/conversation/init answered on 13 Oct 2026.
        return [{"feature_name": "file_upload", "remaining": 80,
                 "reset_after": "2026-10-13T17:07:00+00:00"},
                {"feature_name": "image_gen", "remaining": remaining,
                 "reset_after": self.RESET}]

    def test_plenty_left_is_no_refusal(self):
        self.assertIsNone(quota_refusal(self.progress(120), now=NOW))

    def test_none_left_refuses_with_the_reset_time(self):
        e = quota_refusal(self.progress(0), now=NOW)
        self.assertIsInstance(e, UsageLimitReached)
        self.assertEqual(e.remaining, 0)
        self.assertEqual(e.reset_at, datetime(2026, 10, 14, 14, 7, tzinfo=timezone.utc))
        self.assertEqual(e.retry_after(), 24 * 3600 + 1)
        self.assertIn("in 24 h 0 min", str(e))
        self.assertIn("do not retry", str(e))

    def test_reserve_keeps_some_back(self):
        self.assertIsNone(quota_refusal(self.progress(11), reserve=10, now=NOW))
        e = quota_refusal(self.progress(10), reserve=10, now=NOW)
        self.assertIsNotNone(e)
        self.assertIn("10 are kept back", str(e))

    def test_blocked_feature_refuses(self):
        e = quota_refusal(self.progress(120), now=NOW, blocked_features=[
            {"feature_name": "image_gen", "reset_after": self.RESET}])
        self.assertIsNotNone(e)
        self.assertIn("blocked", str(e))
        self.assertEqual(e.retry_after(), 24 * 3600 + 1)

    def test_unreadable_counter_never_blocks(self):
        for progress in (None, [], [{"feature_name": "image_gen"}],
                         [{"feature_name": "image_gen", "remaining": "0"}],
                         [{"feature_name": "image_gen", "remaining": True}], ["junk"]):
            self.assertIsNone(quota_refusal(progress, now=NOW), progress)

    def test_describe_reset(self):
        self.assertEqual(describe_reset(None, NOW), "")
        self.assertIn("in 3 min", describe_reset(NOW + timedelta(minutes=3), NOW))
        self.assertIn("in 1 min", describe_reset(NOW + timedelta(seconds=10), NOW))


# ---------------------------------------------------------------------------
# The site's refusal text
# ---------------------------------------------------------------------------
class LimitMessage(unittest.TestCase):
    def test_plan_limit_with_a_relative_reset(self):
        text = ("You've hit the Plus plan limit for image generation requests. "
                "You can create more images when the limit resets in 23 hours and 59 minutes.")
        hit = parse_limit_message(text, NOW)
        self.assertEqual(hit["kind"], "limit")
        self.assertEqual(hit["reset_at"], NOW + timedelta(hours=23, minutes=59))
        self.assertIn("Plus plan limit", hit["message"])

    def test_curly_apostrophe_and_too_fast(self):
        text = ("You’re generating images too quickly. To ensure the best experience "
                "for everyone, we have rate limits in place. Please wait for 3 minutes "
                "before generating more images.")
        hit = parse_limit_message(text, NOW)
        self.assertEqual(hit["kind"], "too_fast")
        self.assertEqual(hit["reset_at"], NOW + timedelta(minutes=3))

    def test_clock_time_is_the_next_occurrence_in_local_time(self):
        hit = parse_limit_message(
            "You've reached the image generation limit. Try again after 4:12 PM.", NOW)
        self.assertIsNotNone(hit)
        local = hit["reset_at"].astimezone()
        self.assertEqual((local.hour, local.minute), (16, 12))
        self.assertGreater(hit["reset_at"], NOW)
        self.assertLessEqual(hit["reset_at"] - NOW, timedelta(days=1))

    def test_no_reset_time_given(self):
        hit = parse_limit_message("You have reached your image generation limit.", NOW)
        self.assertIsNotNone(hit)
        self.assertIsNone(hit["reset_at"])
        e = refusal_from_message("You have reached your image generation limit.", NOW)
        self.assertIn("did not say when", str(e))
        self.assertIsNone(e.retry_after())

    def test_ordinary_answers_are_not_refusals(self):
        for text in (
            "",
            "Here is your image of a lighthouse at dawn.",
            "ChatGPT Plus has higher limits for image generation than the free plan.",
            "You've hit the limit of what this recipe can do.",          # no image word
            "The image shows a speed limit sign.",
            "You've hit the Plus plan limit for image generation. " + "x" * 1300,  # too long
        ):
            self.assertIsNone(parse_limit_message(text, NOW), text[:60])

    def test_refusal_from_message_is_a_429_cause(self):
        e = refusal_from_message(
            "You've hit the Plus plan limit for image generation requests. "
            "It resets in 2 hours.", NOW)
        self.assertIsInstance(e, UsageLimitReached)
        self.assertEqual(e.retry_after(), 2 * 3600 + 1)
        self.assertEqual(e.source, "the page's message")
        self.assertIn("in 2 h 0 min", str(e))


# ---------------------------------------------------------------------------
# Model choice
# ---------------------------------------------------------------------------
class ModelChoice(unittest.TestCase):
    def test_parse_variant(self):
        self.assertEqual(parse_variant(None), (None, None))
        self.assertEqual(parse_variant("instant"), (None, "instant"))
        self.assertEqual(parse_variant("Extended"), (None, "high"))
        self.assertEqual(parse_variant("standard"), (None, "medium"))
        self.assertEqual(parse_variant("GPT-5.6 Sol"), ("GPT-5.6 Sol", None))
        self.assertEqual(parse_variant("GPT-6/medium"), ("GPT-6", "medium"))
        self.assertEqual(parse_variant(" high / GPT-6 "), ("GPT-6", "high"))

    def test_effort_is_read_from_the_word_in_the_label(self):
        # GPT-6's button reads "High"; GPT-5.6 Sol's read "5.6 Instant" (13 Oct 2026).
        self.assertTrue(effort_matches("High", "high"))
        self.assertTrue(effort_matches("5.6 Instant", "instant"))
        self.assertFalse(effort_matches("5.6 Instant", "high"))
        self.assertFalse(effort_matches("Medium", "high"))
        self.assertFalse(effort_matches(None, "high"))
        self.assertFalse(effort_matches("High", None))

    def test_model_version_matches_labels_and_slugs(self):
        self.assertEqual(model_version("GPT-6"), "6")
        self.assertEqual(model_version("gpt-6-thinking"), "6")
        self.assertEqual(model_version("GPT-5.6 Sol"), "5.6")
        self.assertEqual(model_version("gpt-5-6-thinking"), "5.6")
        self.assertEqual(model_version(None), "")

    def test_a_different_sent_model_forgets_the_cached_picker(self):
        p = ChatGPTProvider()
        p._model_seen = "GPT-6"
        p.note_model_used({"model": "gpt-6-thinking", "requested_by_page": "gpt-6-thinking"})
        self.assertEqual(p._model_seen, "GPT-6")
        p.note_model_used({"requested_by_page": "gpt-5-6-thinking"})
        self.assertIsNone(p._model_seen)
        self.assertEqual(p.status_extra()["last_model_used"],
                         {"requested_by_page": "gpt-5-6-thinking"})

    def test_monitor_notes_what_the_page_sent(self):
        m = StreamMonitor(["backend-api/conversation"])
        m._note_model(json.dumps({"action": "next", "model": "gpt-6-thinking",
                                  "thinking_effort": "extended", "messages": []}))
        self.assertEqual(m.sent_model, {"model": "gpt-6-thinking",
                                        "thinking_effort": "extended"})
        for junk in (None, "", "not json with \"model\"", json.dumps({"x": 1}),
                     json.dumps({"model": 5})):
            m._note_model(junk)
        self.assertEqual(m.sent_model["model"], "gpt-6-thinking")


class MonitorAsksForBodies(unittest.IsolatedAsyncioTestCase):
    # Chrome omits postData from requestWillBeSent unless Network.enable names
    # a size; without it sent_model stayed empty on every live request.
    async def test_enable_bodies_names_a_post_data_size(self):
        sent = []

        class Tab:
            async def send(self, cmd):
                sent.append(next(cmd))

        await StreamMonitor(["backend-api/conversation"]).enable_bodies(Tab())
        self.assertEqual(sent[0]["method"], "Network.enable")
        self.assertGreaterEqual(sent[0]["params"]["maxPostDataSize"], 65536)

    async def test_open_and_send_asks_for_bodies(self):
        calls = []

        class Monitor:
            def attach(self, tab):
                calls.append("attach")

            async def enable_bodies(self, tab):
                calls.append("enable_bodies")

        class Stop(Exception):
            pass

        p = ChatGPTProvider()
        p.new_monitor = Monitor

        async def stop(*a, **k):
            raise Stop

        class Browser:
            async def get(self, url):
                return object()

        with mock.patch("providers.base.asyncio.sleep", new=stop):
            with self.assertRaises(Stop):
                await p.open_and_send(Browser(), "hi")
        self.assertEqual(calls, ["attach", "enable_bodies"])


# ---------------------------------------------------------------------------
# The words an image request types
# ---------------------------------------------------------------------------
class ImageWords(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(parse_size("1600x900"), (1600, 900))
        self.assertEqual(parse_size(" 1200 × 630 "), (1200, 630))
        self.assertIsNone(parse_size("square"))
        self.assertIsNone(parse_size(None))

    def test_size_instruction(self):
        self.assertEqual(size_instruction(None), "")
        self.assertEqual(size_instruction("auto"), "")
        self.assertEqual(size_instruction("portrait"), "Format: portrait orientation.")
        wide = size_instruction("1600x900")
        self.assertIn("exactly 1600x900 pixels", wide)
        self.assertIn("aspect ratio 16:9 (landscape)", wide)
        self.assertIn("1.91:1", size_instruction("1200x630"))
        self.assertIn("(square)", size_instruction("1024x1024"))
        self.assertIn("9:16 (portrait)", size_instruction("1080x1920"))
        # A ratio with no common name is written out exactly.
        self.assertIn("aspect ratio 7:5", size_instruction("1400x1000"))

    def test_chatgpt_names_the_tool_and_adds_no_style(self):
        typed = ChatGPTProvider().image_prompt("a teapot on a table", size="1600x900",
                                               background="transparent")
        self.assertTrue(typed.startswith("Use the image generation tool to create this image."))
        self.assertIn("a teapot on a table", typed)
        self.assertIn("1600x900", typed)
        self.assertIn("transparent", typed)
        # The design-with-chatgpt rule: no steering adjectives are added.
        for word in ("minimal", "clean", "subtle", "muted", "elegant", "modern",
                     "professional", "sleek", "editorial"):
            self.assertNotIn(word, typed.lower())

    def test_no_size_no_background_is_the_prompt_alone(self):
        self.assertEqual(GeminiProvider().image_prompt("  a fox  "), "a fox")


# ---------------------------------------------------------------------------
# What the image endpoint answers
# ---------------------------------------------------------------------------
class PixelSize(unittest.TestCase):
    def encoded(self, fmt, **kw):
        buf = io.BytesIO()
        Image.new("RGB", (321, 123), (10, 20, 30)).save(buf, fmt, **kw)
        return buf.getvalue()

    def test_formats(self):
        self.assertEqual(server._pixel_size(png_bytes(1254, 1254)), (1254, 1254))
        self.assertEqual(server._pixel_size(self.encoded("JPEG")), (321, 123))
        self.assertEqual(server._pixel_size(self.encoded("JPEG", progressive=True)), (321, 123))
        self.assertEqual(server._pixel_size(self.encoded("WEBP")), (321, 123))
        self.assertEqual(server._pixel_size(self.encoded("WEBP", lossless=True)), (321, 123))

    def test_unknown_is_none(self):
        self.assertIsNone(server._pixel_size(b"GIF89a...."))
        self.assertIsNone(server._pixel_size(b""))

    def test_payload_reports_the_original_size(self):
        b64 = base64.b64encode(png_bytes(1254, 836)).decode()
        out = server._image_payload(
            [{"b64": b64, "mime": "image/png", "path": "/tmp/x.png", "alt": "a teapot"}],
            None, {"model": "gpt-5-4-thinking"})
        d = out["data"][0]
        self.assertEqual((d["width"], d["height"], d["mime"]), (1254, 836, "image/png"))
        self.assertEqual(d["revised_prompt"], "a teapot")
        self.assertEqual(out["model_used"], {"model": "gpt-5-4-thinking"})


class ImageEndpoint(unittest.IsolatedAsyncioTestCase):
    async def run_request(self, provider, drive, **kw):
        with mock.patch.object(server, "drive_once", drive):
            return await server._run_image_request(
                provider, "a teapot", [], None, None, allow_local_paths=False, **kw)

    async def test_a_provider_outside_image_providers_gets_403(self):
        called = []

        async def drive(*a, **k):
            called.append(1)
            return "", []

        with mock.patch.object(server, "IMAGE_PROVIDERS", ["chatgpt-browser"]):
            with self.assertRaises(HTTPException) as caught:
                await self.run_request(GeminiProvider(), drive)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("chatgpt-browser", caught.exception.detail)
        self.assertEqual(called, [])

    async def test_counter_refusal_is_429_with_retry_after(self):
        async def drive(*a, **k):
            raise quota_refusal([{"feature_name": "image_gen", "remaining": 0,
                                  "reset_after": (datetime.now(timezone.utc)
                                                  + timedelta(hours=5)).isoformat()}])

        with mock.patch.object(server, "IMAGE_PROVIDERS", ["chatgpt-browser"]):
            with self.assertRaises(HTTPException) as caught:
                await self.run_request(ChatGPTProvider(), drive)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("image limit is reached", caught.exception.detail)
        secs = int(caught.exception.headers["Retry-After"])
        self.assertTrue(5 * 3600 - 60 < secs <= 5 * 3600 + 1, secs)

    async def test_refusal_text_with_no_image_is_429(self):
        async def drive(*a, **k):
            return ("You've hit the Plus plan limit for image generation requests. "
                    "You can create more images when the limit resets in 3 hours."), []

        with mock.patch.object(server, "IMAGE_PROVIDERS", ["chatgpt-browser"]):
            with self.assertRaises(HTTPException) as caught:
                await self.run_request(ChatGPTProvider(), drive)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("Plus plan limit", caught.exception.detail)
        self.assertIn("Retry-After", caught.exception.headers)

    async def test_other_text_with_no_image_is_502_quoting_the_page(self):
        async def drive(*a, **k):
            return "I can't make that image.", []

        with mock.patch.object(server, "IMAGE_PROVIDERS", ["chatgpt-browser"]):
            with self.assertRaises(HTTPException) as caught:
                await self.run_request(ChatGPTProvider(), drive)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("I can't make that image.", caught.exception.detail)

    async def test_typed_prompt_carries_size_and_the_request_its_options(self):
        seen = {}

        async def drive(provider, typed, specs, **k):
            seen.update(typed=typed, **k)
            k["meta"]["model_used"] = {"model": "gpt-5-4-thinking"}
            return "", [{"b64": base64.b64encode(png_bytes(1254, 1254)).decode(),
                         "mime": "image/png"}]

        with mock.patch.object(server, "IMAGE_PROVIDERS", ["chatgpt-browser"]):
            out = await self.run_request(ChatGPTProvider(), drive, size="1600x900",
                                         background="transparent", variant="instant")
        self.assertIn("1600x900", seen["typed"])
        self.assertIn("transparent", seen["typed"])
        self.assertEqual((seen["kind"], seen["variant"]), ("image", "instant"))
        self.assertEqual(out["data"][0]["width"], 1254)
        self.assertEqual(out["model_used"], {"model": "gpt-5-4-thinking"})


# ---------------------------------------------------------------------------
# gen_asset
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    reply = (200, {}, {})
    bodies = []

    def do_POST(self):
        self.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        status, headers, body = self.reply
        data = json.dumps(body).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class GenAssetRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}/v1/images/generations"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _Handler.bodies = []

    def test_429_is_exit_code_3_with_retry_after(self):
        _Handler.reply = (429, {"Retry-After": "3600"},
                          {"detail": "ChatGPT's image limit is reached ... resets at 01:00"})
        with mock.patch.object(gen_asset, "API", self.url):
            with self.assertRaises(gen_asset.GenAssetError) as caught:
                gen_asset.generate_raw("a teapot")
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(caught.exception.retry_after, "3600")
        self.assertIn("image limit is reached", str(caught.exception))

    def test_request_carries_model_size_background_and_variant(self):
        _Handler.reply = (200, {}, {"data": [{"b64_json": base64.b64encode(
            png_bytes(1254, 1254)).decode(), "mime": "image/png"}],
            "model_used": {"model": "gpt-5-4-thinking"}})
        with mock.patch.object(gen_asset, "API", self.url):
            raw, mime, item = gen_asset.generate_raw(
                "a teapot", size="1600x900", background="transparent", variant="instant")
        self.assertEqual(_Handler.bodies[0], {
            "prompt": "a teapot", "model": "chatgpt-browser", "size": "1600x900",
            "background": "transparent", "model_variant": "instant"})
        self.assertEqual((mime, item["model_used"]["model"]),
                         ("image/png", "gpt-5-4-thinking"))
        self.assertNotIn("b64_json", item)


class GenAssetRender(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="blm-test-plus-")

    def fake(self, fail_on=None, size=(1254, 1254)):
        calls = []

        def generate_raw(prompt, **kw):
            calls.append((prompt, kw))
            if fail_on and len(calls) >= fail_on:
                raise gen_asset.GenAssetError("limit reached", code=3, retry_after="60")
            return png_bytes(*size), "image/png", {"model_used": {"model": "gpt-5-4-thinking"}}
        return generate_raw, calls

    def test_exact_size_and_kept_original(self):
        fake, calls = self.fake()
        out = os.path.join(self.dir, "hero.png")
        with mock.patch.object(gen_asset, "generate_raw", fake):
            res = gen_asset.render_full("a teapot", out, width=1600, height=900,
                                        keep_original=True)
        self.assertEqual(calls[0][1]["size"], "1600x900")
        self.assertEqual(size_of(out), (1600, 900))
        self.assertEqual(res["size"], [1600, 900])
        self.assertEqual(res["original_size"], [1254, 1254])
        self.assertEqual(size_of(res["original"]), (1254, 1254))
        self.assertTrue(res["original"].endswith("hero.original.png"))
        self.assertEqual(res["model_used"], {"model": "gpt-5-4-thinking"})

    def test_knockout_asks_for_transparency(self):
        fake, calls = self.fake()
        with mock.patch.object(gen_asset, "generate_raw", fake):
            gen_asset.render_full("a logo", os.path.join(self.dir, "logo.png"),
                                  square_size=512, knockout=True)
        self.assertEqual(calls[0][1]["background"], "transparent")
        self.assertEqual(calls[0][1]["size"], "512x512")

    def test_variants_side_by_side(self):
        fake, calls = self.fake()
        out = os.path.join(self.dir, "card.png")
        with mock.patch.object(gen_asset, "generate_raw", fake):
            res = gen_asset.render_variants("a teapot", out, 3, width=800, height=600)
        self.assertEqual([os.path.basename(v["out"]) for v in res["variants"]],
                         ["card-v1.png", "card-v2.png", "card-v3.png"])
        self.assertTrue(os.path.isfile(res["sheet"]))
        self.assertIn("alternative 2 of 3", calls[1][0])
        self.assertTrue(calls[0][0].startswith("a teapot"))

    def test_a_limit_part_way_keeps_what_was_made(self):
        fake, _calls = self.fake(fail_on=3)
        out = os.path.join(self.dir, "card.png")
        with mock.patch.object(gen_asset, "generate_raw", fake):
            with self.assertRaises(gen_asset.GenAssetError) as caught:
                gen_asset.render_variants("a teapot", out, 4)
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(len(caught.exception.made), 2)
        self.assertTrue(os.path.isfile(caught.exception.sheet))


# ---------------------------------------------------------------------------
# The draft the composer restores after a failed send
# ---------------------------------------------------------------------------
class DraftComposer:
    """A composer that keeps a restored draft: insertText replaces the text
    only when the select-all script ran first, else appends to it."""

    def __init__(self, draft):
        self.text = draft
        self.selected = False


class DraftBox:
    def __init__(self, composer):
        self.c = composer

    async def apply(self, js):
        if "selectNodeContents" in js:
            self.c.selected = True


class DraftPage:
    def __init__(self, composer):
        self.c = composer
        self.sent = []

    async def send(self, cmd):
        msg = next(cmd)
        self.sent.append(msg)
        if msg["method"] == "Input.insertText":
            new = msg["params"]["text"]
            self.c.text = new if self.c.selected else self.c.text + new
            self.c.selected = False

    async def evaluate(self, expr, *a, **k):
        return self.c.text


class DraftIsReplaced(unittest.IsolatedAsyncioTestCase):
    async def test_a_restored_draft_is_replaced_not_appended(self):
        # 13 Oct 2026: a send that did not land left the prompt as a draft, and
        # the retry typed it again behind it, so the site got it twice.
        composer = DraftComposer("a teapot on a table")
        page = DraftPage(composer)
        with mock.patch("providers.chatgpt.asyncio.sleep", new=_no_sleep):
            await ChatGPTProvider().type_prompt(page, DraftBox(composer), "a teapot on a table")
        self.assertEqual(composer.text, "a teapot on a table")


_real_sleep = asyncio.sleep


async def _no_sleep(*_a, **_k):
    # The patch replaces asyncio.sleep itself, so call the saved original.
    await _real_sleep(0)


if __name__ == "__main__":
    unittest.main()
