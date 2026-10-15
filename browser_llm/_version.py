"""Single source of truth for the package version.

Imported by the server (surfaced at ``/api/status`` + ``/version`` and in the
web UI / widget footer) and read by ``pyproject.toml`` (dynamic version). Bump
here on every release, add a CHANGELOG entry, and tag the repo to match
(``git tag v2.0.0``); docs/releasing.md has the checklist.
"""

__version__ = "2.1.0"
