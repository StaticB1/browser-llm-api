"""
Tests for the design upgrade (14 Oct 2026): the kinds and the words they add,
the set runner (references, resume, directions and pick, the quota check, a
limit hit part-way), tokens and review parsing, the server's `kind` fields,
gen_asset's kind options and upscale, and ChatGPT's "Create image" mode.

No browser and no network: gen_asset's generator, the server status and the
page are all stand-ins. Whether ChatGPT still offers "Create image" in its +
menu, and what the images look like, only a live run can tell (CLAUDE.md).

Run from the repo root:
    ./venv/bin/python -m unittest discover -s tests -v
"""
import base64
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402
from PIL import Image  # noqa: E402

from browser_llm import design  # noqa: E402
from browser_llm import gen_asset  # noqa: E402
from browser_llm import server  # noqa: E402
import browser_llm.providers.chatgpt as chatgpt  # noqa: E402
from browser_llm.providers.chatgpt import ChatGPTProvider  # noqa: E402

# The design-with-chatgpt skill's banned steering words, plus a few more style
# words: structural prompts must leave the look to ChatGPT.
STYLE_WORDS = ("minimal", "minimalist", "clean", "simple", "subtle", "muted",
               "understated", "low-contrast", "soft", "elegant", "refined", "tasteful",
               "sophisticated", "modern", "professional", "sleek", "editorial",
               "corporate", "polished", "premium", "luxury", "award", "cinematic",
               "stunning", "beautiful", "vibrant")


def load(path):
    with open(path) as f:
        return json.load(f)


def read(path):
    with open(path) as f:
        return f.read()


def png_bytes(w, h, color=(200, 120, 40), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, (w, h), color if mode == "RGB" else color + (255,)).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Kinds and prompts
# ---------------------------------------------------------------------------
class Compose(unittest.TestCase):
    def test_every_kind_composes_without_style_words(self):
        for name in design.KINDS:
            text = design.compose(name, "Roam, a travel app for quiet places",
                                  part={"name": "Hero", "content": "headline 'Go'"},
                                  index=1, total=2, ref_roles=["lock", "user"],
                                  theme="dark", platform="ios", copy_space="left")
            self.assertIn(design.KINDS[name]["what"], text, name)
            self.assertIn("Requirements:", text, name)
            for w in STYLE_WORDS:
                self.assertIsNone(re.search(r"\b%s\b" % re.escape(w), text.lower()),
                                  f"{name}: style word {w!r} in a structural prompt")

    def test_direction_adds_the_group_block(self):
        plain = design.compose("section", "x")
        directed = design.compose("section", "x", direction=True)
        self.assertNotIn("Art direction:", plain)
        self.assertIn("Art direction:\n" + design.DIRECTION["web"], directed)
        self.assertIn(design.DIRECTION["app"], design.compose("mobile-screen", "x",
                                                              direction=True))
        self.assertIn(design.DIRECTION["brand"], design.compose("icon", "x", direction=True))

    def test_text_policy_per_kind(self):
        self.assertIn("No text, letters", design.compose("hero-image", "harbour"))
        self.assertIn("No text, letters", design.compose("icon", "calendar"))
        self.assertIn("readable when the whole image is viewed at half size",
                      design.compose("section", "x"))
        self.assertIn("letter for letter", design.compose("diagram", "A -> B"))
        self.assertIn("in quotation marks", design.compose("og-image", "x"))

    def test_theme_platform_system_and_copy_space(self):
        t = design.compose("mobile-screen", "x", theme="light", platform="android",
                           system="Primary #0A2540, Inter")
        self.assertIn("light backgrounds with dark text", t)
        self.assertIn("Platform: Android", t)
        self.assertIn("Design system to follow exactly:\nPrimary #0A2540, Inter", t)
        self.assertIn("Platform: no particular one", design.compose("mobile-screen", "x"))
        self.assertNotIn("Platform:", design.compose("section", "x", platform="ios"))
        self.assertIn("Keep the left half calm",
                      design.compose("hero-image", "x", copy_space="left"))
        self.assertIn("on one side or across the lower third",
                      design.compose("hero-image", "x"))

    def test_references_say_what_each_image_is(self):
        t = design.compose("section", "x", ref_roles=["lock", "previous",
                                                      {"role": "user", "note": "our logo"}])
        self.assertIn("1. the first section of this same design", t)
        self.assertIn("2. the section just before this one", t)
        self.assertIn("3. a reference image from the user: our logo", t)
        self.assertIn("do not edit, extend or copy them", t)
        self.assertIn("Match the design references exactly", t)
        user_only = design.compose("icon", "x", ref_roles=["user"])
        self.assertIn("do not edit, extend or copy them", user_only)
        self.assertNotIn("Match the design references", user_only)
        screen = design.compose("mobile-screen", "x", ref_roles=["previous"])
        self.assertIn("the screen just before this one", screen)

    def test_alternative_and_unknown_kind(self):
        self.assertIn("This is direction 2 of 3.",
                      design.compose("section", "x", alternative=(2, 3)))
        with self.assertRaises(ValueError):
            design.compose("poster", "x")

    def test_catalog_and_defaults(self):
        cat = design.kinds_catalog()
        self.assertEqual([k["id"] for k in cat], list(design.KINDS))
        self.assertEqual(design.kind_defaults("og-image"),
                         {"size": "1200x630", "background": None, "exact": True})
        self.assertEqual(design.kind_defaults("icon")["background"], "transparent")
        for k in cat:
            self.assertRegex(k["size"], r"^\d+x\d+$")


