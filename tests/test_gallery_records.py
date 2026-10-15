"""
Unit tests for what the 2.1 dashboard reads from the server — no browser.

The gallery pages through saved images and shows what made each one, the
Server view charts recent request times, and the UI's own fonts are served
without a key. Each of those is a small server promise worth pinning down,
and the record file sits next to an image that a delete must take with it.
"""
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.requests import Request  # noqa: E402

from browser_llm import authz, server  # noqa: E402

# a 1x1 PNG, so the record can measure a real image
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def trusted_request():
    return Request({"type": "http", "method": "POST", "path": "/api/gallery/delete",
                    "headers": [(b"host", b"localhost:8081")], "query_string": b""})


class ImageDirCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "images"
        (self.base / "chatgpt").mkdir(parents=True)
        self._saved = (server._image_dir, server._SAVE_ENABLED)
        server._image_dir = self.base
        server._SAVE_ENABLED = True

    def tearDown(self):
        server._image_dir, server._SAVE_ENABLED = self._saved
        self._tmp.cleanup()

    def image(self, name, age=0):
        p = self.base / "chatgpt" / name
        p.write_bytes(PNG_1PX)
        t = time.time() - age
        import os
        os.utime(p, (t, t))
        return p


class RecordTest(ImageDirCase):
    def test_record_is_written_next_to_the_image_and_measures_it(self):
        p = self.image("chatgpt_1_aa.png")
        server._write_record([{"path": str(p), "b64": base64.b64encode(PNG_1PX).decode()}],
                             {"brief": "a rocket", "kind": "app-icon"})
        rec = json.loads((self.base / "chatgpt" / "chatgpt_1_aa.png.json").read_text())
        self.assertEqual(rec["brief"], "a rocket")
        self.assertEqual(rec["kind"], "app-icon")
        self.assertEqual((rec["width"], rec["height"]), (1, 1))
        self.assertIn("created", rec)

    def test_an_image_that_was_not_saved_gets_no_record(self):
        server._write_record([{"src": "https://example.com/x.png"}], {"brief": "x"})
        self.assertEqual(list((self.base / "chatgpt").glob("*.json")), [])

    def test_a_broken_record_reads_as_none(self):
        p = self.image("chatgpt_2_bb.png")
        (self.base / "chatgpt" / "chatgpt_2_bb.png.json").write_text("{not json")
        self.assertIsNone(server._read_record(p))

    async def test_gallery_delete_takes_the_record_with_the_image(self):
        p = self.image("chatgpt_3_cc.png")
        server._write_record([{"path": str(p)}], {"brief": "x"})
        out = await server.api_gallery_delete(
            server.GalleryDeleteRequest(files=["/images/chatgpt/chatgpt_3_cc.png"]),
            trusted_request())
        self.assertEqual(out["deleted"], ["/images/chatgpt/chatgpt_3_cc.png"])
        self.assertEqual(list((self.base / "chatgpt").iterdir()), [])

    def test_a_conversation_sweep_takes_the_records_too(self):
        stamp = server._conv_stamp("6a1b2c3d-0000-4000-8000-1234567890ab")
        p = self.image(f"chatgpt_4_c{stamp}_dd.png")
        server._write_record([{"path": str(p)}], {"brief": "x"})
        for path in server._conversation_images(["6a1b2c3d-0000-4000-8000-1234567890ab"]):
            server._unlink_saved(path)
        self.assertEqual(list((self.base / "chatgpt").iterdir()), [])


class GalleryPagingTest(ImageDirCase):
    async def test_offset_pages_newest_first_and_says_when_more_remain(self):
        for i in range(5):
            self.image(f"chatgpt_{i}_x{i}.png", age=100 - i)   # x4 is the newest
        first = await server.api_gallery(limit=2)
        self.assertEqual([it["file"] for it in first["images"]], ["chatgpt_4_x4.png", "chatgpt_3_x3.png"])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["total"], 5)
        last = await server.api_gallery(limit=2, offset=4)
        self.assertEqual([it["file"] for it in last["images"]], ["chatgpt_0_x0.png"])
        self.assertFalse(last["has_more"])

    async def test_records_ride_along_and_never_appear_as_images(self):
        p = self.image("chatgpt_9_rec.png")
        server._write_record([{"path": str(p)}], {"brief": "the brief"})
        j = await server.api_gallery()
        self.assertEqual(len(j["images"]), 1)
        self.assertEqual(j["images"][0]["record"]["brief"], "the brief")


class RecentRequestsTest(unittest.IsolatedAsyncioTestCase):
    async def test_status_lists_recent_requests_oldest_first_and_bounded(self):
        name = next(iter(server.PROVIDERS))
        m = server._metrics[name]
        saved = list(m["recent"])
        m["recent"].clear()
        try:
            for i in range(server._RECENT_KEPT + 5):
                server._record_request(name, time.monotonic() - 1,
                                       RuntimeError("x") if i == 0 else None)
            recent = (await server.api_status())["providers"][name]["recent"]
            self.assertEqual(len(recent), server._RECENT_KEPT)
            self.assertTrue(all(r["ok"] for r in recent))      # the failure aged out
            self.assertTrue(all(set(r) == {"at", "s", "ok"} for r in recent))
        finally:
            m["recent"].clear()
            m["recent"].extend(saved)


class AssetsTest(unittest.TestCase):
    def test_fonts_ship_with_the_package_and_are_public(self):
        for f in ("Geist-Variable.woff2", "GeistMono-Variable.woff2", "Geist-OFL.txt"):
            self.assertTrue((server._ASSETS_DIR / f).is_file(), f)
        self.assertFalse(authz.needs_key("/assets/Geist-Variable.woff2", "GET"))
        self.assertTrue(authz.needs_key("/api/status", "GET"))


if __name__ == "__main__":
    unittest.main()
