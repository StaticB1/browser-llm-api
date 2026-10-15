"""Guest keys: who may use what, and the image allowance per 24 hours.

    ./venv/bin/python -m unittest tests.test_guests -v

No browser and no network: the drive is replaced by a stub, and the HTTP
checks go through FastAPI's TestClient, which runs the real key middleware.
"""
import base64
import io
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402
from PIL import Image  # noqa: E402

from browser_llm import authz, design, guests, server  # noqa: E402
from browser_llm.providers.chatgpt import ChatGPTProvider  # noqa: E402

KEY = "g" * 40
OWNER = "o" * 40
DAY = 24 * 3600


def png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class ParseTest(unittest.TestCase):
    def test_entries_are_read(self):
        g = guests.parse_guest_keys(f"sam:{KEY}:20, ann:{'a' * 20}:0")
        self.assertEqual(g[KEY], guests.Guest("sam", KEY, 20))
        self.assertEqual(g["a" * 20].images_per_day, 0)

    def test_bad_entries_are_skipped_not_fatal(self):
        raw = ",".join([f"sam:{KEY}", "x y:" + "k" * 20 + ":5", "short:abc:5",
                        "neg:" + "n" * 20 + ":-1", "word:" + "w" * 20 + ":ten",
                        f"first:{KEY}:3", f"reuse:{KEY}:4"])
        g = guests.parse_guest_keys(raw)
        self.assertEqual([v.name for v in g.values()], ["first"])

    def test_a_guest_cannot_hold_the_owners_key(self):
        self.assertEqual(guests.parse_guest_keys(f"sam:{OWNER}:20", OWNER), {})

    def test_unset_is_empty(self):
        self.assertEqual(guests.parse_guest_keys(None), {})
        self.assertEqual(guests.parse_guest_keys(""), {})


class ScopeTest(unittest.TestCase):
    def test_chat_images_models_and_status_are_allowed(self):
        for path in ("/v1/models", "/v1/chat/completions", "/v1/images/generations",
                     "/v1/images/edits", "/api/status", "/api/design/kinds"):
            self.assertTrue(authz.guest_may(path, "POST"), path)

    def test_history_gallery_and_deletes_are_not(self):
        for path in ("/api/history", "/api/conversation/abc", "/api/conversations/delete",
                     "/api/gallery", "/api/gallery/delete", "/api/anything-new",
                     "/v1/something-new"):
            self.assertFalse(authz.guest_may(path, "GET"), path)

    def test_pages_and_images_stay_public(self):
        for path in ("/", "/widget.js", "/images/chatgpt/x.png", "/assets/geist.woff2"):
            self.assertTrue(authz.guest_may(path, "GET"), path)


class QuotaTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "guest-images.json"
        self.q = guests.ImageQuota(self.path)
        self.g = guests.Guest("sam", KEY, 3)

    def tearDown(self):
        self.dir.cleanup()

    def fill(self, times):
        for t in times:
            self.q.commit(self.g, ["f.png"], self.q.reserve(self.g, now=t), now=t)

    def test_the_allowance_then_a_refusal_naming_when(self):
        self.fill([1000, 2000, 3000])
        with self.assertRaises(guests.QuotaExceeded) as caught:
            self.q.reserve(self.g, now=5000)
        # The oldest image leaves the window first: 1000 + 24 h.
        self.assertEqual(caught.exception.retry_after, 1000 + DAY - 5000)
        self.assertIn("3 images in any 24 hours", str(caught.exception))

    def test_the_window_rolls(self):
        self.fill([1000, 2000, 3000])
        self.q.reserve(self.g, now=1000 + DAY + 1)   # the first one has left

    def test_a_request_in_flight_holds_its_place(self):
        g = guests.Guest("one", KEY, 1)
        tok = self.q.reserve(g, now=3000)
        with self.assertRaises(guests.QuotaExceeded) as caught:
            self.q.reserve(g, now=3001)
        self.assertEqual(caught.exception.retry_after, 60)   # nothing saved yet to expire
        self.q.release(tok)                                    # it failed: the place is back
        self.q.reserve(g, now=3002)

    def test_with_one_in_flight_the_wait_is_the_oldest_images(self):
        self.fill([1000, 2000])
        self.q.reserve(self.g, now=3000)
        with self.assertRaises(guests.QuotaExceeded) as caught:
            self.q.reserve(self.g, now=3001)
        self.assertEqual(caught.exception.retry_after, 1000 + DAY - 3001)

    def test_retry_after_when_full_of_saved_images(self):
        g = guests.Guest("sam", KEY, 2)
        self.q.commit(g, ["a"], self.q.reserve(g, now=1000), now=1000)
        self.q.commit(g, ["b"], self.q.reserve(g, now=2000), now=2000)
        with self.assertRaises(guests.QuotaExceeded) as caught:
            self.q.reserve(g, now=3000)
        self.assertEqual(caught.exception.retry_after, 1000 + DAY - 3000)

    def test_the_count_survives_a_restart_and_old_entries_are_dropped(self):
        self.fill([1000, 2000])
        self.q.commit(self.g, ["late.png"], now=1000 + DAY + 5)
        again = guests.ImageQuota(self.path)
        self.assertEqual(again.snapshot(self.g, now=1000 + DAY + 6)["used"], 2)
        saved = json.loads(self.path.read_text())
        self.assertEqual([e[1] for e in saved["sam"]], ["f.png", "late.png"])

    def test_snapshot_reports_remaining_and_next_free(self):
        self.fill([1000, 2000, 3000])
        snap = self.q.snapshot(self.g, now=4000)
        self.assertEqual((snap["used"], snap["remaining"]), (3, 0))
        self.assertEqual(snap["next_free_at"], 1000 + DAY)

    def test_a_damaged_file_starts_empty_without_failing(self):
        self.path.write_text("{not json")
        self.assertEqual(guests.ImageQuota(self.path).snapshot(self.g)["used"], 0)

    def test_zero_allowance_is_a_words_only_key(self):
        with self.assertRaises(guests.QuotaExceeded) as caught:
            self.q.reserve(guests.Guest("none", KEY, 0), now=1)
        self.assertIsNone(caught.exception.retry_after)
        self.assertIn("may not make images", str(caught.exception))