class Parts(unittest.TestCase):
    def test_parse_parts(self):
        got = design.parse_parts(["Hero: headline 'Go'", {"name": "Pricing", "content": "2"},
                                  "", "Footer"])
        self.assertEqual(got, [{"name": "Hero", "content": "headline 'Go'"},
                               {"name": "Pricing", "content": "2"},
                               {"name": "Footer", "content": ""}])
        long = "a sentence with no name that runs on for quite a while, more than forty"
        self.assertEqual(design.parse_parts([long]), [{"name": "Part 1", "content": long}])

    def test_item_refs_order_and_cap(self):
        users = [{"path": f"u{i}.png", "note": ""} for i in range(6)]
        paths, roles = design.item_refs(3, lock="a.png", previous="b.png", user_refs=users)
        self.assertEqual(paths[:3], ["a.png", "b.png", "u0.png"])
        self.assertEqual(len(paths), 6)
        self.assertEqual(roles[:2], ["lock", "previous"])
        self.assertEqual(design.item_refs(2, lock="a.png", previous="a.png")[0], ["a.png"])

    def test_plan_references(self):
        spec = {"kind": "section", "brief": "b",
                "sections": ["Hero: h", "Steps: s", "Pricing: p"]}
        steps = design.plan(spec, "/o")
        self.assertEqual([os.path.basename(s["file"]) for s in steps],
                         ["01-hero.png", "02-steps.png", "03-pricing.png"])
        self.assertEqual([[os.path.basename(r) for r in s["refs"]] for s in steps],
                         [[], ["01-hero.png"], ["01-hero.png", "02-steps.png"]])
        locked = design.plan(dict(spec, lock="/x/approved.png"), "/o")
        self.assertEqual([[os.path.basename(r) for r in s["refs"]] for s in locked],
                         [["approved.png"], ["approved.png", "01-hero.png"],
                          ["approved.png", "02-steps.png"]])
        self.assertIn("an approved design for this same product", locked[0]["prompt"])
        dirs = design.plan(spec, "/o", directions=3)
        self.assertEqual([s["step"] for s in dirs][:3], ["direction"] * 3)
        self.assertTrue(dirs[0]["file"].endswith("directions/01-hero-v1.png"))


