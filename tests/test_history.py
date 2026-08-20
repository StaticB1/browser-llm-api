"""
Unit tests for the history/delete layer — no browser, no network.

These endpoints read someone's whole chat history and delete files, so the
pure decisions in front of them are the ones worth pinning down: which caller
is allowed to ask, which provider can answer, and which reference resolves to
a file this server is willing to unlink.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

import server  # noqa: E402
from providers.chatgpt import ChatGPTProvider  # noqa: E402


def req(origin=None, host="localhost:8081"):
    """A Request carrying just the headers the trust decision reads."""
    headers = []
    if host is not None:
        headers.append((b"host", host.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": "GET", "path": "/api/history",
                    "headers": headers, "query_string": b""})


class TrustedCallerTest(unittest.TestCase):
    def test_no_origin_is_trusted(self):
        # curl, client.py, the desktop app. A browser cannot omit Origin on a
        # cross-origin request, so its absence is not something a page can fake.
        self.assertTrue(server._trusted_caller(req()))

    def test_same_origin_is_trusted(self):
        self.assertTrue(server._trusted_caller(
            req("http://localhost:8081", "localhost:8081")))
        self.assertTrue(server._trusted_caller(
            req("https://192.168.1.34:8081", "192.168.1.34:8081")))

    def test_other_site_is_not(self):
        for origin in ("https://evil.example", "http://localhost:3000",
                       "http://localhost:8081.evil.example", "null"):
            self.assertFalse(server._trusted_caller(req(origin)), origin)

    def test_require_trusted_raises_403_naming_the_origin(self):
        with self.assertRaises(HTTPException) as caught:
            server._require_trusted(req("https://evil.example"), "reading history")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("evil.example", caught.exception.detail)
        self.assertIn("reading history", caught.exception.detail)

    def test_require_trusted_passes_quietly(self):
        self.assertIsNone(server._require_trusted(req(), "reading history"))


class HistoryProviderTest(unittest.TestCase):
    def test_chatgpt_answers(self):
        self.assertIs(type(server._history_provider("chatgpt-browser")),
                      ChatGPTProvider)

    def test_provider_without_a_readable_list_is_501(self):
        with self.assertRaises(HTTPException) as caught:
            server._history_provider("gemini-browser")
        self.assertEqual(caught.exception.status_code, 501)
        self.assertIn("conversation list", caught.exception.detail)

    def test_remote_model_points_at_the_other_install(self):
        # The history lives in a browser profile on the upstream box; proxying
        # the read would hand back that machine's chats under this model name.
        server.REMOTES["chatgpt-browser"] = "http://192.168.1.99:8081"
        try:
            with self.assertRaises(HTTPException) as caught:
                server._history_provider("chatgpt-browser")
        finally:
            server.REMOTES.pop("chatgpt-browser", None)
        self.assertEqual(caught.exception.status_code, 501)
        self.assertIn("192.168.1.99", caught.exception.detail)


class GalleryTargetTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name) / "images"
        (self.base / "chatgpt").mkdir(parents=True)
        (self.base / "chatgpt" / "a.png").write_bytes(b"x")
        self._saved = server._image_dir
        server._image_dir = self.base

    def tearDown(self):
        server._image_dir = self._saved
        self._tmp.cleanup()

    def resolve(self, ref):
        return server._gallery_target(ref)

    def test_accepted_reference_forms(self):
        want = (self.base / "chatgpt" / "a.png").resolve()
        for ref in ("chatgpt/a.png",
                    "/images/chatgpt/a.png",
                    "http://localhost:8081/images/chatgpt/a.png",
                    "http://192.168.1.34:8081/images/chatgpt/a.png?v=2",
                    "/images/chatgpt/a.png#top"):
            self.assertEqual(self.resolve(ref), want, ref)

    def test_traversal_is_refused(self):
        for ref in ("../secret.png", "chatgpt/../../secret.png",
                    "/images/../../etc/hosts.png", "chatgpt/../../../a.png"):
            self.assertIsNone(self.resolve(ref), ref)

    def test_an_absolute_path_cannot_leave_the_image_dir(self):
        # Leading slashes are stripped rather than honoured, so a reference that
        # names a real file elsewhere resolves to a nonexistent path *inside*
        # the image dir; the endpoint then reports it missing.
        base = self.base.resolve()
        for ref in ("/home/eben/.ssh/id_rsa.png", "file:///etc/shadow.png"):
            got = self.resolve(ref)
            self.assertIsNotNone(got, ref)
            self.assertTrue(got.is_relative_to(base), got)
            self.assertFalse(got.exists(), got)

    def test_encoded_traversal_is_refused(self):
        for ref in ("..%2Fsecret.png", "%2e%2e%2Fsecret.png",
                    "chatgpt/..%2f..%2fsecret.png",
                    "http://localhost:8081/images/..%2Fsecret.png"):
            self.assertIsNone(self.resolve(ref), ref)

    def test_only_image_extensions(self):
        (self.base / "chatgpt" / "notes.txt").write_bytes(b"x")
        self.assertIsNone(self.resolve("chatgpt/notes.txt"))
        self.assertIsNone(self.resolve("chatgpt/a.png.sh"))
        self.assertIsNone(self.resolve("chatgpt"))

    def test_empty_and_the_base_itself_are_refused(self):
        for ref in ("", "   ", "/images/", "/", None):
            self.assertIsNone(self.resolve(ref), repr(ref))

    def test_symlink_out_of_the_dir_is_refused(self):
        outside = Path(self._tmp.name) / "outside.png"
        outside.write_bytes(b"x")
        link = self.base / "chatgpt" / "escape.png"
        link.symlink_to(outside)
        self.assertIsNone(self.resolve("chatgpt/escape.png"))
        self.assertTrue(outside.exists())

    def test_a_missing_file_still_resolves(self):
        # The endpoint reports it as missing rather than refused; resolution is
        # about where the path points, not whether it is there right now.
        self.assertEqual(self.resolve("chatgpt/gone.png"),
                         (self.base / "chatgpt" / "gone.png").resolve())


class ConversationIdTest(unittest.TestCase):
    def test_only_a_uuid_shaped_id_is_sent_to_the_site(self):
        ok = "6c1f0f2e-9b3a-4f6d-8a21-0b7c5d9e1234"
        self.assertTrue(ChatGPTProvider._CONV_ID_RE.match(ok))
        for bad in ("", "../../etc/passwd", ok + "x", ok[:-1],
                    "6c1f0f2e-9b3a-4f6d-8a21-0b7c5d9e123g",
                    ok + "?is_visible=true", "<script>"):
            self.assertIsNone(ChatGPTProvider._CONV_ID_RE.match(bad), bad)


class BatchDeleteTest(unittest.TestCase):
    """One page call for the whole list, and nothing malformed reaches the site.

    Deleting one id per call cost ~4s each: a 70-chat clear-out ran for five
    minutes with the browser still waiting, which is what made it look like the
    account was never touched.
    """

    class FakePage:
        def __init__(self, reply):
            self.reply, self.seen = reply, []

        async def evaluate(self, js, **kw):
            self.seen.append(js)
            return self.reply

    def delete(self, page, ids):
        import asyncio
        return asyncio.run(ChatGPTProvider().delete_conversations(page, ids))

    def test_one_call_for_the_whole_list(self):
        ids = ["6c1f0f2e-9b3a-4f6d-8a21-0b7c5d9e%04d" % n for n in range(30)]
        page = self.FakePage(json.dumps({"ok": ids, "failed": {}}))
        done, failed = self.delete(page, ids)
        self.assertEqual(len(page.seen), 1)
        self.assertEqual(done, ids)
        self.assertEqual(failed, {})
        for conv_id in ids:
            self.assertIn(conv_id, page.seen[0])

    def test_malformed_ids_never_reach_the_page(self):
        ok = "6c1f0f2e-9b3a-4f6d-8a21-0b7c5d9e1234"
        page = self.FakePage(json.dumps({"ok": [ok], "failed": {}}))
        done, failed = self.delete(page, [ok, "../../etc/passwd", "", "  ", "<script>"])
        self.assertEqual(done, [ok])
        self.assertEqual(set(failed), {"../../etc/passwd", "<script>"})
        self.assertNotIn("passwd", page.seen[0])

    def test_a_partial_failure_is_reported_per_id(self):
        a = "6c1f0f2e-9b3a-4f6d-8a21-0b7c5d9e1234"
        b = "7d2f0f2e-9b3a-4f6d-8a21-0b7c5d9e5678"
        page = self.FakePage(json.dumps({"ok": [a], "failed": {b: "HTTP 429"}}))
        done, failed = self.delete(page, [a, b])
        self.assertEqual(done, [a])
        self.assertEqual(failed, {b: "HTTP 429"})

    def test_a_dead_session_raises_instead_of_reporting_success(self):
        page = self.FakePage(json.dumps({"ok": [], "failed": {},
                                         "error": "not signed in (no access token)"}))
        with self.assertRaises(RuntimeError):
            self.delete(page, ["6c1f0f2e-9b3a-4f6d-8a21-0b7c5d9e1234"])

    def test_nothing_to_do_makes_no_call(self):
        page = self.FakePage("{}")
        done, failed = self.delete(page, ["not-an-id"])
        self.assertEqual((done, failed), ([], {"not-an-id": "malformed id"}))
        self.assertEqual(page.seen, [])


class BulkLimitTest(unittest.TestCase):
    def test_the_cap_is_smaller_than_a_whole_account(self):
        # A slip in the dashboard should not turn into 2000 site calls.
        self.assertGreaterEqual(server._MAX_BULK_DELETE, 50)
        self.assertLessEqual(server._MAX_BULK_DELETE, 500)


if __name__ == "__main__":
    unittest.main()