class GuestImageRequests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.quota = guests.ImageQuota(Path(self.dir.name) / "g.json")
        self.guest = guests.Guest("sam", KEY, 2)
        self.calls = []
        self.patches = [mock.patch.object(server, "QUOTA", self.quota),
                        mock.patch.object(server, "IMAGE_PROVIDERS", ["chatgpt-browser"])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    async def run_request(self, drive, guest):
        with mock.patch.object(server, "drive_once", drive):
            return await server._run_image_request(
                ChatGPTProvider(), "a teapot", [], None, None,
                allow_local_paths=False, guest=guest)

    async def drive_image(self, *a, **k):
        self.calls.append(k)
        return "", [{"b64": png_b64(), "mime": "image/png", "path": "/x/chatgpt_1_ab.png"}]

    async def test_allowance_then_429_with_retry_after_and_no_drive(self):
        await self.run_request(self.drive_image, self.guest)
        await self.run_request(self.drive_image, self.guest)
        with self.assertRaises(HTTPException) as caught:
            await self.run_request(self.drive_image, self.guest)
        self.assertEqual(caught.exception.status_code, 429)
        self.assertIn("2 images in any 24 hours", caught.exception.detail)
        self.assertGreater(int(caught.exception.headers["Retry-After"]), DAY - 120)
        self.assertEqual(len(self.calls), 2)          # the third never reached the browser

    async def test_a_words_only_key_gets_403_not_a_wait(self):
        with self.assertRaises(HTTPException) as caught:
            await self.run_request(self.drive_image, guests.Guest("words", KEY, 0))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertFalse(caught.exception.headers)
        self.assertEqual(self.calls, [])

    async def test_a_guests_image_chat_is_deleted_after(self):
        await self.run_request(self.drive_image, self.guest)
        self.assertTrue(self.calls[0]["ephemeral"])

    async def test_the_owner_is_not_counted_and_keeps_the_chat(self):
        for _ in range(4):
            await self.run_request(self.drive_image, None)
        self.assertFalse(self.calls[0]["ephemeral"])
        self.assertEqual(self.quota.snapshot(self.guest)["used"], 0)

    async def test_a_failed_drive_gives_the_place_back(self):
        async def boom(*a, **k):
            raise RuntimeError("page fell over")
        with self.assertRaises(HTTPException):
            await self.run_request(boom, self.guest)
        self.assertEqual(self.quota.snapshot(self.guest)["used"], 0)

    async def test_no_image_gives_the_place_back(self):
        async def words(*a, **k):
            return "I can't make that image.", []
        with self.assertRaises(HTTPException) as caught:
            await self.run_request(words, self.guest)
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(self.quota.snapshot(self.guest)["used"], 0)


class GuestChat(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.quota = guests.ImageQuota(Path(self.dir.name) / "g.json")
        self.guest = guests.Guest("sam", KEY, 1)
        self.seen = {}

    def tearDown(self):
        self.dir.cleanup()

    async def chat(self, guest, imgs=()):
        async def drive(provider, prompt, specs, **k):
            self.seen.update(prompt=prompt, **k)
            return "hello", [dict(i) for i in imgs]
        req = server.ChatCompletionRequest(messages=[{"role": "user", "content": "draw a cat"}],
                                           ephemeral=False)
        request = SimpleNamespace(state=SimpleNamespace(guest=guest))
        with mock.patch.object(server, "drive_once", drive), \
                mock.patch.object(server, "QUOTA", self.quota), \
                mock.patch.object(server, "_client_may_send_paths", lambda r: False):
            return await server.chat_completions(req, request)

    async def test_a_guest_chat_is_always_deleted(self):
        await self.chat(self.guest)
        self.assertTrue(self.seen["ephemeral"])

    async def test_images_drawn_in_chat_count(self):
        await self.chat(self.guest, [{"b64": png_b64(), "mime": "image/png",
                                      "path": "/x/chatgpt_2_cd.png"}])
        self.assertEqual(self.quota.snapshot(self.guest)["used"], 1)

    async def test_at_the_allowance_chat_is_asked_for_words_only(self):
        self.quota.commit(self.guest, ["earlier.png"])
        await self.chat(self.guest)
        self.assertTrue(self.seen["prompt"].startswith(server._WORDS_ONLY))

    async def test_the_owner_chat_is_untouched(self):
        await self.chat(None)
        self.assertFalse(self.seen["ephemeral"])
        self.assertNotIn("Answer in words only", self.seen["prompt"])


class GuestHttp(unittest.TestCase):
    """The real middleware, through TestClient (its client address is not
    loopback, so this is how a machine on the LAN is treated)."""

    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from fastapi.testclient import TestClient
        cls.client = TestClient(server.app)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        g = guests.Guest("sam", KEY, 20)
        self.patches = [mock.patch.object(server, "API_KEY", OWNER),
                        mock.patch.object(server, "GUESTS", {KEY: g}),
                        mock.patch.object(server, "QUOTA",
                                          guests.ImageQuota(Path(self.dir.name) / "g.json"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def get(self, path, key=None):
        return self.client.get(path, headers={"Authorization": f"Bearer {key}"} if key else {})

    def test_guest_reaches_models_and_a_trimmed_status(self):
        self.assertEqual(self.get("/v1/models", KEY).status_code, 200)
        body = self.get("/api/status", KEY).json()
        self.assertEqual(body["guest"]["remaining"], 20)
        prov = body["providers"]["chatgpt-browser"]
        self.assertNotIn("recent", prov)
        self.assertNotIn("last_error", prov)
        self.assertNotIn("image_dir", body)

    def test_guest_is_refused_history_gallery_and_deletes(self):
        for path in ("/api/history", "/api/conversation/6ac7579c-5eac-83ec-9587-9ce0cb23d7dc",
                     "/api/gallery"):
            r = self.get(path, KEY)
            self.assertEqual(r.status_code, 403, path)
            self.assertEqual(r.json()["error"]["code"], "forbidden_for_guest_key")
        for path in ("/api/conversations/delete", "/api/gallery/delete"):
            r = self.client.post(path, json={"ids": ["x"], "files": ["x"]},
                                 headers={"X-Api-Key": KEY})
            self.assertEqual(r.status_code, 403, path)

    def test_owner_key_still_reaches_everything_and_no_key_is_401(self):
        self.assertIn("recent", self.get("/api/status", OWNER).json()["providers"]
                      ["chatgpt-browser"])
        self.assertEqual(self.get("/api/status").status_code, 401)
        self.assertEqual(self.get("/api/status", "wrong-" + KEY).status_code, 401)

    def test_guest_keys_alone_still_lock_the_lan_out(self):
        with mock.patch.object(server, "API_KEY", ""):
            self.assertEqual(self.get("/v1/models").status_code, 401)
            self.assertEqual(self.get("/v1/models", KEY).status_code, 200)


class PlanTest(unittest.TestCase):
    def test_a_guest_set_plans_against_its_own_allowance(self):
        status = {"guest": {"images_per_day": 20, "remaining": 3, "next_free_at": 0},
                  "providers": {"chatgpt-browser": {"image_quota": {"remaining": 100},
                                                    "image_reserve": 10}}}
        ok, msg = design.quota_plan(3, status, "chatgpt-browser")
        self.assertTrue(ok, msg)
        ok, msg = design.quota_plan(4, status, "chatgpt-browser")
        self.assertFalse(ok)
        self.assertIn("3 of 20", msg)


if __name__ == "__main__":
    unittest.main()