class Quota(unittest.TestCase):
    def status(self, remaining, reserve=10):
        prov = {"image_reserve": reserve,
                "image_quota": {"remaining": remaining,
                                "reset_after": "2026-10-15T03:44:09+00:00"}
                if remaining is not None else None}
        return {"providers": {"chatgpt-browser": prov}}

    def test_fits_short_and_unknown(self):
        ok, msg = design.quota_plan(5, self.status(120), "chatgpt-browser")
        self.assertTrue(ok)
        self.assertIn("120 left", msg)
        ok, msg = design.quota_plan(8, self.status(15), "chatgpt-browser")
        self.assertFalse(ok)
        self.assertIn("only 5 usable", msg)
        self.assertIn("--partial", msg)
        self.assertTrue(design.quota_plan(50, self.status(None), "chatgpt-browser")[0])


# ---------------------------------------------------------------------------
# The set runner, with a stand-in generator
# ---------------------------------------------------------------------------
class FakeLimit(Exception):
    code = 3


class FakeGen:
    DEFAULT_MODEL = "chatgpt-browser"

    def __init__(self, fail_on=None, size=(800, 450)):
        self.calls = []
        self.fail_on = fail_on
        self.size = size

    def render_full(self, prompt, out, refs=None, **kw):
        self.calls.append({"prompt": prompt, "out": out, "refs": list(refs or []), **kw})
        if self.fail_on and len(self.calls) == self.fail_on:
            raise FakeLimit("You've hit the Plus plan limit for image generation")
        for r in refs or []:
            assert os.path.isfile(r), f"reference {r} does not exist yet"
        shade = 40 * len(self.calls) % 255
        Image.new("RGB", self.size, (shade, 90, 160)).save(out)
        return {"out": out, "original": out, "original_size": list(self.size),
                "size": list(self.size), "model_used": {"model": "gpt-image"},
                "prompt_sent": "typed: " + prompt[:20], "image_tool": True}

    def contact_sheet(self, paths, out):
        Image.new("RGB", (100, 50)).save(out)
        return out


