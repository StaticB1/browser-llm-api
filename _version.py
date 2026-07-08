"""Single source of truth for the package version.

Imported by ``server.py`` (surfaced at ``/api/status`` + ``/version`` and in the
web UI / widget footer) and read by ``pyproject.toml`` (dynamic version). Bump
here on every release and tag the repo to match (e.g. ``git tag v0.1.0``).
"""

__version__ = "0.1.0"
