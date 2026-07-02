"""
Provider registry. Selection is by the OpenAI ``model`` field on each request;
DEFAULT_PROVIDER (env, default ``gemini-browser``) is used when the requested
model is unknown or absent.
"""
import os

from .base import Provider, StreamMonitor, CompletionTracker, patch_cdp, CHROME_ARGS
from .gemini import GeminiProvider
from .chatgpt import ChatGPTProvider

_INSTANCES = [GeminiProvider(), ChatGPTProvider()]
PROVIDERS = {p.name: p for p in _INSTANCES}

DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "gemini-browser")
if DEFAULT_PROVIDER not in PROVIDERS:
    DEFAULT_PROVIDER = _INSTANCES[0].name


def get_provider(model: str | None) -> Provider:
    """Resolve a request's model name to a provider, falling back to the default."""
    if model and model in PROVIDERS:
        return PROVIDERS[model]
    return PROVIDERS[DEFAULT_PROVIDER]


__all__ = ["Provider", "StreamMonitor", "CompletionTracker", "patch_cdp", "CHROME_ARGS", "PROVIDERS", "DEFAULT_PROVIDER", "get_provider"]