class RunSet(unittest.TestCase):
    SPEC = {"kind": "section", "brief": "Roam",
            "sections": ["Hero: h", "Steps: s", "Pricing: p"]}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "set")
        self.gen = FakeGen()
        self.status = {"providers": {"chatgpt-browser": {
            "image_reserve": 10, "image_quota": {"remaining": 120}}}}
        self.p = [mock.patch.object(design, "_gen_asset", lambda: self.gen),
                  mock.patch.object(design, "server_status", lambda: self.status)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        self.tmp.cleanup()

    def run_(self, spec=None, **kw):
        return design.run_set(spec or self.SPEC, self.out, log=lambda m: None, **kw)

    def refs(self):
        return [[os.path.basename(r) for r in c["refs"]] for c in self.gen.calls]

    def test_makes_each_item_with_the_set_references(self):
        res = self.run_()
        self.assertEqual(res["status"], "done")
        self.assertEqual(self.refs(), [[], ["01-hero.png"], ["01-hero.png", "02-steps.png"]])
        m = load(res["manifest"])
        self.assertEqual([it["file"] for it in m["items"]],
                         ["01-hero.png", "02-steps.png", "03-pricing.png"])
        self.assertTrue(all(it["image_tool"] for it in m["items"]))
        self.assertEqual(m["items"][1]["refs"], ["01-hero.png"])
        with Image.open(res["preview"]) as pv:
            self.assertEqual(pv.size, (1280, 3 * 720))      # stacked like a page
        self.assertEqual(self.gen.calls[0]["size"], "1600x900")

    def test_resume_skips_finished_items_and_redo_forces_one(self):
        self.run_()
        self.gen.calls.clear()
        self.assertEqual(self.run_()["made"], 0)
        changed = dict(self.SPEC, sections=["Hero: h", "Steps: s, now four", "Pricing: p"])
        self.run_(changed)
        self.assertEqual([os.path.basename(c["out"]) for c in self.gen.calls],
                         ["02-steps.png"])
        self.gen.calls.clear()
        self.run_(changed, redo=["3"])
        self.assertEqual([os.path.basename(c["out"]) for c in self.gen.calls],
                         ["03-pricing.png"])

    def test_refuses_when_the_set_does_not_fit_unless_partial(self):
        self.status["providers"]["chatgpt-browser"]["image_quota"]["remaining"] = 11
        with self.assertRaises(design.DesignError) as caught:
            self.run_()
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(self.gen.calls, [])
        self.assertEqual(self.run_(partial=True)["made"], 3)

    def test_a_limit_part_way_keeps_progress_and_resumes(self):
        self.gen.fail_on = 2
        with self.assertRaises(design.DesignError) as caught:
            self.run_()
        self.assertEqual(caught.exception.code, 3)
        m = load(os.path.join(self.out, "manifest.json"))
        self.assertEqual([it["index"] for it in m["items"]], [1])
        self.gen.fail_on, self.gen.calls = None, []
        self.run_()
        self.assertEqual([os.path.basename(c["out"]) for c in self.gen.calls],
                         ["02-steps.png", "03-pricing.png"])

    def test_directions_stop_for_a_pick_then_continue_from_it(self):
        res = self.run_(directions=3)
        self.assertEqual(res["status"], "pick")
        self.assertEqual(len(res["directions"]), 3)
        self.assertTrue(os.path.isfile(res["sheet"]))
        self.assertIn("This is direction 2 of 3.", self.gen.calls[1]["prompt"])
        self.gen.calls.clear()
        res = self.run_(directions=3, pick=2)
        self.assertEqual(res["status"], "done")
        self.assertEqual(self.refs(), [["01-hero.png"], ["01-hero.png", "02-steps.png"]])
        with open(os.path.join(self.out, "01-hero.png"), "rb") as a, \
                open(os.path.join(self.out, "directions", "01-hero-v2.png"), "rb") as b:
            self.assertEqual(a.read(), b.read())
        m = load(res["manifest"])
        self.assertEqual(m["items"][0]["from_direction"], 2)
        self.assertEqual(m["directions"]["picked"], 2)
        self.gen.calls.clear()
        self.assertEqual(self.run_(directions=3, pick=2)["made"], 0)

    def test_dry_run_spends_nothing(self):
        res = self.run_(dry_run=True, directions=2)
        self.assertEqual(res["status"], "dry-run")
        self.assertEqual(self.gen.calls, [])
        self.assertEqual([s["state"] for s in res["steps"]],
                         ["make", "make", "after --pick", "after --pick", "after --pick"])
        self.assertFalse(os.path.exists(self.out))

    def test_screens_preview_side_by_side_and_icons_ask_for_transparency(self):
        self.gen.size = (400, 500)
        res = self.run_({"kind": "mobile-screen", "brief": "b",
                         "sections": ["Home: h", "Detail: d"]})
        with Image.open(res["preview"]) as pv:
            self.assertGreater(pv.width, pv.height)
        self.gen.calls.clear()
        self.out = os.path.join(self.tmp.name, "icons")
        self.run_({"kind": "icon", "brief": "b", "sections": ["Home: h"]})
        self.assertTrue(self.gen.calls[0]["knockout"])


# ---------------------------------------------------------------------------
# Tokens and review
# ---------------------------------------------------------------------------
TOKENS = {
    "name": "Roam", "theme": "dark",
    "colors": [{"name": "Ink", "hex": "#0B0F14", "role": "page background"},
               {"name": "Sea Glass", "hex": "#7FD1C1", "role": "accent"}],
    "fonts": [{"role": "display", "family_guess": "Fraunces", "fallback": "serif",
               "weights": [500]}],
    "type_scale": [{"name": "h1", "font_role": "display", "size_px": 88,
                    "line_height": 1.0, "weight": 500, "letter_spacing_em": -0.03}],
    "spacing_px": [8, 16, 32], "radii_px": {"button": 999},
    "shadows": [{"name": "card", "value": "0 8px 24px rgba(0,0,0,.3)"}],
    "components": [{"name": "primary button", "spec": "52px tall"}],
    "sections": [{"image": 1, "name": "hero", "layout": "wordmark over photo"}],
    "rules": ["One accent per section."],
}


class TokensAndReview(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(design.extract_json('x\n```json\n{"a": 1}\n```\ny'), {"a": 1})
        self.assertEqual(design.extract_json('Here: {"a": {"b": 2}} done'), {"a": {"b": 2}})
        with self.assertRaises(ValueError):
            design.extract_json("no json here")

    def test_tokens_css(self):
        css = design.tokens_css(TOKENS)
        self.assertIn("--color-ink: #0B0F14;", css)
        self.assertIn("--color-sea-glass: #7FD1C1;", css)
        self.assertIn('--font-display: "Fraunces", serif;', css)
        self.assertIn("--text-h1-size: 88px;", css)
        self.assertIn("--text-h1-tracking: -0.03em;", css)
        self.assertIn("--space-3: 32px;", css)
        self.assertIn("--radius-button: 999px;", css)
        self.assertTrue(css.startswith(":root {") and css.rstrip().endswith("}"))

    def test_design_md(self):
        md = design.design_md(dict(TOKENS, measured_palette=[{"hex": "#0B0F14",
                                                              "share": 61.2}]),
                              ["/x/01-hero.png"])
        self.assertIn("# Roam", md)
        self.assertIn("| `--color-ink` | #0B0F14 | page background |", md)
        self.assertIn("#0B0F14 (61.2%)", md)
        self.assertIn("`01-hero.png`", md)
        self.assertIn("## Components", md)

    def test_measured_palette(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.png")
            im = Image.new("RGB", (200, 100), (10, 20, 30))
            im.paste((240, 200, 60), (0, 0, 50, 100))
            im.save(p)
            pal = design.measured_palette([p])
        self.assertEqual(pal[0]["hex"], "#0A141E")
        self.assertAlmostEqual(pal[0]["share"], 75.0, delta=2)
        self.assertEqual(pal[1]["hex"], "#F0C83C")

    def test_extract_tokens_writes_three_files(self):
        answer = "```json\n" + json.dumps(TOKENS) + "\n```"
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "01-hero.png")
            Image.new("RGB", (64, 64), (11, 15, 20)).save(img)
            with mock.patch.object(design, "_ask", return_value=answer) as ask:
                res = design.extract_tokens([img], d, log=lambda m: None)
            prompt = ask.call_args[0][0]
            self.assertIn("#0B0F14", prompt)              # measured colours go along
            for p in res["files"].values():
                self.assertTrue(os.path.isfile(p))
            self.assertEqual(load(res["files"]["json"])["source_images"],
                             ["01-hero.png"])

    def test_unparseable_answer_is_saved(self):
        with tempfile.TemporaryDirectory() as d:
            img = os.path.join(d, "a.png")
            Image.new("RGB", (8, 8)).save(img)
            with mock.patch.object(design, "_ask", return_value="sorry"):
                with self.assertRaises(design.DesignError):
                    design.extract_tokens([img], d, log=lambda m: None)
            self.assertTrue(os.path.isfile(os.path.join(d, "tokens.raw.md")))

    def test_review_and_critique(self):
        r = {"match_score": 72, "summary": "close",
             "differences": [{"severity": "high", "area": "hero", "design": "88px",
                              "build": "64px", "fix": "font-size: 88px"}],
             "matches": ["palette"]}
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "design.png"), os.path.join(d, "build.png")
            for p in (a, b):
                Image.new("RGB", (8, 8)).save(p)
            with mock.patch.object(design, "_ask", return_value=json.dumps(r)) as ask:
                res = design.review(design=a, build=b, width=1440, log=lambda m: None)
            self.assertIn("at 1440 px wide", ask.call_args[0][0])
            self.assertEqual(ask.call_args[0][1], [a, b])
            md = read(res["files"]["md"])
            self.assertIn("Match: 72 of 100.", md)
            self.assertIn("font-size: 88px", md)
            c = {"score": 55, "issues": [{"severity": "low", "area": "nav",
                                          "problem": "tight", "fix": "gap: 32px"}]}
            with mock.patch.object(design, "_ask", return_value=json.dumps(c)):
                res = design.review(critique=b, log=lambda m: None)
            self.assertIn("Score: 55 of 100.", read(res["files"]["md"]))
        with self.assertRaises(design.DesignError):
            design.review(design="x.png", log=lambda m: None)


# ---------------------------------------------------------------------------
# The server's kind fields
# ---------------------------------------------------------------------------
class ServerKinds(unittest.IsolatedAsyncioTestCase):
    async def test_kind_composes_and_brings_its_size_and_background(self):
        seen = {}

        async def run(provider, prompt, specs, raw, fmt, **kw):
            seen.update(prompt=prompt, **kw)
            return {"data": []}

        req = server.ImageGenRequest(prompt="a calendar with a check", kind="icon",
                                     model="chatgpt-browser")
        with mock.patch.object(server, "_run_image_request", run), \
                mock.patch.object(server, "_client_may_send_paths", lambda r: False):
            await server.images_generations(req, request=None)
        self.assertIn("Create ONE icon.", seen["prompt"])
        self.assertIn("Brief: a calendar with a check", seen["prompt"])
        self.assertEqual((seen["size"], seen["background"]), ("1024x1024", "transparent"))

    async def test_no_kind_leaves_the_prompt_alone(self):
        p, s, b = server._design_prompt("a fox", {}, 0, "1600x900", None)
        self.assertEqual((p, s, b), ("a fox", "1600x900", None))

    async def test_unknown_kind_is_400(self):
        with self.assertRaises(HTTPException) as caught:
            server._design_prompt("x", {"kind": "poster"}, 0, None, None)
        self.assertEqual(caught.exception.status_code, 400)

    async def test_response_carries_prompt_sent_and_image_tool(self):
        async def drive(provider, typed, specs, **k):
            k["meta"]["image_tool"] = True
            return "", [{"b64": base64.b64encode(png_bytes(64, 64)).decode(),
                         "mime": "image/png"}]

        with mock.patch.object(server, "drive_once", drive), \
                mock.patch.object(server, "IMAGE_PROVIDERS", ["chatgpt-browser"]):
            out = await server._run_image_request(ChatGPTProvider(), "a fox", [], None, None,
                                                  allow_local_paths=False, size="1024x1024")
        self.assertTrue(out["prompt_sent"].startswith("Use the image generation tool"))
        self.assertIn("a fox", out["prompt_sent"])
        self.assertIs(out["image_tool"], True)

    async def test_kinds_endpoint(self):
        out = await server.api_design_kinds()
        self.assertEqual(len(out["kinds"]), len(design.KINDS))
        self.assertEqual(out["direction_groups"], ["app", "brand", "content", "web"])


# ---------------------------------------------------------------------------
# gen_asset with a kind
# ---------------------------------------------------------------------------
class GenAssetKinds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, w, h, mode="RGB"):
        def generate_raw(prompt, **kw):
            self.seen.append(dict(prompt=prompt, **kw))
            return png_bytes(w, h, mode=mode), "image/png", {"prompt_sent": "typed",
                                                             "image_tool": True}
        return generate_raw

    def test_icon_kind_composes_and_asks_for_transparency(self):
        out = os.path.join(self.tmp.name, "i.png")
        with mock.patch.object(gen_asset, "generate_raw", self.fake(1024, 1024, "RGBA")):
            res = gen_asset.render_full("a calendar", out, kind="icon", square_size=256)
        s = self.seen[0]
        self.assertIn("Create ONE icon.", s["prompt"])
        self.assertEqual(s["background"], "transparent")
        self.assertEqual(s["size"], "256x256")           # the caller's size wins
        self.assertEqual(res["size"], [256, 256])
        self.assertEqual((res["prompt_sent"], res["image_tool"]), ("typed", True))

    def test_og_image_crops_to_exact_pixels_by_default(self):
        out = os.path.join(self.tmp.name, "og.jpg")
        with mock.patch.object(gen_asset, "generate_raw", self.fake(1731, 909)):
            res = gen_asset.render_full("Roam", out, kind="og-image")
        self.assertEqual(self.seen[0]["size"], "1200x630")
        self.assertEqual(res["size"], [1200, 630])

    def test_jpg_never_asks_for_transparency(self):
        out = os.path.join(self.tmp.name, "i.jpg")
        with mock.patch.object(gen_asset, "generate_raw", self.fake(1024, 1024)):
            gen_asset.render_full("a calendar", out, kind="icon")
        self.assertIsNone(self.seen[0]["background"])

    def test_variants_with_a_kind_use_its_direction_wording(self):
        out = os.path.join(self.tmp.name, "hero.png")
        with mock.patch.object(gen_asset, "generate_raw", self.fake(400, 225)):
            gen_asset.render_variants("harbour", out, 2, kind="hero-image")
        self.assertIn("This is direction 1 of 2.", self.seen[0]["prompt"])
        self.assertIn("This is direction 2 of 2.", self.seen[1]["prompt"])
        self.assertNotIn("alternative", self.seen[0]["prompt"])

    def test_upscale(self):
        out = os.path.join(self.tmp.name, "u.png")
        with mock.patch.object(gen_asset, "generate_raw", self.fake(300, 200)):
            res = gen_asset.render_full("x", out, upscale=2)
        self.assertEqual(res["size"], [600, 400])


