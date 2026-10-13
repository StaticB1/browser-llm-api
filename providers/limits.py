"""
Usage limits a chat site imposes, read two ways (stdlib only, no browser).

1. The site's own counter, read before an image request. chatgpt.com answers
   ``POST /backend-api/conversation/init`` (the call its page makes on every
   new chat) with ``limits_progress``: one entry per metered feature with
   ``remaining`` and ``reset_after``. Measured on a Plus account on
   13 Oct 2026, 23:07 KST: image_gen 120 remaining with reset_after 24 hours
   ahead, file_upload 80 with reset_after 3 hours ahead. ``quota_refusal``
   turns that into a refusal when too few are left, so an agent is told the
   reset time at once instead of driving the page into a refusal.

2. The site's refusal text, read after an image request that produced no
   image. ``parse_limit_message`` recognises ChatGPT's wording ("You've hit the
   Plus plan limit for image generation requests ... resets in 23 hours and 59
   minutes", "You're generating images too quickly ... Please wait for 3
   minutes") and works out when it resets.

Both end in ``UsageLimitReached``, a ``RateLimited`` the server answers with
HTTP 429 and a Retry-After header.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .base import RateLimited


class UsageLimitReached(RateLimited):
    """The site will not do this until its limit resets.

    ``reset_at`` is an aware datetime when the site said when (None if it did
    not); ``source`` says how we know ("the page's own counter" or "the page's
    message")."""

    def __init__(self, message: str, *, feature: str = "image_gen",
                 reset_at: datetime | None = None, remaining: int | None = None,
                 source: str = "", now: datetime | None = None):
        super().__init__(message)
        self.feature = feature
        self.reset_at = reset_at
        self.remaining = remaining
        self.source = source
        self._now = now

    def retry_after(self) -> int | None:
        """Whole seconds until the reset, for a Retry-After header."""
        if self.reset_at is None:
            return None
        now = self._now or datetime.now(timezone.utc)
        return max(0, int((self.reset_at - now).total_seconds()) + 1)


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def describe_reset(reset_at: datetime | None, now: datetime | None = None) -> str:
    """'at 2026-10-14 23:02 KST (in 23 h 52 min)', or '' when unknown."""
    if reset_at is None:
        return ""
    now = now or datetime.now(timezone.utc)
    secs = max(0, int((reset_at - now).total_seconds()))
    hours, rest = divmod(secs, 3600)
    mins = rest // 60
    span = f"{hours} h {mins} min" if hours else f"{max(mins, 1)} min"
    local = reset_at.astimezone().strftime("%Y-%m-%d %H:%M %Z").strip()
    return f"at {local} (in {span})"


def quota_refusal(limits_progress, *, feature: str = "image_gen", reserve: int = 0,
                  blocked_features=None, now: datetime | None = None
                  ) -> UsageLimitReached | None:
    """A refusal when the site's counter says ``feature`` has ``reserve`` or
    fewer uses left (or the feature is blocked), else None.

    ``reserve`` keeps some uses back for the account owner's own chats: the
    counter is shared with everything else signed in to the account."""
    now = now or datetime.now(timezone.utc)
    for b in blocked_features or []:
        name = b.get("feature_name") if isinstance(b, dict) else b
        if isinstance(name, str) and (name == feature or "image" in name.lower()):
            reset_at = _parse_iso(b.get("reset_after")) if isinstance(b, dict) else None
            when = describe_reset(reset_at, now)
            return UsageLimitReached(
                f"ChatGPT has blocked {name} on this account"
                + (f"; it resets {when}" if when else "")
                + ". Stop and say so; do not retry before then.",
                feature=feature, reset_at=reset_at, remaining=0,
                source="the page's own counter", now=now)
    for entry in limits_progress or []:
        if not isinstance(entry, dict) or entry.get("feature_name") != feature:
            continue
        remaining = entry.get("remaining")
        if not isinstance(remaining, int) or isinstance(remaining, bool) or remaining > reserve:
            return None
        reset_at = _parse_iso(entry.get("reset_after"))
        kept = f", and {reserve} are kept back for the account owner" if reserve else ""
        when = describe_reset(reset_at, now)
        return UsageLimitReached(
            f"ChatGPT's image limit is reached: its own counter shows {remaining} "
            f"image(s) left{kept}. It resets {when or 'at a time the site did not give'}. "
            f"Stop and say so; do not retry before then.",
            feature=feature, reset_at=reset_at, remaining=remaining,
            source="the page's own counter", now=now)
    return None


# ---------------------------------------------------------------------------
# The refusal text
# ---------------------------------------------------------------------------
# Second-person refusal wording only, so an answer that merely talks ABOUT
# limits never matches; the caller also only asks when no image came back.
_LIMIT_RE = re.compile(
    r"(you(?:'|’)?ve|you have)\s+(hit|reached)\b[^.\n]{0,80}\blimit"
    r"|\b(image|images)\b[^.\n]{0,60}\blimit\b[^.\n]{0,40}\b(reached|hit|resets?)"
    r"|you(?:'|’)?re generating images too quickly"
    r"|you are generating images too quickly"
    r"|wait\b[^.\n]{0,40}\bbefore generating more images",
    re.I)
_IMAGE_WORD_RE = re.compile(r"\b(image|images|picture|pictures|generat\w*)\b", re.I)

_UNIT_SECONDS = {"day": 86400, "hour": 3600, "hr": 3600, "minute": 60, "min": 60,
                 "second": 1, "sec": 1}
_SPAN_RE = re.compile(r"(\d+)\s*(days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)\b", re.I)
_RELATIVE_RE = re.compile(
    r"(?:resets?|wait|try again|available again|more images)\b[^.\n]{0,40}?"
    r"\b(?:in|for|after)\s+((?:\d+\s*(?:days?|hours?|hrs?|minutes?|mins?|seconds?|secs?)"
    r"[\s,]*(?:and\s+)?)+)", re.I)
_CLOCK_RE = re.compile(
    r"\b(?:at|after|until)\s+(\d{1,2}):(\d{2})\s*([ap]\.?m\.?)?", re.I)


def _span_seconds(text: str) -> int:
    total = 0
    for n, unit in _SPAN_RE.findall(text):
        u = unit.lower()
        u = u[:-1] if u.endswith("s") and u[:-1] in _UNIT_SECONDS else u
        total += int(n) * _UNIT_SECONDS.get(u, 0)
    return total


def parse_limit_message(text: str, now: datetime | None = None) -> dict | None:
    """Recognise a ChatGPT image-limit refusal and when it resets.

    Returns None for ordinary text, else ``{"kind": "limit"|"too_fast",
    "message": <the sentence>, "reset_at": aware datetime|None}``. A clock
    time ("after 4:12 PM") is read in this machine's local zone, which is the
    browser's zone too, and moved to the next day if it has already passed."""
    t = (text or "").strip()
    if not t or len(t) > 1200:
        return None
    m = _LIMIT_RE.search(t)
    if not m or not _IMAGE_WORD_RE.search(t):
        return None
    now = now or datetime.now(timezone.utc)
    kind = "too_fast" if re.search(r"too quickly|before generating more", t, re.I) else "limit"
    reset_at = None
    rel = _RELATIVE_RE.search(t)
    if rel:
        secs = _span_seconds(rel.group(1))
        if secs:
            reset_at = now + timedelta(seconds=secs)
    if reset_at is None:
        clock = _CLOCK_RE.search(t)
        if clock:
            hour, minute = int(clock.group(1)), int(clock.group(2))
            ampm = (clock.group(3) or "").lower().replace(".", "")
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
            if hour < 24 and minute < 60:
                local_now = now.astimezone()
                cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if cand <= local_now:
                    cand += timedelta(days=1)
                reset_at = cand
    start = t.rfind(".", 0, m.start()) + 1
    end = t.find(".", m.end())
    sentence = t[start:end + 1 if end >= 0 else len(t)].strip()
    return {"kind": kind, "message": sentence or t[:300], "reset_at": reset_at}


def refusal_from_message(text: str, now: datetime | None = None
                         ) -> UsageLimitReached | None:
    """``parse_limit_message`` as an exception, or None."""
    hit = parse_limit_message(text, now)
    if not hit:
        return None
    when = describe_reset(hit["reset_at"], now)
    what = ("ChatGPT says images are being made too quickly"
            if hit["kind"] == "too_fast" else "ChatGPT says its image limit is reached")
    return UsageLimitReached(
        f"{what}: \"{hit['message']}\" "
        + (f"It resets {when}. " if when else "It did not say when it resets. ")
        + "Stop and say so; do not retry before then.",
        feature="image_gen", reset_at=hit["reset_at"], remaining=0,
        source="the page's message", now=now)
