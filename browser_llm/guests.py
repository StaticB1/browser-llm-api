"""
Guest keys: a second person on the API with a daily image allowance (stdlib only).

BROWSER_LLM_GUEST_KEYS
    Comma-separated ``name:key:images`` entries, for example
    ``sam:Xy...long-random...:20``. A request carrying a guest's key is that
    guest's, from any address. A guest reaches chat, images, the model list
    and a trimmed status (``authz.guest_may``), never the account's history,
    the gallery or the delete endpoints, and may make ``images`` images in any
    24 hours. The owner's own key (BROWSER_LLM_API_KEY) and loopback callers
    are not limited.

Why a rolling 24 hours: ChatGPT's own image counter is a 24-hour window, so a
guest's share is measured the same way as the pool it is taken from.

The count is kept in a small JSON file (``guest-images.json`` in the state
directory) so a restart does not hand a guest a fresh day. A request reserves
a place before it queues for the browser, which stops two requests sent at
once from both taking the last image; a failed request gives its place back.
"""
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gemini_server")

WINDOW = 24 * 3600
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_MIN_KEY = 16


@dataclass(frozen=True)
class Guest:
    name: str
    key: str
    images_per_day: int


def parse_guest_keys(raw: Optional[str], owner_key: str = "") -> dict:
    """``name:key:images,...`` -> {key: Guest}. A malformed entry is logged and
    skipped rather than stopping the server, and a guest can never hold the
    owner's key (that would quietly put the owner under a limit)."""
    guests: dict = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            logger.warning("BROWSER_LLM_GUEST_KEYS: an entry is not name:key:images; skipped")
            continue
        name, key, limit = (p.strip() for p in parts)
        if not _NAME_RE.match(name):
            logger.warning(f"BROWSER_LLM_GUEST_KEYS: bad guest name {name!r}; skipped")
            continue
        if len(key) < _MIN_KEY:
            logger.warning(f"BROWSER_LLM_GUEST_KEYS: {name}'s key is shorter than "
                           f"{_MIN_KEY} characters; skipped")
            continue
        if owner_key and key == owner_key:
            logger.warning(f"BROWSER_LLM_GUEST_KEYS: {name}'s key is the owner's key; skipped")
            continue
        try:
            n = int(limit)
        except ValueError:
            n = -1
        if n < 0:
            logger.warning(f"BROWSER_LLM_GUEST_KEYS: {name}'s image count {limit!r} is not "
                           f"a whole number; skipped")
            continue
        if key in guests:
            logger.warning(f"BROWSER_LLM_GUEST_KEYS: {name} reuses {guests[key].name}'s key; "
                           f"skipped")
            continue
        guests[key] = Guest(name, key, n)
    return guests


def default_state_dir() -> Path:
    """Where the count lives: BROWSER_LLM_STATE_DIR, else the same folder the
    watchdog writes to (``$XDG_STATE_HOME/browser-llm-api``)."""
    d = os.environ.get("BROWSER_LLM_STATE_DIR", "").strip()
    if d:
        return Path(d).expanduser()
    base = os.environ.get("XDG_STATE_HOME", "").strip() or str(Path.home() / ".local/state")
    return Path(base).expanduser() / "browser-llm-api"