# ---------------------------------------------------------------------------
# ChatGPT's "Create image" mode
# ---------------------------------------------------------------------------
class FakeBox:
    def __init__(self):
        self.calls = []

    async def apply(self, js):
        self.calls.append(js)


class SendingPage:
    def __init__(self):
        self.sent = []

    async def send(self, cmd):
        self.sent.append(next(cmd))


class ImageToolMode(unittest.IsolatedAsyncioTestCase):
    def test_pill_selectors_keep_the_measured_markers(self):
        self.assertIn('[data-system-hint-type="picture_v2"]', chatgpt._IMAGE_PILL_SEL)
        self.assertIn("[data-inline-selection-pill]", chatgpt._TOOL_PILL_SEL)
        self.assertIn("Add files and more", chatgpt._PLUS_LABELS)
        self.assertIn("Create image", chatgpt._IMAGE_TOOL_LABELS)

    def provider(self, state):
        p = ChatGPTProvider()
        p.read_limits = mock.AsyncMock(return_value={})
        p.select_variant = mock.AsyncMock(return_value="GPT-6 / High")
        p._tool_state = mock.AsyncMock(return_value=state)
        p.select_image_tool = mock.AsyncMock(return_value=True)
        p.clear_composer = mock.AsyncMock(return_value=True)
        return p

    async def test_an_image_request_chooses_the_tool(self):
        p, opts = self.provider({"pills": 0}), {"kind": "image"}
        await p.prepare_page(object(), opts)
        p.select_image_tool.assert_awaited_once()
        self.assertIs(opts["image_tool"], True)

    async def test_a_text_request_clears_a_left_over_pill(self):
        p = self.provider({"editor": True, "pills": 1, "image": 1})
        await p.prepare_page(object(), {"kind": "chat"})
        p.clear_composer.assert_awaited_once()
        p.select_image_tool.assert_not_awaited()

    async def test_a_text_request_without_pills_touches_nothing(self):
        p = self.provider({"editor": True, "pills": 0})
        await p.prepare_page(object(), {"kind": "chat"})
        p.clear_composer.assert_not_awaited()

    async def test_mode_off_falls_back_to_words(self):
        p, opts = self.provider({"pills": 0}), {"kind": "image"}
        with mock.patch.object(chatgpt, "IMAGE_TOOL_MODE", False):
            await p.prepare_page(object(), opts)
        p.select_image_tool.assert_not_awaited()
        self.assertNotIn("image_tool", opts)

    async def test_typing_in_tool_mode_inserts_after_the_pill(self):
        prompt = "Create a design mockup.\nBrief: Roam"
        p = ChatGPTProvider()
        p._tool_state = mock.AsyncMock(side_effect=[
            {"editor": True, "pills": 1, "image": 1, "text": ""},
            {"editor": True, "pills": 1, "image": 1, "text": prompt}])
        page, box = SendingPage(), FakeBox()
        await p.type_prompt(page, box, prompt)
        self.assertEqual(box.calls, [p._CARET_END_JS])     # never select-all
        self.assertEqual([m["method"] for m in page.sent], ["Input.insertText"])

    async def test_composer_text_leaves_the_pill_out(self):
        p = ChatGPTProvider()
        p._tool_state = mock.AsyncMock(return_value={"editor": True, "pills": 1, "text": ""})
        self.assertEqual(await p.composer_text(object()), "")


