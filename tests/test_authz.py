"""Tests for authz.py — API-key gating decisions + REMOTE_PROVIDERS parsing.

Pure stdlib, no browser, no server import (server.py has import-time side
effects like log truncation).  Run:  ./venv/bin/python -m unittest discover -s tests
"""
import unittest

import authz


class TestIsLoopback(unittest.TestCase):
    def test_loopback_addresses(self):
        for h in ("127.0.0.1", "127.0.0.2", "::1", "localhost", "::ffff:127.0.0.1"):
            self.assertTrue(authz.is_loopback(h), h)

    def test_remote_addresses(self):
        for h in ("192.168.1.50", "10.0.0.2", "::ffff:192.168.1.50", "8.8.8.8",
                  "", None, "1270.0.0.1"):
            self.assertFalse(authz.is_loopback(h), repr(h))


class TestNeedsKey(unittest.TestCase):
    def test_api_paths_protected(self):
        for p in ("/v1/chat/completions", "/v1/images/generations", "/v1/models",
                  "/api/status", "/api/gallery"):
            self.assertTrue(authz.needs_key(p, "POST"), p)
            self.assertTrue(authz.needs_key(p, "GET"), p)

    def test_pages_and_assets_public(self):
        for p in ("/", "/ui", "/version", "/widget.js", "/demo", "/widget-demo",
                  "/images/gemini/gemini_123_abc.png", "/favicon.ico"):
            self.assertFalse(authz.needs_key(p, "GET"), p)

    def test_options_preflight_always_passes(self):
        # CORS preflights cannot carry auth headers by spec.
        self.assertFalse(authz.needs_key("/v1/chat/completions", "OPTIONS"))

    def test_images_prefix_only(self):
        # nothing outside /images/ sneaks through the prefix rule
        self.assertTrue(authz.needs_key("/imagesX/evil", "GET"))


class TestExtractKey(unittest.TestCase):
    def test_bearer(self):
        self.assertEqual(authz.extract_key("Bearer abc123", None), "abc123")
        self.assertEqual(authz.extract_key("bearer abc123", None), "abc123")  # case-insensitive

    def test_x_api_key(self):
        self.assertEqual(authz.extract_key(None, "abc123"), "abc123")

    def test_bearer_wins_over_x_api_key(self):
        self.assertEqual(authz.extract_key("Bearer a", "b"), "a")

    def test_absent_or_malformed(self):
        self.assertEqual(authz.extract_key(None, None), "")
        self.assertEqual(authz.extract_key("Basic dXNlcg==", None), "")
        self.assertEqual(authz.extract_key("Bearer", None), "")  # no token


class TestKeyMatches(unittest.TestCase):
    def test_match(self):
        self.assertTrue(authz.key_matches("s3cret", "s3cret"))

    def test_mismatch_and_empty(self):
        self.assertFalse(authz.key_matches("wrong", "s3cret"))
        self.assertFalse(authz.key_matches("", "s3cret"))
        # empty configured key must never be matchable by an empty supplied key
        self.assertFalse(authz.key_matches("", ""))


class TestOriginIsTrusted(unittest.TestCase):
    """Guards the file-path attachment rule: a page on another site can make the
    operator's browser POST here, and it arrives over loopback like any local
    client. Verified live before this existed — `Origin: https://evil.example`
    plus a local path got the file uploaded and described back."""

    def test_no_origin_is_trusted(self):
        # curl, the CLI, the desktop app, a proxying server: only browsers set it.
        self.assertTrue(authz.origin_is_trusted(None, "localhost:8081"))
        self.assertTrue(authz.origin_is_trusted("", "localhost:8081"))
        self.assertTrue(authz.origin_is_trusted("   ", "localhost:8081"))

    def test_opaque_origin_is_not_trusted(self):
        # A page on any site can hand itself an opaque origin with
        # <iframe sandbox="allow-scripts" srcdoc>, so "null" is a cross-origin
        # caller wearing a disguise. Reproduced live from localhost:3000: the
        # sandboxed frame read the account's chat titles while the page's own
        # direct fetch was refused.
        for spelling in ("null", "NULL", " null "):
            self.assertFalse(authz.origin_is_trusted(spelling, "localhost:8081"),
                             spelling)
        self.assertFalse(authz.origin_is_trusted("null", None))

    def test_same_origin_trusted(self):
        self.assertTrue(authz.origin_is_trusted("http://localhost:8081", "localhost:8081"))
        self.assertTrue(authz.origin_is_trusted("http://127.0.0.1:8081", "127.0.0.1:8081"))
        self.assertTrue(authz.origin_is_trusted("http://192.168.1.34:8081", "192.168.1.34:8081"))

    def test_loopback_spellings_are_the_same_server(self):
        self.assertTrue(authz.origin_is_trusted("http://localhost:8081", "127.0.0.1:8081"))
        self.assertTrue(authz.origin_is_trusted("http://127.0.0.1:8081", "localhost:8081"))
        self.assertTrue(authz.origin_is_trusted("http://[::1]:8081", "localhost:8081"))

    def test_cross_site_origin_rejected(self):
        self.assertFalse(authz.origin_is_trusted("https://evil.example", "localhost:8081"))
        self.assertFalse(authz.origin_is_trusted("http://evil.example:8081", "localhost:8081"))
        # a page served from another host on the LAN (e.g. the embedded widget)
        self.assertFalse(authz.origin_is_trusted("http://192.168.1.99", "192.168.1.34:8081"))

    def test_port_mismatch_rejected(self):
        self.assertFalse(authz.origin_is_trusted("http://localhost:3000", "localhost:8081"))

    def test_missing_or_malformed_origin_host(self):
        self.assertFalse(authz.origin_is_trusted("not-a-url", "localhost:8081"))
        self.assertFalse(authz.origin_is_trusted("https://evil.example", None))

    def test_hostname_matches_when_host_header_omits_the_port(self):
        self.assertTrue(authz.origin_is_trusted("http://localhost", "localhost"))
        self.assertTrue(authz.origin_is_trusted("http://box.lan:8081", "box.lan"))


class TestSplitHostport(unittest.TestCase):
    def test_plain_and_ipv6(self):
        self.assertEqual(authz._split_hostport("localhost:8081"), ("localhost", "8081"))
        self.assertEqual(authz._split_hostport("localhost"), ("localhost", ""))
        self.assertEqual(authz._split_hostport("[::1]:8081"), ("[::1]", "8081"))
        self.assertEqual(authz._split_hostport("[::1]"), ("[::1]", ""))


class TestParseRemoteProviders(unittest.TestCase):
    def test_single(self):
        self.assertEqual(
            authz.parse_remote_providers("chatgpt-browser=http://192.168.1.34:8081"),
            {"chatgpt-browser": "http://192.168.1.34:8081"})

    def test_multiple_with_spaces_and_trailing_slash(self):
        self.assertEqual(
            authz.parse_remote_providers(
                " chatgpt-browser = http://a:8081/ , gemini-browser=https://b "),
            {"chatgpt-browser": "http://a:8081", "gemini-browser": "https://b"})

    def test_malformed_entries_skipped(self):
        self.assertEqual(
            authz.parse_remote_providers("nourl, =http://x, bad=ftp://x, ok=http://y"),
            {"ok": "http://y"})

    def test_empty(self):
        self.assertEqual(authz.parse_remote_providers(None), {})
        self.assertEqual(authz.parse_remote_providers(""), {})


if __name__ == "__main__":
    unittest.main()
