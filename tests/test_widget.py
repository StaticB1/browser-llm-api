"""
Static checks on the embeddable widget (browser_llm/webui/widget.js) — no
browser, no node.

The widget is configured by data-* attributes on someone else's page and by a
small JavaScript API, so its header comment is the only manual most hosts
read. These tests keep that comment honest and guard the two rendering rules
that are easy to lose in an edit: the fence placeholder stays a plain text
marker, and links in an answer cannot carry a script URL.
"""
import re
import unittest
from pathlib import Path

WEBUI = Path(__file__).resolve().parent.parent / "browser_llm" / "webui"
JS = (WEBUI / "widget.js").read_text(encoding="utf-8")
HEADER = JS.split("*/", 1)[0]


class WidgetHeaderTest(unittest.TestCase):
    def test_every_attribute_read_is_documented(self):
        read = set(re.findall(r'attr\("([a-z-]+)"', JS))
        self.assertTrue(read, "no attributes found; did attr() change name?")
        for name in read:
            self.assertIn("data-" + name, HEADER, name)

    def test_every_public_method_is_documented(self):
        api = JS.split("var api = {", 1)[1].split("\n  };", 1)[0]
        methods = set(re.findall(r"^    ([a-z]+): (?:function\b|[a-z]+,)", api, re.M))
        self.assertEqual(methods, {"open", "close", "reset", "ask", "on", "off", "config", "destroy"})
        for m in methods:
            self.assertIn(m + "(", HEADER, m)

    def test_events_named_in_the_header_are_the_ones_emitted(self):
        emitted = set(re.findall(r'emit\("([a-z]+)"', JS))
        self.assertEqual(emitted, {"ready", "open", "close", "message", "error"})
        for e in emitted:
            self.assertIn('"%s"' % e, HEADER, e)


class WidgetRenderingTest(unittest.TestCase):
    def test_fence_marker_is_plain_text(self):
        self.assertNotIn("\x00", JS)
        self.assertIn("@@BLMCODE", JS)

    def test_answer_links_go_through_the_url_filter(self):
        self.assertIn("function safeUrl(", JS)
        self.assertNotIn('<a href="$2"', JS)
        self.assertNotIn('<img src="$2"', JS)

    def test_fonts_come_from_the_server_that_served_the_widget(self):
        self.assertIn('cfg.base + \'/assets/Geist-Variable.woff2', JS.replace('"', "'"))
        self.assertTrue((WEBUI / "assets" / "Geist-Variable.woff2").is_file())


if __name__ == "__main__":
    unittest.main()
