"""Unit tests for the MCP stdio server — protocol framing and dispatch only.

No browser and no HTTP: the tool handlers are stubbed, so what's under test is
the JSON-RPC layer (handshake shape, tools/list contract, error mapping,
threading hand-off) rather than the upstream API.
"""
import json
import os
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp_server  # noqa: E402


class Captured:
    """Collects frames the server would have written to stdout."""

    def __init__(self):
        self.frames = []
        self._lock = threading.Lock()

    def __call__(self, msg):
        with self._lock:
            self.frames.append(msg)

    def by_id(self, rid):
        return next((f for f in self.frames if f.get("id") == rid), None)


class MCPProtocolTest(unittest.TestCase):
    def setUp(self):
        self.sent = Captured()
        patcher = mock.patch.object(mcp_server, "send", self.sent)
        patcher.start()
        self.addCleanup(patcher.stop)
        mcp_server._workers.clear()

    def drain(self):
        for t in list(mcp_server._workers):
            t.join(timeout=10)

    # -- handshake ---------------------------------------------------------

    def test_initialize_returns_protocol_and_tools_capability(self):
        mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}})
        r = self.sent.by_id(1)["result"]
        self.assertEqual(r["protocolVersion"], mcp_server.PROTOCOL_VERSION)
        self.assertIn("tools", r["capabilities"])
        self.assertEqual(r["serverInfo"]["name"], "browser-llm")

    def test_initialized_notification_gets_no_reply(self):
        mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(self.sent.frames, [])

    def test_ping_replies_empty(self):
        mcp_server.handle({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(self.sent.by_id(9)["result"], {})

    def test_unknown_method_is_a_jsonrpc_error(self):
        mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
        self.assertEqual(self.sent.by_id(4)["error"]["code"], -32601)

    def test_unknown_notification_is_ignored_not_errored(self):
        mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/whatever"})
        self.assertEqual(self.sent.frames, [])

    # -- tools/list --------------------------------------------------------

    def test_tools_list_shape(self):
        mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = self.sent.by_id(2)["result"]["tools"]
        self.assertEqual({t["name"] for t in tools},
                         {"ask", "generate_image", "list_models", "health"})
        for t in tools:
            self.assertTrue(t["description"], f"{t['name']} needs a description")
            self.assertEqual(t["inputSchema"]["type"], "object")
        # every advertised tool must actually be dispatchable
        self.assertEqual({t["name"] for t in tools}, set(mcp_server.HANDLERS))

    def test_required_args_are_declared(self):
        by_name = {t["name"]: t for t in mcp_server.TOOLS}
        self.assertEqual(by_name["ask"]["inputSchema"]["required"], ["prompt"])
        self.assertEqual(by_name["generate_image"]["inputSchema"]["required"],
                         ["prompt", "out"])

    def test_tools_list_is_json_serialisable(self):
        json.dumps(mcp_server.TOOLS)  # a client can only receive what encodes

    # -- tools/call --------------------------------------------------------

    def test_call_returns_text_content(self):
        with mock.patch.dict(mcp_server.HANDLERS, {"ask": lambda a: "hello " + a["prompt"]}):
            mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "ask", "arguments": {"prompt": "x"}}})
            self.drain()
        r = self.sent.by_id(3)["result"]
        self.assertFalse(r["isError"])
        self.assertEqual(r["content"][0], {"type": "text", "text": "hello x"})

    def test_handler_exception_becomes_isError_not_a_crash(self):
        def boom(_a):
            raise RuntimeError("upstream refused")

        with mock.patch.dict(mcp_server.HANDLERS, {"ask": boom}):
            mcp_server.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "ask", "arguments": {"prompt": "x"}}})
            self.drain()
        r = self.sent.by_id(5)["result"]
        self.assertTrue(r["isError"])
        self.assertIn("upstream refused", r["content"][0]["text"])

    def test_unknown_tool_is_an_error_frame(self):
        mcp_server.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                           "params": {"name": "nope", "arguments": {}}})
        self.drain()
        self.assertEqual(self.sent.by_id(6)["error"]["code"], -32602)

    def test_calls_run_off_the_read_loop(self):
        """A slow tool must not block the next frame — that's why ping stays
        answerable while an image generation is running."""
        started, release = threading.Event(), threading.Event()

        def slow(_a):
            started.set()
            release.wait(10)
            return "done"

        with mock.patch.dict(mcp_server.HANDLERS, {"ask": slow}):
            mcp_server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": "ask", "arguments": {"prompt": "x"}}})
            self.assertTrue(started.wait(5))
            mcp_server.handle({"jsonrpc": "2.0", "id": 8, "method": "ping"})
            self.assertIsNotNone(self.sent.by_id(8), "ping blocked behind a slow tool call")
            release.set()
            self.drain()
        self.assertEqual(self.sent.by_id(7)["result"]["content"][0]["text"], "done")

    def test_empty_prompt_is_rejected(self):
        with mock.patch.object(mcp_server, "_ask", side_effect=AssertionError("should not call")):
            mcp_server.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                               "params": {"name": "ask", "arguments": {"prompt": "   "}}})
            self.drain()
        r = self.sent.by_id(10)["result"]
        self.assertTrue(r["isError"])
        self.assertIn("prompt is required", r["content"][0]["text"])

    def test_ask_strip_fences_removes_only_fence_lines(self):
        with mock.patch.object(mcp_server, "_ask", return_value="```html\n<p>hi</p>\n```"):
            out = mcp_server.tool_ask({"prompt": "x", "strip_fences": True})
        self.assertEqual(out, "<p>hi</p>")


if __name__ == "__main__":
    unittest.main()
