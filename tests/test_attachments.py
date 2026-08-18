"""
Unit tests for image INPUT (attachments) — no browser, no network.

Covers the pure layer that turns whatever a client sends (data: URL, bare
base64, http(s) URL, local path, multipart bytes) into files on disk for the
provider's file picker, plus multimodal message flattening and the local-path
policy that keeps a LAN client from reading files off this box.
"""
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402

import server  # noqa: E402
from server import (  # noqa: E402
    ChatCompletionRequest, ContentPart, ImageGenRequest, Message,
    _attachment_files, _build_prompt, _decode_data_spec, _image_payload,
    _client_may_send_paths, _inline_paths_for_remote, _message_pieces,
    _ref_specs, _sniff_ext,
    _spec_kind, _spec_to_data_url,
)

PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_1x1).decode()
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


class SniffTest(unittest.TestCase):
    def test_known_signatures(self):
        self.assertEqual(_sniff_ext(PNG_1x1), "png")
        self.assertEqual(_sniff_ext(JPEG), "jpg")
        self.assertEqual(_sniff_ext(GIF), "gif")
        self.assertEqual(_sniff_ext(WEBP), "webp")
        self.assertEqual(_sniff_ext(b"BM" + b"\x00" * 20), "bmp")

    def test_unknown_is_empty(self):
        self.assertEqual(_sniff_ext(b"not an image at all"), "")


class SpecKindTest(unittest.TestCase):
    def test_data_url(self):
        self.assertEqual(_spec_kind(PNG_DATA_URL), "data")
        self.assertEqual(_spec_kind("DATA:image/png;base64,AAAA"), "data")

    def test_http(self):
        self.assertEqual(_spec_kind("http://example.com/a.png"), "http")
        self.assertEqual(_spec_kind("https://example.com/a.png"), "http")

    def test_paths(self):
        self.assertEqual(_spec_kind("/tmp/a.png"), "path")
        self.assertEqual(_spec_kind("~/Pictures/a.png"), "path")
        self.assertEqual(_spec_kind("relative/a.png"), "path")
        self.assertEqual(_spec_kind("file:///tmp/a.png"), "path")

    def test_long_bare_base64_is_data(self):
        self.assertEqual(_spec_kind(base64.b64encode(PNG_1x1 * 40).decode()), "data")

    def test_short_bare_string_is_path(self):
        # too short to be a plausible image payload → treated as a path (and
        # then fails loudly as "file not found" rather than silently decoding)
        self.assertEqual(_spec_kind("AAAA"), "path")


class DecodeDataSpecTest(unittest.TestCase):
    def test_data_url_png(self):
        data, ext = _decode_data_spec(PNG_DATA_URL)
        self.assertEqual(data, PNG_1x1)
        self.assertEqual(ext, "png")

    def test_bare_base64_jpeg(self):
        data, ext = _decode_data_spec(base64.b64encode(JPEG).decode())
        self.assertEqual(data, JPEG)
        self.assertEqual(ext, "jpg")

    def test_whitespace_in_payload_is_tolerated(self):
        b64 = base64.b64encode(PNG_1x1).decode()
        spec = "data:image/png;base64," + b64[:8] + "\n  " + b64[8:]
        self.assertEqual(_decode_data_spec(spec)[0], PNG_1x1)

    def test_declared_mime_used_when_signature_unknown(self):
        _data, ext = _decode_data_spec(
            "data:image/webp;base64," + base64.b64encode(b"mystery bytes").decode())
        self.assertEqual(ext, "webp")

    def test_non_base64_data_url_rejected(self):
        with self.assertRaises(HTTPException) as cm:
            _decode_data_spec("data:image/png,notbase64")
        self.assertEqual(cm.exception.status_code, 400)

    def test_garbage_rejected(self):
        with self.assertRaises(HTTPException):
            _decode_data_spec("data:image/png;base64,")


