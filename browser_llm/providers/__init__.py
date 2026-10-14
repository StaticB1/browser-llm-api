"""
Provider registry. Selection is by the OpenAI ``model`` field on each request;
DEFAULT_PROVIDER (env, default ``chatgpt-browser``) is used when the requested
model is unknown or absent.

ChatGPT is the default since 13 Oct 2026 (0.5.0). Gemini stays available by
name; an install can bar it from images with IMAGE_PROVIDERS.
"""
import os

from .base import (Provider, StreamMonitor, CompletionTracker, RateLimited,
                   patch_cdp, CHROME_ARGS)
from .chatgpt import ChatGPTProvider
from .gemini import GeminiProvider
from .limits import UsageLimitReached

_INSTANCES = [ChatGPTProvider(), GeminiProvider()]
PROVIDERS = {p.name: p for p in _INSTANCES}

DEFAULT_PROVIDER = os.environ.get("DEFAULT_PROVIDER", "chatgpt-browser")
if DEFAULT_PROVIDER not in PROVIDERS:
    DEFAULT_PROVIDER = _INSTANCES[0].name

# Providers allowed to make images (comma-separated names). Unset = every
# provider that supports images. This box sets chatgpt-browser only.
_IMAGE_ENV = os.environ.get("IMAGE_PROVIDERS", "").strip()
IMAGE_PROVIDERS = ([n.strip() for n in _IMAGE_ENV.split(",") if n.strip()] if _IMAGE_ENV
                   else [p.name for p in _INSTANCES if p.supports_images])


def get_provider(model: str | None) -> Provider:
    """Resolve a request's model name to a provider, falling back to the default."""
    if model and model in PROVIDERS:
        return PROVIDERS[model]
    return PROVIDERS[DEFAULT_PROVIDER]


__all__ = ["Provider", "StreamMonitor", "CompletionTracker", "RateLimited",
           "UsageLimitReached", "patch_cdp", "CHROME_ARGS", "PROVIDERS",
           "DEFAULT_PROVIDER", "IMAGE_PROVIDERS", "get_provider"]