class QuotaExceeded(Exception):
    """No image left for this guest. ``retry_after`` is None for a key whose
    allowance is 0: it is a words-only key, and no wait will change that."""

    def __init__(self, guest: Guest, used: int, retry_after: Optional[int]):
        self.guest, self.used = guest, used
        if retry_after is None:
            self.retry_after = None
            super().__init__("This key may not make images; it is for chat only.")
            return
        self.retry_after = max(1, int(retry_after))
        free_at = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(time.time()
                                                                    + self.retry_after))
        h, m = divmod((self.retry_after + 59) // 60, 60)
        wait = f"{h} h {m} min" if h else f"{m} min"
        super().__init__(
            f"This key may make {guest.images_per_day} images in any 24 hours and has "
            f"used {used}. The next one is free at {free_at} (in {wait}).")


class ImageQuota:
    """Per-guest image count over a rolling 24 hours, kept in a JSON file.

    File shape: {"<guest name>": [[unix time, "file name"], ...]}. Entries older
    than the window are dropped on every write, so the file stays small."""

    def __init__(self, path: Path, window: int = WINDOW):
        self.path = Path(path)
        self.window = window
        self._lock = threading.Lock()
        self._data: Optional[dict] = None
        self._pending: dict = {}      # token -> (name, reserved at)
        self._next = 0

    # -- storage --
    def _load(self) -> dict:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text())
                self._data = {k: [list(e) for e in v] for k, v in raw.items()
                              if isinstance(v, list)}
            except FileNotFoundError:
                self._data = {}
            except Exception as e:
                # A damaged file must not lift every limit, but it must not stop
                # the server either: start from empty and say so.
                logger.warning(f"guest image count unreadable ({e}); starting from empty")
                self._data = {}
        return self._data

    def _save(self, now: float) -> None:
        data = self._load()
        for name in list(data):
            data[name] = [e for e in data[name] if now - float(e[0]) < self.window]
            if not data[name]:
                del data[name]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"could not save the guest image count to {self.path}: {e}")

    def _times(self, name: str, now: float) -> list:
        return sorted(float(e[0]) for e in self._load().get(name, [])
                      if now - float(e[0]) < self.window)

    def _pending_for(self, name: str) -> int:
        return sum(1 for n, _ in self._pending.values() if n == name)

    # -- the calls the server makes --
    def snapshot(self, guest: Guest, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        with self._lock:
            times = self._times(guest.name, now)
            used = len(times) + self._pending_for(guest.name)
        out = {"name": guest.name, "images_per_day": guest.images_per_day,
               "used": used, "remaining": max(guest.images_per_day - used, 0)}
        idx = used - guest.images_per_day
        if 0 <= idx < len(times):
            out["next_free_at"] = int(times[idx] + self.window)
        return out

    def reserve(self, guest: Guest, now: Optional[float] = None) -> int:
        """Hold one image for a request about to run, or raise QuotaExceeded."""
        now = time.time() if now is None else now
        if guest.images_per_day <= 0:
            raise QuotaExceeded(guest, 0, None)
        with self._lock:
            times = self._times(guest.name, now)
            used = len(times) + self._pending_for(guest.name)
            if used >= guest.images_per_day:
                # Images leave the window oldest first; a place opens when
                # used - allowance + 1 of them have gone.
                idx = used - guest.images_per_day
                if idx < len(times):
                    wait = times[idx] + self.window - now
                else:
                    wait = 60   # the place is held by a request still running
                raise QuotaExceeded(guest, used, wait)
            self._next += 1
            self._pending[self._next] = (guest.name, now)
            return self._next

    def release(self, token: Optional[int]) -> None:
        """Give a reservation back (the request failed or made no image)."""
        with self._lock:
            self._pending.pop(token, None)

    def commit(self, guest: Guest, files: list, token: Optional[int] = None,
               now: Optional[float] = None) -> dict:
        """Count the images a request delivered (``files`` = their names; a
        chat answer has no reservation, so ``token`` may be None)."""
        now = time.time() if now is None else now
        with self._lock:
            self._pending.pop(token, None)
            entries = self._load().setdefault(guest.name, [])
            for f in files:
                entries.append([now, str(f or "")])
            self._save(now)
        snap = self.snapshot(guest, now)
        for f in files:
            logger.info(f"guest {guest.name}: image {snap['used']}/{guest.images_per_day} "
                        f"in 24 h ({f or 'not saved'})")
        if snap["used"] > guest.images_per_day:
            logger.warning(f"guest {guest.name} is over the allowance "
                           f"({snap['used']}/{guest.images_per_day}): a chat answer drew "
                           f"an image after being asked for words only")
        return snap
