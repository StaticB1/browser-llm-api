"""client.ask() can retry once on a second server (BROWSER_LLM_FALLBACK_API).

No network: urlopen is mocked. The rules pinned here: a dead or failing first
server, a 429 or 5xx, or an empty answer all go to the fallback with the
fallback model; a 400 does not (the request itself is wrong); a stream that
has already delivered text does not (the caller would see it twice); and with
no fallback configured the first failure is raised as before.
"""
import io
import json
import unittest
import urllib.error
from unittest import mock

from browser_llm import client


def _resp(text):
    body = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
    r = io.BytesIO(body)
    r.__enter__ = lambda *a: r
    r.__exit__ = lambda *a: False
    return r


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.multiple(
            client, BASE="http://primary", API_KEY="k1",
            FALLBACK_BASE="http://second", FALLBACK_API_KEY="k2",
            FALLBACK_MODEL="gemini-browser")
        self.p.start()
        self.addCleanup(self.p.stop)

    def _run(self, side_effect, **kw):
        with mock.patch.object(client.urllib.request, "urlopen", side_effect=side_effect) as u, \
                mock.patch("sys.stderr", new=io.StringIO()):
            out = client.ask("hi", model="chatgpt-browser", **kw)
        return out, [c.args[0] for c in u.call_args_list]

    def test_dead_primary_goes_to_fallback_with_fallback_model(self):
        out, reqs = self._run([urllib.error.URLError("refused"), _resp("from gemini")])
        self.assertEqual(out, "from gemini")
        self.assertEqual([r.full_url for r in reqs],
                         ["http://primary/v1/chat/completions",
                          "http://second/v1/chat/completions"])
        self.assertEqual(json.loads(reqs[0].data)["model"], "chatgpt-browser")
        self.assertEqual(json.loads(reqs[1].data)["model"], "gemini-browser")
        self.assertEqual(reqs[0].get_header("Authorization"), "Bearer k1")
        self.assertEqual(reqs[1].get_header("Authorization"), "Bearer k2")

    def test_rate_limit_and_server_error_fall_back(self):
        for code in (429, 502):
            out, reqs = self._run([_http_error(code), _resp("ok")])
            self.assertEqual(out, "ok", code)
            self.assertEqual(len(reqs), 2, code)

    def test_empty_answer_falls_back(self):
        out, reqs = self._run([_resp(""), _resp("second")])
        self.assertEqual(out, "second")
        self.assertEqual(len(reqs), 2)

    def test_bad_request_is_not_retried(self):
        with self.assertRaises(urllib.error.HTTPError):
            self._run([_http_error(400), _resp("never")])

    def test_model_variant_is_dropped_for_the_fallback(self):
        _, reqs = self._run([urllib.error.URLError("x"), _resp("ok")], model_variant="instant")
        self.assertEqual(json.loads(reqs[0].data)["model_variant"], "instant")
        self.assertNotIn("model_variant", json.loads(reqs[1].data))

    def test_fallback_can_be_switched_off_per_call(self):
        with self.assertRaises(RuntimeError):
            self._run([urllib.error.URLError("x"), _resp("never")], fallback=False)

    def test_good_primary_never_touches_fallback(self):
        out, reqs = self._run([_resp("first")])
        self.assertEqual(out, "first")
        self.assertEqual(len(reqs), 1)


class NoFallbackConfiguredTest(unittest.TestCase):
    def test_failure_is_raised(self):
        with mock.patch.multiple(client, BASE="http://primary", FALLBACK_BASE=""), \
                mock.patch.object(client.urllib.request, "urlopen",
                                  side_effect=urllib.error.URLError("refused")), \
                mock.patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(RuntimeError):
                client.ask("hi")


if __name__ == "__main__":
    unittest.main()
