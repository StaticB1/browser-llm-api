"""browser-llm-api: an OpenAI-compatible API over signed-in ChatGPT and Gemini
web sessions, plus design tools built on ChatGPT's image tool.

Importing the package reads a .env file first (see config.py), so the server,
the CLI, the MCP server and the tests all see the same settings, however they
were started. The real environment always wins over the file.
"""
from . import config as _config
from ._version import __version__

_config.load_env()

__all__ = ["__version__"]