class MessagePiecesTest(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(_message_pieces(Message(role="user", content="hi")), ("hi", []))

    def test_none_content(self):
        self.assertEqual(_message_pieces(Message(role="assistant", content=None)), ("", []))

    def test_openai_parts(self):
        m = Message(role="user", content=[
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL, "detail": "high"}},
        ])
        text, specs = _message_pieces(m)
        self.assertEqual(text, "what is this?")
        self.assertEqual(specs, [PNG_DATA_URL])

    def test_image_url_as_plain_string(self):
        m = Message(role="user", content=[
            {"type": "input_image", "image_url": "https://x/y.png"}])
        self.assertEqual(_message_pieces(m)[1], ["https://x/y.png"])

    def test_anthropic_source_block(self):
        m = Message(role="user", content=[
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(JPEG).decode()}}])
        specs = _message_pieces(m)[1]
        self.assertEqual(len(specs), 1)
        self.assertTrue(specs[0].startswith("data:image/jpeg;base64,"))

    def test_source_already_data_url_not_double_wrapped(self):
        m = Message(role="user", content=[
            {"type": "image", "source": {"type": "base64", "data": PNG_DATA_URL}}])
        self.assertEqual(_message_pieces(m)[1], [PNG_DATA_URL])

    def test_multiple_images_and_texts(self):
        m = Message(role="user", content=[
            {"type": "text", "text": "a"},
            {"type": "image_url", "image_url": {"url": "/tmp/1.png"}},
            {"type": "image_url", "image_url": {"url": "/tmp/2.png"}},
            {"type": "text", "text": "b"},
        ])
        text, specs = _message_pieces(m)
        self.assertEqual(text, "a\nb")
        self.assertEqual(specs, ["/tmp/1.png", "/tmp/2.png"])

    def test_image_part_without_payload_is_dropped(self):
        m = Message(role="user", content=[ContentPart(type="image_url")])
        self.assertEqual(_message_pieces(m), ("", []))


class BuildPromptTest(unittest.TestCase):
    def test_single_turn_with_image_is_not_annotated(self):
        prompt, specs = _build_prompt([Message(role="user", content=[
            {"type": "text", "text": "describe it"},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}}])])
        self.assertEqual(prompt, "describe it")
        self.assertEqual(specs, [PNG_DATA_URL])

    def test_multi_turn_annotates_the_turn_that_carried_images(self):
        prompt, specs = _build_prompt([
            Message(role="user", content=[
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "/tmp/a.png"}}]),
            Message(role="assistant", content="a cat"),
            Message(role="user", content="and now?"),
        ])
        self.assertIn("User: look [1 attached image]", prompt)
        self.assertIn("Assistant: a cat", prompt)
        self.assertIn("User: and now?", prompt)
        self.assertEqual(specs, ["/tmp/a.png"])

    def test_system_preamble_still_built(self):
        prompt, _ = _build_prompt([
            Message(role="system", content="be terse"),
            Message(role="user", content="hi")])
        self.assertTrue(prompt.startswith("[Context/Instructions: be terse]"))

    def test_over_limit_keeps_the_most_recent_images(self):
        parts = [{"type": "image_url", "image_url": {"url": f"/tmp/{i}.png"}}
                 for i in range(server._MAX_ATTACHMENTS + 3)]
        _prompt, specs = _build_prompt([Message(role="user", content=parts)])
        self.assertEqual(len(specs), server._MAX_ATTACHMENTS)
        self.assertEqual(specs[-1], f"/tmp/{server._MAX_ATTACHMENTS + 2}.png")