# ---------------------------------------------------------------------------
# Attaching several references (live fault, 14 Oct 2026: 3 images sent for 2)
# ---------------------------------------------------------------------------
import shutil  # noqa: E402
import subprocess  # noqa: E402

from browser_llm.providers import base as provider_base  # noqa: E402

# ChatGPT's four inputs as measured on 14 Oct 2026, in document order.
LIVE_INPUTS = [
    {"label": "Take a photo", "accept": "image/*", "capture": True, "multiple": False},
    {"label": "Attach photos or videos", "accept": "image/*,video/*", "multiple": True},
    {"label": "Attach photos", "accept": "image/*", "multiple": True},
    {"label": "Attach files", "accept": "", "multiple": True},
]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FileInputRanking(unittest.TestCase):
    def rank(self, n, many):
        stub = """
const inputs = %s.map(d => ({label: d.label, multiple: !!d.multiple,
  getAttribute: k => (k === 'accept' ? d.accept : null),
  hasAttribute: k => (k === 'capture' ? !!d.capture : false)}));
global.document = {querySelectorAll: s => (s === 'input[type=file]' ? inputs : [])};
const el = %s;
console.log(el ? el.label : 'none');
""" % (json.dumps(LIVE_INPUTS), (provider_base._NTH_FILE_INPUT_JS % (n, many)).strip())
        out = subprocess.run(["node", "-e", stub], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_several_files_never_go_to_a_single_file_camera_input_first(self):
        self.assertEqual(self.rank(0, "true"), "Attach photos or videos")
        self.assertEqual(self.rank(3, "true"), "Take a photo")

    def test_one_file_still_prefers_an_image_gallery_input_over_the_camera(self):
        self.assertEqual(self.rank(0, "false"), "Attach photos or videos")


class AttachSomeOfTheFiles(unittest.IsolatedAsyncioTestCase):
    async def test_an_input_that_takes_some_gets_the_rest_one_at_a_time(self):
        p = ChatGPTProvider()
        sent = []

        class Page:
            async def evaluate(self, js, *a, **k):
                return "4"

            async def send(self, cmd):
                try:
                    req = next(cmd)
                except StopIteration:
                    return None
                if req["method"] == "DOM.setFileInputFiles":
                    sent.append((req["params"]["objectId"], req["params"]["files"]))
                return None

        from nodriver import cdp

        class Remote:
            object_id = cdp.runtime.RemoteObjectId("input-0")

        async def handle(page, js):
            return Remote()

        p.attachment_count = mock.AsyncMock(side_effect=[0, 1, 1])
        p.wait_uploads_ready = mock.AsyncMock(side_effect=[False, True])
        with mock.patch.object(provider_base, "eval_handle", handle):
            ok = await p._attach_via_input(Page(), ["/r/a.png", "/r/b.png"])
        self.assertTrue(ok)
        self.assertEqual(sent, [("input-0", ["/r/a.png", "/r/b.png"]),
                                ("input-0", ["/r/b.png"])])     # never a second input


class AnswerReadFailures(unittest.IsolatedAsyncioTestCase):
    async def test_a_failed_read_is_logged_not_silently_empty(self):
        class Page:
            async def evaluate(self, *a, **k):
                return {"exceptionId": 1, "text": "Uncaught TypeError: x is null"}

        p = ChatGPTProvider()
        with self.assertLogs("gemini_server", level="WARNING") as logs:
            self.assertEqual(await p.get_response_text(Page()), "")
        self.assertIn("answer read failed", logs.output[0])
        self.assertIn("TypeError", logs.output[0])


if __name__ == "__main__":
    unittest.main()
