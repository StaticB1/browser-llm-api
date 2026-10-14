"""Settings: a .env file, and the setting names of 2.0.

``load_env()`` reads ``KEY=VALUE`` lines from the first .env file it finds:
``$BROWSER_LLM_ENV_FILE``, then ``.env`` in the current directory, then ``.env``
in the checkout root. A key the environment already sets is left alone, so a
systemd drop-in or a shell export always wins over the file. Lines may start
with ``export``, values may be quoted, and ``#`` starts a comment. Nothing is
expanded. ``BROWSER_LLM_NO_DOTENV=1`` skips the file.

``env(name, *old, default)`` reads a setting by its 2.0 name and falls back to
the names it had before 2.0, logging once which old name is still in use:

    BROWSER_LLM_IMAGE_DIR   was GEMINI_IMAGE_DIR (and IMAGE_DIR)
    BROWSER_LLM_PUBLIC_URL  was GEMINI_PUBLIC_URL

Standard library only: client.py and the MCP server import the package too.
"""
import logging
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # the checkout, for a source install
_log = logging.getLogger("browser_llm.config")
_loaded: "str | None" = None
_warned: set = set()


def parse_env(text: str) -> dict:
    """KEY=VALUE pairs from .env text. Blank lines, comments and malformed
    lines are skipped; a value wrapped in matching quotes loses them."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def find_env_file() -> "Path | None":
    explicit = os.environ.get("BROWSER_LLM_ENV_FILE", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    for p in (Path.cwd() / ".env", ROOT / ".env"):
        if p.is_file():
            return p
    return None


def load_env() -> "str | None":
    """Apply the .env file once. Returns its path, or None when none applied."""
    global _loaded
    if _loaded is not None:
        return _loaded or None
    _loaded = ""
    if os.environ.get("BROWSER_LLM_NO_DOTENV", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    path = find_env_file()
    if path is None:
        return None
    try:
        pairs = parse_env(path.read_text(encoding="utf-8"))
    except OSError as e:
        _log.warning(f"could not read {path}: {e}")
        return None
    for key, value in pairs.items():
        os.environ.setdefault(key, value)
    _loaded = str(path)
    return _loaded


def env(name: str, *old: str, default=None):
    """The setting ``name``, else the first of its pre-2.0 names that is set,
    else ``default``. Empty values count as unset."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for legacy in old:
        value = os.environ.get(legacy, "").strip()
        if value:
            if legacy not in _warned:
                _warned.add(legacy)
                _log.warning(f"{legacy} is the pre-2.0 name of {name}; it still works, "
                             f"but rename it when you next touch the config")
            return value
    return default
