"""
Tests for what 2.0 added around the code: the .env file, the setting names of
2.0 with their pre-2.0 fallbacks, and the `browser-llm` command's dispatch.

No browser and no network (the health check talks to a closed port).

Run from the repo root:
    ./venv/bin/python -m unittest discover -s tests -v
"""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from browser_llm import cli, config


class ParseEnv(unittest.TestCase):
    def test_lines_quotes_comments_and_export(self):
        text = """
# a comment
BROWSER_LLM_PORT=8091
export BROWSER_LLM_HOST=0.0.0.0
DEFAULT_PROVIDER="gemini-browser"
CHATGPT_MODEL='GPT-6'
BROWSER_LLM_PUBLIC_URL=http://host:8081   # trailing comment
not a pair
=novalue
9BAD=x
"""
        self.assertEqual(config.parse_env(text), {
            "BROWSER_LLM_PORT": "8091", "BROWSER_LLM_HOST": "0.0.0.0",
            "DEFAULT_PROVIDER": "gemini-browser", "CHATGPT_MODEL": "GPT-6",
            "BROWSER_LLM_PUBLIC_URL": "http://host:8081"})

    def test_a_hash_inside_quotes_is_kept(self):
        self.assertEqual(config.parse_env('BROWSER_LLM_API_KEY="ab#cd"'),
                         {"BROWSER_LLM_API_KEY": "ab#cd"})


class LoadEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = Path(self.tmp.name) / "settings.env"
        self.env.write_text("BLM_TEST_A=from-file\nBLM_TEST_B=from-file\n")
        config._loaded = None

    def tearDown(self):
        config._loaded = None
        self.tmp.cleanup()

    def test_the_environment_wins_over_the_file(self):
        with mock.patch.dict(os.environ, {"BROWSER_LLM_ENV_FILE": str(self.env),
                                          "BLM_TEST_A": "from-shell"}, clear=False):
            os.environ.pop("BROWSER_LLM_NO_DOTENV", None)
            os.environ.pop("BLM_TEST_B", None)
            self.assertEqual(config.load_env(), str(self.env))
            self.assertEqual(os.environ["BLM_TEST_A"], "from-shell")
            self.assertEqual(os.environ["BLM_TEST_B"], "from-file")
        os.environ.pop("BLM_TEST_B", None)

    def test_it_can_be_switched_off(self):
        with mock.patch.dict(os.environ, {"BROWSER_LLM_ENV_FILE": str(self.env),
                                          "BROWSER_LLM_NO_DOTENV": "1"}, clear=False):
            os.environ.pop("BLM_TEST_B", None)
            self.assertIsNone(config.load_env())
            self.assertNotIn("BLM_TEST_B", os.environ)

    def test_a_missing_explicit_file_applies_nothing(self):
        with mock.patch.dict(os.environ, {"BROWSER_LLM_ENV_FILE": "/nonexistent/x.env"},
                             clear=False):
            os.environ.pop("BROWSER_LLM_NO_DOTENV", None)
            self.assertIsNone(config.load_env())


class SettingNames(unittest.TestCase):
    def test_new_name_first_then_old_names_with_one_warning(self):
        keys = ("BROWSER_LLM_IMAGE_DIR", "GEMINI_IMAGE_DIR", "IMAGE_DIR")
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in keys:
                os.environ.pop(k, None)
            config._warned.discard("GEMINI_IMAGE_DIR")
            self.assertEqual(config.env(*keys, default="/d"), "/d")
            os.environ["GEMINI_IMAGE_DIR"] = "/old"
            with self.assertLogs("browser_llm.config", level="WARNING") as logs:
                self.assertEqual(config.env(*keys, default="/d"), "/old")
            self.assertIn("pre-2.0 name of BROWSER_LLM_IMAGE_DIR", logs.output[0])
            os.environ["BROWSER_LLM_IMAGE_DIR"] = "/new"
            self.assertEqual(config.env(*keys, default="/d"), "/new")

    def test_empty_counts_as_unset(self):
        with mock.patch.dict(os.environ, {"BROWSER_LLM_PUBLIC_URL": "  "}, clear=False):
            os.environ.pop("GEMINI_PUBLIC_URL", None)
            self.assertEqual(config.env("BROWSER_LLM_PUBLIC_URL", "GEMINI_PUBLIC_URL",
                                        default="http://localhost:8081"),
                             "http://localhost:8081")


class Command(unittest.TestCase):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_help_lists_every_command(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        for name in cli.COMMANDS:
            self.assertIn(f"  {name}", out)

    def test_version(self):
        from browser_llm import __version__
        self.assertEqual(self.run_cli("version")[1].strip(), __version__)

    def test_unknown_command_is_exit_2(self):
        code, _, err = self.run_cli("frobnicate")
        self.assertEqual(code, 2)
        self.assertIn("unknown command", err)

    def test_design_kinds_runs_through_the_command(self):
        code, out, _ = self.run_cli("design", "kinds")
        self.assertEqual(code, 0)
        self.assertIn("mobile-screen", out)

    def test_health_says_unreachable_with_exit_1(self):
        code, _, err = self.run_cli("health", "http://127.0.0.1:9")
        self.assertEqual(code, 1)
        self.assertIn("unreachable", err)


if __name__ == "__main__":
    unittest.main()
