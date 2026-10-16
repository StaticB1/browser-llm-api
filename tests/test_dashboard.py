"""
Static checks on the dashboard (browser_llm/webui/index.html) — no browser.

The dashboard is one file with no build step, so the rules written in the
project notes are only as good as the next edit. These pin the ones that
fail quietly: the theme resolves before the first paint, colours stay tokens,
links in an answer cannot carry a script URL, the fence marker stays text,
the dashboard's own chats are never ephemeral, and every element the script
looks up by id exists.
"""
import re
import unittest
from pathlib import Path

WEBUI = Path(__file__).resolve().parent.parent / "browser_llm" / "webui"
HTML = (WEBUI / "index.html").read_text(encoding="utf-8")
CSS = HTML.split("<style>", 1)[1].split("</style>", 1)[0]
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]


class DashboardRulesTest(unittest.TestCase):
    def test_theme_is_resolved_before_the_stylesheet(self):
        head = HTML.split("</head>", 1)[0]
        self.assertLess(head.index("blm.theme"), head.index("<style>"))

    def test_colours_are_tokens_outside_the_two_theme_blocks(self):
        rules = re.sub(r":root\{.*?\n  \}", "", CSS, count=1, flags=re.S)
        rules = re.sub(r':root\[data-theme="dark"\]\{.*?\n  \}', "", rules, count=1, flags=re.S)
        rules = re.sub(r"/\*.*?\*/", "", rules, flags=re.S)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", rules), [])

    def test_hidden_beats_display_rules(self):
        self.assertIn("[hidden]{display:none!important}", CSS)

    def test_answer_links_and_images_go_through_safe_url(self):
        inline = SCRIPT.split("function inline(s){", 1)[1].split("\n}", 1)[0]
        self.assertNotIn('href="$2"', inline)
        self.assertNotIn('src="$2"', inline)
        self.assertEqual(inline.count("safeUrl("), 2)
        safe = SCRIPT.split("function safeUrl(u, img){", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("javascript", safe.lower())

    def test_fence_placeholder_is_plain_text(self):
        self.assertIn("@@BLMCODE", SCRIPT)
        self.assertNotIn("\x00", HTML)

    def test_the_dashboard_keeps_its_own_conversations(self):
        self.assertIn("ephemeral:false", SCRIPT)

    def test_every_id_the_script_looks_up_exists(self):
        ids = set(re.findall(r'id="([A-Za-z0-9_-]+)"', HTML))
        used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)', SCRIPT))
        self.assertEqual(sorted(used - ids), [])

    def test_every_turns_conversation_is_kept_for_the_delete(self):
        # Each turn is a new conversation on the site; the chat must remember
        # all of them, and the cascade must send all of them.
        self.assertIn("noteThread(cur, convId, model)", SCRIPT)
        self.assertNotIn("cur.conv = convId", SCRIPT)
        self.assertIn("for (const th of chatThreads(c))", SCRIPT)

    def test_fonts_come_from_the_package(self):
        self.assertIn('url("/assets/Geist-Variable.woff2")', CSS)
        self.assertNotIn("fonts.googleapis", HTML)


if __name__ == "__main__":
    unittest.main()