class RefSpecsTest(unittest.TestCase):
    def test_image_string_list_and_images(self):
        self.assertEqual(_ref_specs(ImageGenRequest(prompt="p", image="/a.png")), ["/a.png"])
        self.assertEqual(_ref_specs(ImageGenRequest(prompt="p", image=["/a.png", "/b.png"])),
                         ["/a.png", "/b.png"])
        self.assertEqual(_ref_specs(ImageGenRequest(prompt="p", image="/a.png",
                                                    images=["/b.png"])),
                         ["/a.png", "/b.png"])

    def test_blank_dropped(self):
        self.assertEqual(_ref_specs(ImageGenRequest(prompt="p", images=["  ", ""])), [])

    def test_chat_request_accepts_images_shorthand(self):
        req = ChatCompletionRequest(messages=[Message(role="user", content="hi")],
                                    images=[PNG_DATA_URL])
        self.assertEqual(req.images, [PNG_DATA_URL])


class MaterializeTest(unittest.IsolatedAsyncioTestCase):
    async def test_data_url_written_with_sniffed_extension_then_cleaned_up(self):
        async with _attachment_files([PNG_DATA_URL]) as paths:
            self.assertEqual(len(paths), 1)
            p = Path(paths[0])
            self.assertTrue(p.is_file())
            self.assertEqual(p.suffix, ".png")
            self.assertEqual(p.read_bytes(), PNG_1x1)
            tmpdir = p.parent
        self.assertFalse(tmpdir.exists(), "temp dir should be removed on exit")

    async def test_raw_uploads_keep_a_safe_name(self):
        async with _attachment_files(None, [("my photo!.jpg", JPEG)]) as paths:
            name = Path(paths[0]).name
            self.assertTrue(name.endswith(".jpg"), name)
            self.assertNotIn(" ", name)
            self.assertEqual(Path(paths[0]).read_bytes(), JPEG)

    async def test_local_path_passed_through_and_not_deleted(self):
        f = Path(self.enterContext(__import__("tempfile").TemporaryDirectory())) / "ref.png"
        f.write_bytes(PNG_1x1)
        async with _attachment_files([str(f)]) as paths:
            self.assertEqual(paths, [str(f.resolve())])
        self.assertTrue(f.is_file(), "caller's own file must survive cleanup")

    async def test_file_url_resolved(self):
        f = Path(self.enterContext(__import__("tempfile").TemporaryDirectory())) / "ref.png"
        f.write_bytes(PNG_1x1)
        async with _attachment_files([f.as_uri()]) as paths:
            self.assertEqual(paths, [str(f.resolve())])

    async def test_local_path_refused_for_non_loopback_clients(self):
        with self.assertRaises(HTTPException) as cm:
            async with _attachment_files(["/etc/hostname"], allow_local_paths=False):
                pass
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("same-origin", cm.exception.detail)
        self.assertIn("data:", cm.exception.detail)

    async def test_missing_file_is_a_clear_400(self):
        with self.assertRaises(HTTPException) as cm:
            async with _attachment_files(["/nope/does-not-exist.png"]):
                pass
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("not found", cm.exception.detail)

    async def test_too_many_attachments_rejected(self):
        specs = [PNG_DATA_URL] * (server._MAX_ATTACHMENTS + 1)
        with self.assertRaises(HTTPException) as cm:
            async with _attachment_files(specs):
                pass
        self.assertEqual(cm.exception.status_code, 400)

    async def test_oversize_attachment_rejected(self):
        orig = server._MAX_ATTACHMENT_BYTES
        server._MAX_ATTACHMENT_BYTES = 10
        try:
            with self.assertRaises(HTTPException) as cm:
                async with _attachment_files([PNG_DATA_URL]):
                    pass
            self.assertEqual(cm.exception.status_code, 413)
        finally:
            server._MAX_ATTACHMENT_BYTES = orig

    async def test_empty_and_blank_specs_ignored(self):
        async with _attachment_files(["", "   ", None]) as paths:
            self.assertEqual(paths, [])

    async def test_mixed_raw_and_specs_keep_order(self):
        async with _attachment_files([PNG_DATA_URL], [("a.jpg", JPEG)]) as paths:
            self.assertEqual([Path(p).suffix for p in paths], [".jpg", ".png"])


class RemoteInliningTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.f = self.dir / "ref.png"
        self.f.write_bytes(PNG_1x1)

    async def test_path_becomes_data_url(self):
        out = await _spec_to_data_url(str(self.f), allow_local_paths=True)
        self.assertTrue(out.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(out.split(",", 1)[1]), PNG_1x1)

    async def test_portable_specs_untouched(self):
        for spec in (PNG_DATA_URL, "https://example.com/a.png"):
            self.assertEqual(await _spec_to_data_url(spec, allow_local_paths=True), spec)

    async def test_payload_rewritten_in_every_shape(self):
        payload = {
            "images": [str(self.f)],
            "image": str(self.f),
            "messages": [
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": str(self.f)}},
                    {"type": "image_url", "image_url": str(self.f)},
                    {"type": "text", "text": "hi"},
                ]},
                {"role": "user", "content": "plain string content is fine"},
            ],
        }
        out = await _inline_paths_for_remote(payload, allow_local_paths=True)
        self.assertTrue(out["images"][0].startswith("data:"))
        self.assertTrue(out["image"].startswith("data:"))
        parts = out["messages"][0]["content"]
        self.assertTrue(parts[0]["image_url"]["url"].startswith("data:"))
        self.assertTrue(parts[1]["image_url"].startswith("data:"))
        self.assertEqual(parts[2]["text"], "hi")


class ImagePayloadTest(unittest.TestCase):
    def test_b64_and_saved_url(self):
        out = _image_payload([{"b64": "QQ==", "mime": "image/png",
                               "url": "http://h/images/x.png", "path": "/p/x.png"}], "b64_json")
        self.assertEqual(out["data"][0]["b64_json"], "QQ==")
        self.assertEqual(out["data"][0]["url"], "http://h/images/x.png")
        self.assertEqual(out["data"][0]["path"], "/p/x.png")

    def test_remote_only_image_uses_src(self):
        out = _image_payload([{"src": "https://x/y.png"}], "url")
        self.assertEqual(out["data"][0]["url"], "https://x/y.png")

    def test_data_url_fallback_when_unsaved(self):
        out = _image_payload([{"b64": "QQ==", "mime": "image/png"}], "url")
        self.assertEqual(out["data"][0]["url"], "data:image/png;base64,QQ==")


class _FakeRequest:
    """Just the two things _client_may_send_paths reads."""

    def __init__(self, client_host, headers=None):
        self.client = type("Client", (), {"host": client_host})() if client_host else None
        self.headers = headers or {}


class ClientMaySendPathsTest(unittest.TestCase):
    """Who may name a server-side file path. Loopback is not enough: a page on
    another site drives the operator's own browser, so the request arrives from
    127.0.0.1 (verified live — a cross-site Origin plus a local path got the file
    uploaded and described back before the origin check existed)."""

    def test_local_cli_or_desktop_app_allowed(self):
        self.assertTrue(_client_may_send_paths(_FakeRequest("127.0.0.1")))

    def test_local_web_ui_allowed(self):
        req = _FakeRequest("127.0.0.1", {"origin": "http://localhost:8081",
                                         "host": "localhost:8081"})
        self.assertTrue(_client_may_send_paths(req))

    def test_drive_by_from_another_site_refused(self):
        req = _FakeRequest("127.0.0.1", {"origin": "https://evil.example",
                                         "host": "localhost:8081"})
        self.assertFalse(_client_may_send_paths(req))

    def test_lan_client_refused(self):
        self.assertFalse(_client_may_send_paths(_FakeRequest("192.168.1.99")))

    def test_operator_opt_in_overrides(self):
        orig = server._ALLOW_REMOTE_FILE_PATHS
        server._ALLOW_REMOTE_FILE_PATHS = True
        try:
            self.assertTrue(_client_may_send_paths(_FakeRequest("192.168.1.99")))
        finally:
            server._ALLOW_REMOTE_FILE_PATHS = orig


if __name__ == "__main__":
    unittest.main()
