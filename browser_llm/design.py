#!/usr/bin/env python3
"""
Design with ChatGPT's image tool through the local browser-LLM API: website
sections, app screens, components, brand boards and other kinds of image, one
at a time or as a set that keeps one design system, then the design system read
back out of the images and a build compared against them.

The Codex-style loop this supports: describe the product -> generate one large
readable image per section or screen -> read the tokens out of them -> build ->
screenshot the build -> review it against the design -> fix.

  browser-llm design kinds
  browser-llm design prompt --kind section --brief "..."      # no image spent
  browser-llm design set --kind section --brief "Roam, a travel app for ..." \\
      --section "Hero: headline 'Go somewhere quiet', one button 'Plan a trip'" \\
      --section "How it works: three steps ..." --out designs/roam [--dry-run]
  browser-llm design tokens designs/roam/0*.png --out designs/roam
  browser-llm design review --design designs/roam/01-hero.png \\
      --build shots/hero-1440.png --out designs/roam/review-hero.md

Prompts are written two ways. By default they carry the structure an image needs
to be buildable (one section per image, readable text, a real grid, the set's
design system) and no style words, so ChatGPT chooses the look. --direction adds
an art-direction block per kind group, condensed from the imagegen-frontend-web,
image-to-code, imagegen-frontend-mobile and brandkit skills (see DIRECTION).

The prompt builder is stdlib-only: server.py imports it to serve the same kinds
to the dashboard and to API clients that send ``kind``. The set runner, tokens
and review import gen_asset (Pillow) and client lazily.

Exit codes (CLI): 0 done, 1 an error, 2 a set stopped for --pick, 3 ChatGPT's
image limit (or the planned set does not fit in what is left of it).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Kinds
# ---------------------------------------------------------------------------
# Every rule below is structural: what the image is, what must be readable,
# what a developer needs to rebuild it. None names a style; that is ChatGPT's
# choice unless --direction is on (tests/test_design.py checks the word list).
_TEXT_RULES = {
    "design": [
        "Use the real words from the brief for headings, buttons and labels; leave a "
        "label out rather than fill it with lorem ipsum or invented words.",
        "Every piece of text must be readable when the whole image is viewed at half size.",
    ],
    "none": ["No text, letters, numbers, logos or watermarks anywhere in the image."],
    "exact": ["Write every word exactly as given in the brief, letter for letter, and no "
              "other words."],
    "title": ["If the brief gives a title in quotation marks, write that title exactly and "
              "large; otherwise put no text in the image."],
}

KINDS = {
    "section": {
        "group": "web", "label": "Website section", "unit": "section",
        "size": "1600x900", "text": "design",
        "about": "One section of a website at desktop width, as a flat mockup.",
        "what": "Create a design mockup of ONE section of a website.",
        "rules": [
            "Show the section at desktop width (a 1440-pixel viewport), flat and front-on "
            "like a screenshot: no browser window, device, perspective, hands or desk.",
            "Show this one section only, filling the frame edge to edge. Include the site's "
            "top navigation only when this is the first section.",
            "Lay it out so a front-end developer can rebuild it in HTML and CSS: a column grid,"
            " consistent spacing, real buttons, links and form controls.",
            "Avoid boxes nested inside boxes and labels that carry no information.",
            "A photograph or illustration either fills the section edge to edge or sits in a "
            "frame with a fixed aspect ratio, so CSS can crop it the same way.",
        ],
    },
    "page": {
        "group": "web", "label": "Whole page (one tall image)", "unit": "page",
        "size": "1024x1536", "text": "design",
        "about": "A short web page top to bottom in one tall image. Text gets small: "
                 "prefer a set of sections when the design will be built.",
        "what": "Create a design mockup of a whole web page, top to bottom, in one tall image.",
        "rules": [
            "Show the page at desktop width, flat and front-on: no browser window or device.",
            "Show the sections in the order given, each one distinct from the next.",
            "Headings and buttons must be readable; shorten body copy to fit rather than "
            "shrink it.",
            "Lay it out on one consistent column grid a developer can rebuild in HTML and CSS.",
        ],
    },
    "hero-image": {
        "group": "web", "label": "Hero background image", "unit": "image",
        "size": "1600x900", "text": "none",
        "about": "A background photograph or illustration for the top of a page, with "
                 "room for a headline set in HTML.",
        "what": "Create a background image for the top of a web page.",
        "rules": [
            "It is a photograph or illustration only: no interface, buttons or mockup.",
            "__COPY_SPACE__",
            "Fill the frame edge to edge and keep the subject away from the edges: it will be "
            "cropped to different screen widths.",
        ],
    },
    "og-image": {
        "group": "web", "label": "Social share image", "unit": "image",
        "size": "1200x630", "exact": True, "text": "title",
        "about": "A 1200x630 link preview card, cropped to exact pixels.",
        "what": "Create a social share image (a link preview card).",
        "rules": [
            "The final image is 1200 by 630 pixels. Keep everything important inside the "
            "central 1000 by 520 pixel area; some networks crop the edges.",
        ],
    },
    "mobile-screen": {
        "group": "app", "label": "Mobile app screen", "unit": "screen",
        "size": "1080x1350", "text": "design",
        "about": "One app screen inside a phone frame.",
        "what": "Create a design mockup of ONE screen of a mobile app.",
        "rules": [
            "Show the screen inside a phone frame, front-on and centred, with even margins "
            "around the phone: no hands, desk or perspective.",
            "Keep content inside the safe areas, with the status bar at the top and the "
            "system bar at the bottom.",
            "Give the screen one primary action a thumb can reach.",
            "Body text no smaller than 16 points at the phone's scale.",
            "Use real content from the brief (names, numbers, dates), not placeholders.",
        ],
    },
    "component": {
        "group": "app", "label": "UI component sheet", "unit": "component",
        "size": "1600x900", "text": "design",
        "about": "One component with its states side by side.",
        "what": "Create a design sheet for ONE user-interface component.",
        "rules": [
            "Show it flat and front-on on a plain canvas.",
            "Show its states side by side, each labelled with the state's name: default, "
            "hover, focus, pressed, disabled and, where they apply, error and loading.",
            "Draw it at real size with room around each state, so its padding, corner "
            "radius and borders can be measured.",
        ],
    },
    "brand-board": {
        "group": "brand", "label": "Brand identity board", "unit": "board",
        "size": "1536x1024", "text": "design",
        "about": "A 3x3 board: logo, construction, colours with hex values, type, "
                 "applications.",
        "what": "Create a brand identity board.",
        "rules": [
            "Lay it out as a 3x3 grid of panels with even gutters on one canvas.",
            "Panels: the logo; its construction; the logo in a digital product; the tagline; "
            "the colour system with each hex value written under its swatch; a typography "
            "specimen naming the typefaces; the logo on a physical object; photography "
            "direction; a pattern or system detail.",
            "Spell the brand name exactly as given, the same way in every panel.",
            "Do not use or imitate any real company's logo.",
        ],
    },
    "icon": {
        "group": "brand", "label": "Icon", "unit": "icon",
        "size": "1024x1024", "background": "transparent", "text": "none",
        "about": "One centred icon on a transparent background.",
        "what": "Create ONE icon.",
        "rules": [
            "Centre it with nothing else in the frame: no frame, tile or backdrop.",
            "It must stay recognisable at 32 by 32 pixels: one idea, few details, even "
            "stroke widths.",
            "If reference icons are attached, match their grid, stroke weight, corners and "
            "fill exactly.",
        ],
    },
    "logo-concept": {
        "group": "brand", "label": "Logo concepts (2x2)", "unit": "sheet",
        "size": "1024x1024", "text": "exact",
        "about": "Four different logo concepts for one brand on one sheet.",
        "what": "Create a sheet of four different logo concepts for one brand.",
        "rules": [
            "Lay them out in a 2x2 grid with even gutters, flat and front-on on a plain "
            "background: no mockups.",
            "Each concept pairs a symbol with the brand name, spelled exactly as given.",
            "Each concept takes a different idea: a monogram, a symbol for what the product "
            "does, two metaphors merged, a negative-space mark.",
            "Do not use or imitate any real company's logo.",
        ],
    },
    "illustration": {
        "group": "content", "label": "Spot illustration", "unit": "illustration",
        "size": "1024x1024", "background": "transparent", "text": "none",
        "about": "One illustration on a transparent background.",
        "what": "Create ONE spot illustration.",
        "rules": [
            "Centre one subject with room around it: no frame or backdrop.",
            "It must read at 240 pixels wide.",
        ],
    },
    "diagram": {
        "group": "content", "label": "Labelled diagram", "unit": "diagram",
        "size": "1600x900", "text": "exact",
        "about": "A diagram that uses exactly the labels given.",
        "what": "Create a labelled diagram.",
        "rules": [
            "Draw it flat and front-on on a plain background.",
            "Arrows show the direction of flow; connect the elements as the brief describes.",
            "Leave even space between elements so every label reads at half size.",
        ],
    },
    "texture": {
        "group": "content", "label": "Surface texture", "unit": "texture",
        "size": "1024x1024", "text": "none",
        "about": "An even texture that fills the frame, for backgrounds.",
        "what": "Create a surface texture.",
        "rules": [
            "Fill the whole frame edge to edge with no focal point and no objects.",
            "Keep the detail even across the frame and the edges alike, so it can repeat "
            "as a tile.",
        ],
    },
    "avatar": {
        "group": "content", "label": "Avatar or mascot", "unit": "character",
        "size": "1024x1024", "background": "transparent", "text": "none",
        "about": "One character, head and shoulders, on a transparent background.",
        "what": "Create ONE character as an avatar.",
        "rules": [
            "Head and shoulders, centred: no frame or backdrop.",
            "It must read at 64 by 64 pixels.",
            "If references are attached, keep the same character: face, hair, clothes, "
            "colours.",
        ],
    },
}

GROUPS = {"web": "Web", "app": "App", "brand": "Brand", "content": "Content"}

_COPY_SPACE = {
    None: "Leave a calm, low-detail area on one side or across the lower third for a "
          "headline and a button set in HTML, and put the subject elsewhere.",
    "left": "Keep the left half calm and low in detail for a headline and a button set in "
            "HTML; put the subject on the right.",
    "right": "Keep the right half calm and low in detail for a headline and a button set in "
             "HTML; put the subject on the left.",
    "center": "Keep the centre calm and low in detail for a headline and a button set in "
              "HTML; let the subject frame it.",
    "top": "Keep the top third calm and low in detail for a headline set in HTML; put the "
           "subject in the lower part.",
    "bottom": "Keep the lower third calm and low in detail for a headline and a button set "
              "in HTML; put the subject above it.",
}

_THEME = {
    "dark": "Colour scheme: dark backgrounds with light text.",
    "light": "Colour scheme: light backgrounds with dark text.",
}

_PLATFORM = {
    "ios": "Platform: iOS. Use its conventions: the time in the status bar, a home "
           "indicator at the bottom, a tab bar or toolbar where the screen needs one.",
    "android": "Platform: Android. Use Material conventions: a status bar at the top, a "
               "gesture bar at the bottom, a bottom navigation bar where the screen needs one.",
    "neutral": "Platform: no particular one. Use a neutral phone frame and the conventions "
               "iOS and Android share.",
}

# --direction: art direction per kind group, condensed (14 Oct 2026) from the
# imagegen-frontend-web, image-to-code, imagegen-frontend-mobile and brandkit
# skills in ~/.claude/skills. Kept here rather than read from those files so
# the repository works on any machine; edit both when one changes.
DIRECTION = {
    "web": (
        "Design this like an award-winning studio site, not a template.\n"
        "- Open with a strong first screen: a headline of one to three lines, generous "
        "negative space, one clear call to action. No pills, badges, fake stats or rows of "
        "tiny logos.\n"
        "- Do not default to text on the left and an image on the right. Vary the "
        "composition between sections: a centred statement, text low over a full-bleed "
        "image, an off-grid offset, the image as canvas with text in a safe area.\n"
        "- Typography leads: a confident display face with real contrast in size and "
        "weight, at most two families.\n"
        "- One disciplined palette and one image treatment (grade, framing, material) "
        "across every section, so the set reads as one brand.\n"
        "- Art-directed photography or illustration in fixed frames. No glowing orbs, "
        "gradient blobs, random 3D shapes or stock-looking people.\n"
        "- Give each section one detail that rewards a second look.\n"
        "- More space between sections than a default template, an even rhythm, no walls "
        "of cards and no cards inside cards."
    ),
    "app": (
        "Make this feel like a top-tier native app, not a template.\n"
        "- One focal point per screen, a short headline and one obvious next action.\n"
        "- Comfortably readable text; hierarchy from size and weight rather than boxes.\n"
        "- Custom icons in one consistent style, never generic emoji-like icons.\n"
        "- Surfaces may carry texture or imagery under readable scrims; avoid grey cards "
        "stacked on cards.\n"
        "- One palette, one device frame and one frame scale across all screens.\n"
        "- Avoid the AI tells: glossy gradients everywhere, floating orbs, fake dashboards, "
        "filler chips and stats."
    ),
    "brand": (
        "Work like a brand studio.\n"
        "- Start from strategy: the category, the audience, the personality and one core "
        "metaphor, and let the mark express that metaphor.\n"
        "- Build the mark from a monogram with meaning, the product's action, two "
        "metaphors fused, negative space, or construction geometry.\n"
        "- The mark is simple, ownable and works at small sizes. No generic swooshes, "
        "globes, lightbulbs or copied real-world logos.\n"
        "- Sparse typography, a disciplined palette, generous gutters, cinematic image "
        "direction and uncluttered premium mockups."
    ),
    "content": (
        "Commit to one medium and one lighting setup and carry them through the image. "
        "Avoid the AI tells: glossy plastic surfaces, lens flares, generic gradients, "
        "extra fingers and warped lettering."
    ),
}

_ROLE_TEXT = {
    "lock": "the first {unit} of this same design, which sets its design system",
    "approved": "an approved design for this same product, which sets its design system",
    "previous": "the {unit} just before this one in the same design",
    "user": "a reference image from the user",
}
_SET_ROLES = ("lock", "approved", "previous")


def kind_names():
    return list(KINDS)


def get_kind(kind) -> dict:
    k = KINDS.get(str(kind or "").strip().lower())
    if not k:
        raise ValueError(f"unknown kind {kind!r}; choose one of: {', '.join(KINDS)}")
    return k


def kinds_catalog() -> list:
    """The kinds as the dashboard and /api/design/kinds show them."""
    return [{"id": name, "label": k["label"], "group": k["group"],
             "group_label": GROUPS[k["group"]], "size": k["size"],
             "exact": bool(k.get("exact")), "background": k.get("background"),
             "text": k["text"], "about": k["about"]}
            for name, k in KINDS.items()]


def _role_line(role, unit) -> str:
    if isinstance(role, dict):
        base = _ROLE_TEXT.get(role.get("role"), _ROLE_TEXT["user"]).format(unit=unit)
        note = str(role.get("note") or "").strip()
        return f"{base}: {note}" if note else base
    if role in _ROLE_TEXT:
        return _ROLE_TEXT[role].format(unit=unit)
    return str(role)


def references_text(ref_roles, unit="section") -> str:
    roles = list(ref_roles or [])
    if not roles:
        return ""
    lines = ["Attached images, in order:"]
    lines += [f"{i}. {_role_line(r, unit)}" for i, r in enumerate(roles, 1)]
    lines.append("Create a new image. The attached images are references only: do not "
                 "edit, extend or copy them.")
    codes = {r.get("role") if isinstance(r, dict) else r for r in roles}
    if codes & set(_SET_ROLES):
        lines.append(f"Match the design references exactly in colours, typefaces, type "
                     f"sizes, spacing, corner radius, button and icon style and image "
                     f"treatment, as one designer's work for one product. Do not repeat "
                     f"their components, photographs or text: this {unit} shows only its "
                     f"own content, in a layout of its own.")
    return "\n".join(lines)


def compose(kind, brief, *, direction=False, theme=None, platform=None, system=None,
            copy_space=None, part=None, index=None, total=None, ref_roles=None,
            page_parts=None, alternative=None) -> str:
    """The words an image request carries for ``kind``. The provider adds its
    own lines after these (ChatGPT: the tool's name, the size, the background).

    ``part`` is one item of a set ({"name", "content"}), ``index``/``total``
    its place; ``ref_roles`` says what each attached image is, in order
    ("lock", "approved", "previous", "user", or {"role", "note"});
    ``alternative`` = (k, n) asks for direction k of n."""
    name = str(kind or "").strip().lower()
    k = get_kind(name)
    unit = k["unit"]
    out = [k["what"]]
    brief = str(brief or "").strip()
    if brief:
        out.append("Brief: " + brief)
    if part:
        pname = str(part.get("name") or "").strip()
        content = str(part.get("content") or "").strip()
        head = (f"This image: {unit} {index} of {total}" if index and total
                else f"This image: the {unit}")
        line = head + (f', "{pname}".' if pname else ".")
        if content:
            line += " Content: " + content
        out.append(line)
    if page_parts:
        names = [str(p.get("name") if isinstance(p, dict) else p).strip() for p in page_parts]
        out.append("Sections, top to bottom: " + "; ".join(n for n in names if n) + ".")
    th = str(theme or "").strip().lower()
    if th in _THEME:
        out.append(_THEME[th])
    if name == "mobile-screen":
        out.append(_PLATFORM.get(str(platform or "neutral").strip().lower(),
                                 _PLATFORM["neutral"]))
    if system and str(system).strip():
        out.append("Design system to follow exactly:\n" + str(system).strip())
    if alternative:
        a, n = alternative
        out.append(f"This is direction {a} of {n}. Take a design idea of your own that "
                   f"differs from the other directions in layout, typography and colour, "
                   f"while keeping to the brief.")
    refs = references_text(ref_roles, unit)
    if refs:
        out.append(refs)
    rules = []
    cs = str(copy_space or "").strip().lower() or None
    for r in k["rules"]:
        rules.append(_COPY_SPACE.get(cs, _COPY_SPACE[None]) if r == "__COPY_SPACE__" else r)
    rules += _TEXT_RULES[k["text"]]
    out.append("Requirements:\n" + "\n".join("- " + r for r in rules))
    if direction:
        out.append("Art direction:\n" + DIRECTION[k["group"]])
    return "\n\n".join(out)


def kind_defaults(kind) -> dict:
    """{size, background, exact}: what an image of this kind asks for when the
    caller does not say."""
    k = get_kind(kind)
    return {"size": k["size"], "background": k.get("background"),
            "exact": bool(k.get("exact"))}


# ---------------------------------------------------------------------------
# Talking to the server
# ---------------------------------------------------------------------------
def _base() -> str:
    return (os.environ.get("BROWSER_LLM_API") or os.environ.get("GEMINI_API")
            or "http://localhost:8081").rstrip("/")


def server_status(timeout=10) -> dict:
    req = urllib.request.Request(_base() + "/api/status")
    key = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def quota_plan(planned, status, model) -> tuple:
    """(ok, message): does a set of ``planned`` images fit in what ChatGPT's
    own counter says is left, after the reserve kept for the account owner?

    The counter is the site's, read by the server before each image. On
    13 Oct 2026 it read 120 and did not drop after 7 images, so it can
    overstate what is left; the site's refusal text (HTTP 429) stays the real
    backstop, and a set resumes where it stopped."""
    prov = ((status or {}).get("providers") or {}).get(model) or {}
    q = prov.get("image_quota") or {}
    reserve = int(prov.get("image_reserve") or 0)
    remaining = q.get("remaining")
    if remaining is None:
        return True, (f"the server has not read ChatGPT's image counter yet; it reads it "
                      f"before each image and keeps {reserve} for the account owner")
    usable = int(remaining) - reserve
    if planned <= usable:
        return True, (f"{planned} image(s) planned; ChatGPT's counter shows {remaining} left, "
                      f"{reserve} kept for the account owner")
    reset = q.get("reset_after") or "later"
    return False, (f"{planned} image(s) planned but only {max(usable, 0)} usable: ChatGPT's "
                   f"counter shows {remaining} left and {reserve} are kept for the account "
                   f"owner (it resets {reset}). Pass --partial to make what fits; a set "
                   f"resumes where it stopped.")


# ---------------------------------------------------------------------------
# Sets
# ---------------------------------------------------------------------------
MAX_REFS = int(os.environ.get("MAX_ATTACHMENTS", "6"))
# Kinds whose set preview is a vertical page; the rest are laid side by side.
_VERTICAL = {"section", "page", "hero-image", "og-image"}


def slug(s, fallback="item") -> str:
    v = re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:40].strip("-")
    return v or fallback


def parse_parts(items) -> list:
    """Sections or screens from "Name: content" strings or {name, content}."""
    parts = []
    for it in items or []:
        if isinstance(it, dict):
            name = str(it.get("name") or "").strip()
            content = str(it.get("content") or it.get("brief") or "").strip()
        else:
            s = str(it).strip()
            if not s:
                continue
            name, _, content = s.partition(":")
            name, content = name.strip(), content.strip()
            if not content and len(name) > 40:
                name, content = "", s
        parts.append({"name": name or f"Part {len(parts) + 1}", "content": content})
    return parts


def _user_refs(refs) -> list:
    """[{path, note}] from paths or {path, note} dicts."""
    out = []
    for r in refs or []:
        if isinstance(r, dict):
            p = str(r.get("path") or r.get("url") or "").strip()
            if p:
                out.append({"path": p, "note": str(r.get("note") or "").strip()})
        elif str(r).strip():
            out.append({"path": str(r).strip(), "note": ""})
    return out


def item_refs(index, *, lock=None, lock_role="lock", previous=None, user_refs=None,
              cap=None) -> tuple:
    """(paths, roles) for item ``index`` (1-based) of a set: the style source
    first, then the item before it, then the user's own references, at most
    ``cap`` (the server's attachment limit)."""
    cap = MAX_REFS if cap is None else cap
    paths, roles = [], []
    if lock:
        paths.append(lock)
        roles.append(lock_role)
    if previous and previous != lock:
        paths.append(previous)
        roles.append("previous")
    for r in user_refs or []:
        paths.append(r["path"])
        roles.append({"role": "user", "note": r.get("note", "")})
    return paths[:cap], roles[:cap]


def _hash(s) -> str:
    return hashlib.sha256(str(s).encode()).hexdigest()[:16]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class DesignError(RuntimeError):
    """A set, tokens or review step that failed. ``code`` is the CLI exit code."""

    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def _write_json(path, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_spec(path) -> dict:
    with open(path) as f:
        return json.load(f)


def normalize_spec(spec) -> dict:
    s = dict(spec or {})
    s["kind"] = str(s.get("kind") or "section").strip().lower()
    get_kind(s["kind"])
    s["brief"] = str(s.get("brief") or "").strip()
    s["parts"] = parse_parts(s.get("sections") or s.get("screens") or s.get("parts") or [])
    if not s["parts"]:
        s["parts"] = [{"name": KINDS[s["kind"]]["label"], "content": ""}]
    s["refs"] = _user_refs(s.get("refs"))
    if s.get("system_file") and not s.get("system"):
        with open(s["system_file"]) as f:
            s["system"] = f.read()
    return s


def plan(spec, out_dir, *, directions=0) -> list:
    """Every image a set would make, in order, with its file and prompt, without
    making any. Used by --dry-run and by run_set itself."""
    s = normalize_spec(spec)
    kind, n = s["kind"], len(s["parts"])
    common = dict(direction=bool(s.get("direction")), theme=s.get("theme"),
                  platform=s.get("platform"), system=s.get("system"),
                  copy_space=s.get("copy_space"))
    lock_ext = s.get("lock")
    steps = []
    first = s["parts"][0]
    stem1 = f"01-{slug(first['name'])}"
    if directions and directions > 1 and not lock_ext:
        for a in range(1, directions + 1):
            paths, roles = item_refs(1, user_refs=s["refs"])
            steps.append({"step": "direction", "index": 1, "alt": a, "part": first,
                          "file": os.path.join(out_dir, "directions", f"{stem1}-v{a}.png"),
                          "refs": paths,
                          "prompt": compose(kind, s["brief"], part=first, index=1, total=n,
                                            ref_roles=roles, alternative=(a, directions),
                                            **common)})
    prev_file = None
    lock_file = os.path.abspath(lock_ext) if lock_ext else None
    for i, part in enumerate(s["parts"], 1):
        f = os.path.join(out_dir, f"{i:02d}-{slug(part['name'])}.png")
        if lock_ext:
            paths, roles = item_refs(i, lock=lock_file, lock_role="approved",
                                     previous=prev_file, user_refs=s["refs"])
        else:
            paths, roles = item_refs(i, lock=lock_file if i > 1 else None,
                                     previous=prev_file if i > 2 else None,
                                     user_refs=s["refs"])
        steps.append({"step": "item", "index": i, "part": part, "file": f, "refs": paths,
                      "prompt": compose(kind, s["brief"], part=part, index=i, total=n,
                                        ref_roles=roles, **common)})
        if i == 1 and not lock_ext:
            lock_file = f
        prev_file = f
    return steps


def preview(paths, out, vertical=True, width=1280, height=900, gap=48) -> str:
    """The set as one picture: sections stacked like a page, or screens side
    by side on a grey board."""
    from PIL import Image
    ims = [Image.open(p).convert("RGB") for p in paths if os.path.isfile(p)]
    if not ims:
        return ""
    if vertical:
        tiles = [im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
                 for im in ims]
        sheet = Image.new("RGB", (width, sum(t.height for t in tiles)), (255, 255, 255))
        y = 0
        for t in tiles:
            sheet.paste(t, (0, y))
            y += t.height
    else:
        tiles = [im.resize((round(im.width * height / im.height), height), Image.LANCZOS)
                 for im in ims]
        sheet = Image.new("RGB", (sum(t.width for t in tiles) + gap * (len(tiles) + 1),
                                  height + gap * 2), (236, 236, 236))
        x = gap
        for t in tiles:
            sheet.paste(t, (x, gap))
            x += t.width + gap
    sheet.save(out)
    return out


def _rel(path, root) -> str:
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def run_set(spec, out_dir, *, dry_run=False, partial=False, directions=0, pick=None,
            redo=None, variant=None, timeout=440, model=None, log=None) -> dict:
    """Make every image of a set, one after another, each new item carrying
    the set's first image and the item before it as references, so the
    design system holds. Progress goes into <out_dir>/manifest.json after
    every image; running the same command again resumes, skipping finished
    items whose prompt has not changed. ``redo`` names items (index or name)
    to make again.

    With ``directions`` N > 1, N candidates for item 1 come first and the run
    stops ({"status": "pick"}); the same call with ``pick`` K copies candidate
    K into item 1 and makes the rest against it.

    Returns {"status": "done" | "pick" | "dry-run", ...}. Raises DesignError
    (code 3) when the set does not fit in ChatGPT's remaining images, or when
    the limit is hit part-way (items made so far are kept)."""
    log = log or (lambda m: print(m, file=sys.stderr))
    out_dir = os.path.abspath(out_dir)
    s = normalize_spec(spec)
    steps = plan(s, out_dir, directions=directions)
    mpath = os.path.join(out_dir, "manifest.json")
    manifest = {}
    if os.path.isfile(mpath):
        try:
            with open(mpath) as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}
    items = {it["index"]: it for it in manifest.get("items", [])}
    redo = {str(r).strip().lower() for r in (redo or []) if str(r).strip()}

    def finished(step):
        it = items.get(step["index"])
        if not it or not os.path.isfile(os.path.join(out_dir, it.get("file", ""))):
            return False
        if str(step["index"]) in redo or step["part"]["name"].lower() in redo:
            return False
        return bool(it.get("pinned")) or it.get("prompt_hash") == _hash(step["prompt"])

    dir_steps = [st for st in steps if st["step"] == "direction"]
    item_steps = [st for st in steps if st["step"] == "item"]
    if dir_steps and finished(item_steps[0]):
        dir_steps = []                       # item 1 is already chosen
    have = set((manifest.get("directions") or {}).get("files") or [])
    dir_todo = [st for st in dir_steps
                if not (_rel(st["file"], out_dir) in have and os.path.isfile(st["file"]))]
    stop_for_pick = bool(dir_steps) and not pick
    item_todo = [] if stop_for_pick else [
        st for st in item_steps
        if not finished(st) and not (dir_steps and st["index"] == 1)]
    todo = dir_todo + item_todo

    if dry_run:
        def state(st):
            if st in todo:
                return "make"
            if st["step"] == "direction" or finished(st):
                return "done"
            return "after --pick" if dir_steps else "skip"
        return {"status": "dry-run", "out_dir": out_dir, "kind": s["kind"],
                "planned": len(todo), "steps": [
                    {"file": st["file"], "refs": st["refs"], "prompt": st["prompt"],
                     "state": state(st)} for st in steps]}

    gen_asset = _gen_asset()
    model = model or gen_asset.DEFAULT_MODEL
    if todo:
        try:
            ok, msg = quota_plan(len(todo), server_status(), model)
        except Exception as e:
            ok, msg = True, f"could not read the server's status ({e}); going ahead"
        log(f"[design] {msg}")
        if not ok and not partial:
            raise DesignError(msg, code=3)

    os.makedirs(out_dir, exist_ok=True)
    manifest.update({"version": 1, "kind": s["kind"], "brief": s["brief"],
                     "system": s.get("system"), "theme": s.get("theme"),
                     "platform": s.get("platform"), "direction": bool(s.get("direction")),
                     "refs": s["refs"], "lock": s.get("lock")})
    bg = KINDS[s["kind"]].get("background")
    size = s.get("size") or KINDS[s["kind"]]["size"]

    def save():
        manifest["items"] = [items[i] for i in sorted(items)]
        manifest["updated"] = _now()
        _write_json(mpath, manifest)

    def make(st):
        os.makedirs(os.path.dirname(st["file"]), exist_ok=True)
        t0 = time.monotonic()
        what = (f"direction {st['alt']} of {len(dir_steps)}" if st["step"] == "direction"
                else f"{KINDS[s['kind']]['unit']} {st['index']} of {len(item_steps)} "
                     f"({st['part']['name']})")
        log(f"[design] making {what} -> {_rel(st['file'], out_dir)}"
            + (f" with {len(st['refs'])} reference(s)" if st["refs"] else ""))
        res = gen_asset.render_full(st["prompt"], st["file"], refs=st["refs"] or None,
                                    size=size, knockout=(bg == "transparent"),
                                    variant=variant, timeout=timeout, model=model)
        res["seconds"] = round(time.monotonic() - t0, 1)
        return res

    made = 0
    try:
        for st in dir_todo:
            make(st)
            made += 1
            d = manifest.setdefault("directions", {})
            d["count"] = len(dir_steps)
            files = d.setdefault("files", [])
            rel = _rel(st["file"], out_dir)
            if rel not in files:
                files.append(rel)
            d.setdefault("prompts", {})[rel] = st["prompt"]
            save()

        if dir_steps:
            d = manifest["directions"]
            files = [st["file"] for st in dir_steps]
            sheet = os.path.join(out_dir, "directions", "directions.png")
            if len(files) > 1 and (dir_todo or not os.path.isfile(sheet)):
                gen_asset.contact_sheet(files, sheet)
            d["sheet"] = _rel(sheet, out_dir) if os.path.isfile(sheet) else None
            if stop_for_pick:
                save()
                return {"status": "pick", "out_dir": out_dir, "manifest": mpath,
                        "directions": files, "made": made,
                        "sheet": sheet if os.path.isfile(sheet) else None}
            k = int(pick)
            if not 1 <= k <= len(files):
                raise DesignError(f"--pick {k}: there are {len(files)} directions")
            first = item_steps[0]
            shutil.copyfile(files[k - 1], first["file"])
            items[1] = {"index": 1, "name": first["part"]["name"],
                        "content": first["part"]["content"],
                        "file": _rel(first["file"], out_dir), "pinned": True,
                        "from_direction": k,
                        "prompt": (d.get("prompts") or {}).get(_rel(files[k - 1], out_dir)),
                        "made_at": _now()}
            d["picked"] = k
            save()
            log(f"[design] direction {k} is item 1; making the rest against it")

        for st in item_todo:
            res = make(st)
            made += 1
            items[st["index"]] = {
                "index": st["index"], "name": st["part"]["name"],
                "content": st["part"]["content"], "file": _rel(st["file"], out_dir),
                "prompt": st["prompt"], "prompt_hash": _hash(st["prompt"]),
                "prompt_sent": res.get("prompt_sent"),
                "refs": [_rel(p, out_dir) for p in st["refs"]],
                "original": res.get("original"), "original_size": res.get("original_size"),
                "model_used": res.get("model_used"), "image_tool": res.get("image_tool"),
                "seconds": res.get("seconds"), "made_at": _now()}
            save()
    except DesignError:
        raise
    except Exception as e:
        if made:
            save()
        code = getattr(e, "code", 1)
        raise DesignError(f"{e}\n[design] made {made} image(s) this run; progress is in "
                          f"{mpath}. Run the same command again to continue.",
                          code=3 if code == 3 else 1) from None

    paths = [os.path.join(out_dir, items[i]["file"]) for i in sorted(items)]
    pv = preview(paths, os.path.join(out_dir, "preview.png"),
                 vertical=s["kind"] in _VERTICAL) if paths else ""
    if pv:
        manifest["preview"] = _rel(pv, out_dir)
    save()
    return {"status": "done", "out_dir": out_dir, "manifest": mpath, "made": made,
            "files": paths, "preview": pv or None, "picked": (manifest.get("directions")
                                                             or {}).get("picked")}


def _gen_asset():
    if __package__:
        from . import gen_asset
        return gen_asset
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import gen_asset
    return gen_asset


# ---------------------------------------------------------------------------
# Tokens: read the design system back out of the images
# ---------------------------------------------------------------------------
TOKENS_PROMPT = """The attached {n} image(s) are design mockups of one product{names}. Read the design system out of them, the way a front-end developer would before rebuilding them in HTML and CSS.

Colours measured from the pixels, most common first, photographs included: {measured}. Use these exact values where they match an interface colour you see. Put in "colors" only the colours the interface itself uses (backgrounds, text, buttons, borders, icons); describe colours that appear only inside photographs or illustrations under "imagery" instead.

Reply with ONE JSON object in a ```json code block and nothing else, in this shape. Estimate pixel values at the size a browser would show the design (a 1440-pixel-wide viewport for web designs, the phone's points for app screens):

{{
  "name": "short name for this design",
  "theme": "light | dark | mixed",
  "colors": [{{"name": "kebab-case-name", "hex": "#RRGGBB", "role": "what it is used for"}}],
  "fonts": [{{"role": "display | body | mono", "family_guess": "closest well-known typeface", "fallback": "serif | sans-serif | monospace", "weights": [400, 600]}}],
  "type_scale": [{{"name": "h1", "font_role": "display", "size_px": 64, "line_height": 1.1, "weight": 600, "letter_spacing_em": -0.02, "case": "none | upper"}}],
  "spacing_px": [4, 8, 12, 16, 24, 32, 48, 64, 96, 128],
  "section_padding_px": {{"desktop": 120, "mobile": 64}},
  "container_max_px": 1200,
  "grid": "columns and gutters",
  "radii_px": {{"button": 999, "card": 16, "image": 12}},
  "borders": [{{"name": "hairline", "value": "1px solid #E5E5E5"}}],
  "shadows": [{{"name": "card", "value": "0 8px 24px rgba(0,0,0,.08)"}}],
  "components": [{{"name": "primary button", "spec": "height, padding, fill, text style, radius, icon, hover"}}],
  "sections": [{{"image": 1, "name": "hero", "layout": "what sits where, in words a developer can build from", "notes": "anything unusual"}}],
  "imagery": "how photographs or illustrations are treated: grade, framing, crops",
  "motion_hints": "what motion the design implies, if any",
  "rules": ["short rules that keep a new page consistent with these designs"]
}}

Use only colours you can see. When a value cannot be read exactly, give your best estimate rather than leaving it out."""

REVIEW_PROMPT = """Image 1 is the design. Image 2 is a screenshot of the implementation{width}. Compare them as a senior product designer reviewing a developer's build. List every visible difference that should be fixed, most important first: layout and alignment, spacing, type size and weight, colours, components, images, and elements that are missing or extra. Ignore differences in the wording of body text.{notes}

Reply with ONE JSON object in a ```json code block and nothing else:

{{"match_score": 0, "summary": "one sentence", "differences": [{{"severity": "high | medium | low", "area": "where on the page", "design": "what the design shows", "build": "what the build shows", "fix": "the exact change, in CSS terms where you can"}}], "matches": ["what already matches and must be kept"]}}

match_score runs from 0 (nothing alike) to 100 (indistinguishable)."""

CRITIQUE_PROMPT = """Critique this interface as a senior product designer: specific and direct. Judge hierarchy, layout, spacing rhythm, typography, colour and contrast, component quality, imagery, and whether it looks like a template.{notes}

Reply with ONE JSON object in a ```json code block and nothing else:

{{"score": 0, "summary": "one sentence", "issues": [{{"severity": "high | medium | low", "area": "where", "problem": "what is wrong", "fix": "the exact change, in CSS terms where you can"}}], "strengths": ["what works and must be kept"]}}

score runs from 0 to 100."""


def extract_json(text):
    """The first JSON object in a ChatGPT answer: a ```json block if there is
    one, else the outermost braces. Raises ValueError."""
    t = str(text or "")
    for block in re.findall(r"```[a-zA-Z]*\s*\n(.*?)```", t, re.S):
        try:
            v = json.loads(block)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            v = json.loads(t[a:b + 1])
            if isinstance(v, dict):
                return v
        except Exception:
            pass
    raise ValueError("no JSON object in the answer")


def measured_palette(paths, n=10) -> list:
    """The most common colours in the images: [{"hex", "share"}], share in %.
    Near-duplicates are merged so a gradient does not take every slot."""
    from PIL import Image
    counts = {}
    for p in paths:
        try:
            im = Image.open(p).convert("RGB")
        except Exception:
            continue
        im.thumbnail((240, 240))
        q = im.quantize(colors=24, method=Image.Quantize.MEDIANCUT)
        pal = q.getpalette()
        for c, idx in q.getcolors() or []:
            rgb = tuple(pal[idx * 3: idx * 3 + 3])
            counts[rgb] = counts.get(rgb, 0) + c
    total = sum(counts.values()) or 1
    picked = []
    for rgb, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        for other in picked:
            if sum(abs(a - b) for a, b in zip(rgb, other["rgb"])) < 36:
                other["count"] += c
                break
        else:
            picked.append({"rgb": rgb, "count": c})
    picked.sort(key=lambda d: -d["count"])
    return [{"hex": "#%02X%02X%02X" % d["rgb"], "share": round(100 * d["count"] / total, 1)}
            for d in picked[:n]]


def _kebab(s, fallback="x") -> str:
    return slug(s, fallback)


def tokens_css(t) -> str:
    """CSS custom properties from the tokens JSON."""
    lines = [":root {"]
    for c in t.get("colors") or []:
        if c.get("hex"):
            lines.append(f"  --color-{_kebab(c.get('name'), 'color')}: {c['hex']};")
    for f in t.get("fonts") or []:
        fam = str(f.get("family_guess") or "").strip()
        fb = str(f.get("fallback") or "sans-serif").strip()
        if fam:
            lines.append(f"  --font-{_kebab(f.get('role'), 'font')}: \"{fam}\", {fb};")
    for ts in t.get("type_scale") or []:
        n = _kebab(ts.get("name"), "text")
        if ts.get("size_px") is not None:
            lines.append(f"  --text-{n}-size: {ts['size_px']}px;")
        if ts.get("line_height") is not None:
            lines.append(f"  --text-{n}-line: {ts['line_height']};")
        if ts.get("weight") is not None:
            lines.append(f"  --text-{n}-weight: {ts['weight']};")
        if ts.get("letter_spacing_em") not in (None, 0):
            lines.append(f"  --text-{n}-tracking: {ts['letter_spacing_em']}em;")
    for i, v in enumerate(t.get("spacing_px") or [], 1):
        lines.append(f"  --space-{i}: {v}px;")
    for k, v in (t.get("section_padding_px") or {}).items():
        lines.append(f"  --section-pad-{_kebab(k)}: {v}px;")
    if t.get("container_max_px"):
        lines.append(f"  --container-max: {t['container_max_px']}px;")
    for k, v in (t.get("radii_px") or {}).items():
        lines.append(f"  --radius-{_kebab(k)}: {v}px;")
    for b in t.get("borders") or []:
        if b.get("value"):
            lines.append(f"  --border-{_kebab(b.get('name'), 'border')}: {b['value']};")
    for sh in t.get("shadows") or []:
        if sh.get("value"):
            lines.append(f"  --shadow-{_kebab(sh.get('name'), 'shadow')}: {sh['value']};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _md_cell(s) -> str:
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def design_md(t, images=None) -> str:
    """DESIGN.md for the coding step, from the tokens JSON."""
    out = [f"# {t.get('name') or 'Design system'}", ""]
    out.append("Read out of the design images by ChatGPT; sizes are its estimates at "
               "browser scale. The measured colours come from the pixels. Check both "
               "against the images before treating them as exact.")
    out.append("")
    if images:
        out += ["Source images: " + ", ".join(f"`{os.path.basename(p)}`" for p in images), ""]
    if t.get("theme"):
        out += [f"Theme: {t['theme']}.", ""]
    if t.get("colors"):
        out += ["## Colours", "", "| Token | Hex | Used for |", "|---|---|---|"]
        out += [f"| `--color-{_kebab(c.get('name'), 'color')}` | {_md_cell(c.get('hex'))} | "
                f"{_md_cell(c.get('role'))} |" for c in t["colors"]]
        out.append("")
    if t.get("measured_palette"):
        out += ["Measured from the pixels: " + ", ".join(
            f"{c['hex']} ({c['share']}%)" for c in t["measured_palette"]) + ".", ""]
    if t.get("fonts") or t.get("type_scale"):
        out += ["## Typography", ""]
        for f in t.get("fonts") or []:
            out.append(f"- {f.get('role')}: {f.get('family_guess')} "
                       f"(fallback {f.get('fallback')}), weights "
                       f"{', '.join(str(w) for w in f.get('weights') or [])}")
        if t.get("type_scale"):
            out += ["", "| Style | Font | Size | Line | Weight | Tracking |",
                    "|---|---|---|---|---|---|"]
            out += [f"| {_md_cell(s.get('name'))} | {_md_cell(s.get('font_role'))} | "
                    f"{_md_cell(s.get('size_px'))}px | {_md_cell(s.get('line_height'))} | "
                    f"{_md_cell(s.get('weight'))} | {_md_cell(s.get('letter_spacing_em'))}em |"
                    for s in t["type_scale"]]
        out.append("")
    lay = []
    if t.get("spacing_px"):
        lay.append("Spacing scale: " + ", ".join(f"{v}px" for v in t["spacing_px"]) + ".")
    if t.get("section_padding_px"):
        lay.append("Section padding: " + ", ".join(
            f"{k} {v}px" for k, v in t["section_padding_px"].items()) + ".")
    if t.get("container_max_px"):
        lay.append(f"Container: {t['container_max_px']}px wide at most.")
    if t.get("grid"):
        lay.append(f"Grid: {t['grid']}.")
    if t.get("radii_px"):
        lay.append("Corner radius: " + ", ".join(
            f"{k} {v}px" for k, v in t["radii_px"].items()) + ".")
    for b in t.get("borders") or []:
        lay.append(f"Border {b.get('name')}: `{b.get('value')}`.")
    for sh in t.get("shadows") or []:
        lay.append(f"Shadow {sh.get('name')}: `{sh.get('value')}`.")
    if lay:
        out += ["## Layout", ""] + lay + [""]
    if t.get("components"):
        out += ["## Components", ""]
        out += [f"- **{c.get('name')}**: {c.get('spec')}" for c in t["components"]]
        out.append("")
    if t.get("sections"):
        out += ["## Sections", ""]
        for sct in t["sections"]:
            out.append(f"- Image {sct.get('image')}, {sct.get('name')}: {sct.get('layout')}"
                       + (f" {sct.get('notes')}" if sct.get("notes") else ""))
        out.append("")
    if t.get("imagery"):
        out += ["## Imagery", "", str(t["imagery"]), ""]
    if t.get("motion_hints"):
        out += ["## Motion", "", str(t["motion_hints"]), ""]
    if t.get("rules"):
        out += ["## Rules", ""] + [f"- {r}" for r in t["rules"]] + [""]
    out += ["The same values are in `tokens.css` as custom properties and in "
            "`tokens.json`.", ""]
    return "\n".join(out)


def _ask(prompt, images, *, variant=None, timeout=940):
    if __package__:
        from . import client
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import client
    return client.ask(prompt, images=images, ephemeral=True, timeout=timeout,
                      model_variant=variant)


def extract_tokens(images, out_dir, *, variant=None, timeout=940, log=None) -> dict:
    """Send up to six design images to ChatGPT, read the design system back
    as JSON, and write tokens.json, tokens.css and DESIGN.md into out_dir."""
    log = log or (lambda m: print(m, file=sys.stderr))
    images = [os.path.abspath(p) for p in images]
    missing = [p for p in images if not os.path.isfile(p)]
    if missing:
        raise DesignError(f"image not found: {missing[0]}")
    if not images:
        raise DesignError("no images given")
    if len(images) > MAX_REFS:
        log(f"[design] {len(images)} images given; sending the first {MAX_REFS}, the "
            f"server's attachment limit")
        images = images[:MAX_REFS]
    measured = measured_palette(images)
    names = " (in order: " + ", ".join(os.path.splitext(os.path.basename(p))[0]
                                       for p in images) + ")"
    prompt = TOKENS_PROMPT.format(n=len(images), names=names, measured=", ".join(
        f"{c['hex']} {c['share']}%" for c in measured) or "none")
    log(f"[design] reading the design system out of {len(images)} image(s); a few minutes")
    answer = _ask(prompt, images, variant=variant, timeout=timeout)
    os.makedirs(out_dir, exist_ok=True)
    try:
        t = extract_json(answer)
    except ValueError:
        raw = os.path.join(out_dir, "tokens.raw.md")
        with open(raw, "w") as f:
            f.write(answer or "")
        raise DesignError(f"ChatGPT's answer held no JSON object; it is saved at {raw}")
    t["measured_palette"] = measured
    t["source_images"] = [os.path.basename(p) for p in images]
    paths = {"json": os.path.join(out_dir, "tokens.json"),
             "css": os.path.join(out_dir, "tokens.css"),
             "md": os.path.join(out_dir, "DESIGN.md")}
    _write_json(paths["json"], t)
    with open(paths["css"], "w") as f:
        f.write(tokens_css(t))
    with open(paths["md"], "w") as f:
        f.write(design_md(t, images))
    return {"tokens": t, "files": paths}


def review_md(r, design=None, build=None, critique=None) -> str:
    out = []
    if critique:
        out += [f"# Critique of {os.path.basename(critique)}", "",
                f"Score: {r.get('score')} of 100. {r.get('summary') or ''}".strip(), ""]
        rows = r.get("issues") or []
        if rows:
            out += ["| Severity | Area | Problem | Fix |", "|---|---|---|---|"]
            out += [f"| {_md_cell(d.get('severity'))} | {_md_cell(d.get('area'))} | "
                    f"{_md_cell(d.get('problem'))} | {_md_cell(d.get('fix'))} |" for d in rows]
            out.append("")
        if r.get("strengths"):
            out += ["Keep:", ""] + [f"- {s}" for s in r["strengths"]] + [""]
        return "\n".join(out)
    out += [f"# Review of {os.path.basename(build or '')} against "
            f"{os.path.basename(design or '')}", "",
            f"Match: {r.get('match_score')} of 100. {r.get('summary') or ''}".strip(), ""]
    rows = r.get("differences") or []
    if rows:
        out += ["| Severity | Area | Design | Build | Fix |", "|---|---|---|---|---|"]
        out += [f"| {_md_cell(d.get('severity'))} | {_md_cell(d.get('area'))} | "
                f"{_md_cell(d.get('design'))} | {_md_cell(d.get('build'))} | "
                f"{_md_cell(d.get('fix'))} |" for d in rows]
        out.append("")
    if r.get("matches"):
        out += ["Already matches:", ""] + [f"- {s}" for s in r["matches"]] + [""]
    return "\n".join(out)


def review(*, design=None, build=None, critique=None, out=None, notes=None, width=None,
           variant=None, timeout=940, log=None) -> dict:
    """Compare a build screenshot with its design image, or critique one image.
    Writes <out>.md and <out>.json (out defaults beside the build/critique)."""
    log = log or (lambda m: print(m, file=sys.stderr))
    if critique:
        imgs = [critique]
        prompt = CRITIQUE_PROMPT.format(notes=f" Context: {notes}" if notes else "")
    elif design and build:
        imgs = [design, build]
        prompt = REVIEW_PROMPT.format(
            width=f" at {width} px wide" if width else "",
            notes=f" Context from the developer: {notes}" if notes else "")
    else:
        raise DesignError("give --design and --build, or --critique")
    for p in imgs:
        if not os.path.isfile(p):
            raise DesignError(f"image not found: {p}")
    base = out or os.path.splitext(critique or build)[0] + "-review.md"
    stem = base[:-3] if base.endswith(".md") else base
    log(f"[design] asking ChatGPT for a {'critique' if critique else 'review'}; a few minutes")
    answer = _ask(prompt, [os.path.abspath(p) for p in imgs], variant=variant,
                  timeout=timeout)
    os.makedirs(os.path.dirname(os.path.abspath(stem)) or ".", exist_ok=True)
    try:
        r = extract_json(answer)
    except ValueError:
        with open(stem + ".raw.md", "w") as f:
            f.write(answer or "")
        raise DesignError(f"ChatGPT's answer held no JSON object; it is saved at "
                          f"{stem}.raw.md")
    _write_json(stem + ".json", r)
    with open(stem + ".md", "w") as f:
        f.write(review_md(r, design, build, critique))
    return {"review": r, "files": {"md": stem + ".md", "json": stem + ".json"}}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _spec_from_args(a) -> dict:
    spec = load_spec(a.spec) if getattr(a, "spec", None) else {}
    for key in ("kind", "brief", "theme", "platform", "copy_space", "size", "lock"):
        v = getattr(a, key, None)
        if v:
            spec[key] = v
    if getattr(a, "direction", False):
        spec["direction"] = True
    if getattr(a, "system_file", None):
        with open(a.system_file) as f:
            spec["system"] = f.read()
    elif getattr(a, "system", None):
        spec["system"] = a.system
    if getattr(a, "section", None):
        spec["sections"] = a.section
    if getattr(a, "ref", None):
        spec["refs"] = list(spec.get("refs") or []) + a.ref
    return spec


def _add_prompt_args(p):
    p.add_argument("--kind", choices=list(KINDS))
    p.add_argument("--brief", help="what the product is, who it is for, what it must say")
    p.add_argument("--theme", choices=["light", "dark"])
    p.add_argument("--platform", choices=["ios", "android", "neutral"],
                   help="mobile-screen only")
    p.add_argument("--system", help="design system text to follow exactly (colours, fonts)")
    p.add_argument("--system-file", help="read the design system text from a file "
                                          "(a DESIGN.md from `browser-llm design tokens` works)")
    p.add_argument("--copy-space", choices=["left", "right", "center", "top", "bottom"],
                   help="hero-image only: where the HTML headline goes")
    p.add_argument("--direction", action="store_true",
                   help="add the art-direction block for this kind's group")


def main(argv=None, prog=None):
    ap = argparse.ArgumentParser(
        prog=prog, description="Design images with ChatGPT through the browser-LLM API.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("kinds", help="list the kinds of image")

    p = sub.add_parser("prompt", help="print the prompt a kind would send (no image made)")
    _add_prompt_args(p)
    p.add_argument("--section", help="'Name: content' for one item")

    p = sub.add_parser("set", help="make a set of images that share one design system")
    p.add_argument("--spec", help="JSON file: kind, brief, sections, system, theme, refs, ...")
    _add_prompt_args(p)
    p.add_argument("--section", action="append", metavar="'NAME: CONTENT'",
                   help="one section or screen (repeatable, in order)")
    p.add_argument("--ref", action="append", metavar="PATH",
                   help="a reference image from you: logo, brand asset, mood (repeatable)")
    p.add_argument("--size", help="ask for this size instead of the kind's default")
    p.add_argument("--out", required=True, help="output folder")
    p.add_argument("--directions", type=int, default=0, metavar="N",
                   help="first make N candidate directions for item 1 and stop, so you "
                        "can choose; continue with --pick K")
    p.add_argument("--pick", type=int, metavar="K", help="continue with direction K")
    p.add_argument("--lock", metavar="PATH",
                   help="an approved image that sets the design system for every item")
    p.add_argument("--redo", action="append", metavar="INDEX|NAME",
                   help="make this item again even though it is finished (repeatable)")
    p.add_argument("--partial", action="store_true",
                   help="go ahead even if the set does not fit in today's images")
    p.add_argument("--dry-run", action="store_true",
                   help="print every prompt and the plan; make nothing")
    p.add_argument("--variant", help="ChatGPT model/power, e.g. 'instant'")
    p.add_argument("--timeout", type=int, default=440)

    p = sub.add_parser("tokens", help="read the design system out of design images")
    p.add_argument("images", nargs="+")
    p.add_argument("--out", required=True, help="folder for tokens.json, tokens.css, DESIGN.md")
    p.add_argument("--variant")

    p = sub.add_parser("review", help="compare a build with its design, or critique one")
    p.add_argument("--design")
    p.add_argument("--build")
    p.add_argument("--critique", metavar="IMAGE")
    p.add_argument("--width", type=int, help="the build screenshot's viewport width")
    p.add_argument("--notes", help="context for the reviewer")
    p.add_argument("--out", help="review file (.md; a .json is written beside it)")
    p.add_argument("--variant")

    a = ap.parse_args(argv)
    try:
        if a.cmd == "kinds":
            for k in kinds_catalog():
                print(f"{k['id']:14} {k['group_label']:8} {k['size']:>10}  {k['about']}")
            return 0
        if a.cmd == "prompt":
            if not a.kind:
                ap.error("--kind is required")
            spec = _spec_from_args(a)
            parts = parse_parts([a.section]) if a.section else None
            print(compose(a.kind, spec.get("brief"), direction=a.direction,
                          theme=a.theme, platform=a.platform, system=spec.get("system"),
                          copy_space=a.copy_space, part=parts[0] if parts else None))
            return 0
        if a.cmd == "set":
            spec = _spec_from_args(a)
            res = run_set(spec, a.out, dry_run=a.dry_run, partial=a.partial,
                          directions=a.directions, pick=a.pick, redo=a.redo,
                          variant=a.variant, timeout=a.timeout)
            if res["status"] == "dry-run":
                for st in res["steps"]:
                    print(f"=== [{st['state']}] {st['file']}  refs: "
                          f"{', '.join(os.path.basename(r) for r in st['refs']) or 'none'}")
                    print(st["prompt"])
                    print()
                print(f"{res['planned']} image(s) to make.")
                return 0
            if res["status"] == "pick":
                for f in res["directions"]:
                    print(f)
                if res.get("sheet"):
                    print(res["sheet"])
                print("Open the sheet, then run the same command with --pick K to continue.",
                      file=sys.stderr)
                return 2
            for f in res["files"]:
                print(f)
            if res.get("preview"):
                print(res["preview"])
            print(res["manifest"])
            return 0
        if a.cmd == "tokens":
            res = extract_tokens(a.images, a.out, variant=a.variant)
            for p_ in res["files"].values():
                print(p_)
            return 0
        if a.cmd == "review":
            res = review(design=a.design, build=a.build, critique=a.critique, out=a.out,
                         notes=a.notes, width=a.width, variant=a.variant)
            print(res["files"]["md"])
            return 0
    except (DesignError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return getattr(e, "code", 1)
    except Exception as e:  # gen_asset / client errors carry their own code
        print(str(e), file=sys.stderr)
        return getattr(e, "code", 1) if isinstance(getattr(e, "code", None), int) else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
