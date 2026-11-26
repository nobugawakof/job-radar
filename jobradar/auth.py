"""Accounts: password hashing, session tokens, name rules and login throttling.
Standard library only.

* Passwords are stored as PBKDF2-HMAC-SHA256 with a random salt (never the
  password itself) and compared in constant time.
* A login gives the browser a random session token in an HttpOnly cookie; the
  database keeps only its SHA-256, so a copied database can't log anyone in.
* "kof311" is the administrator. That name can't be taken through sign-up.
* Repeated failed logins from one address or for one name are slowed down.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time

ADMIN_USERNAME = "kof311"
RESERVED = {ADMIN_USERNAME, "admin", "administrator", "root", "system", "jobradar"}
SESSION_DAYS = 30
_ITERATIONS = 240_000
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


class AuthError(ValueError):
    """A user-facing problem with a name, password or login."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(_ITERATIONS, base64.b64encode(salt).decode(),
                                           base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_username(name: str, *, allow_reserved: bool = False) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise AuthError("Usernames are 3-32 characters: letters, digits, dot, dash or underscore, "
                        "starting with a letter or digit.")
    if not allow_reserved and name.lower() in RESERVED:
        raise AuthError("That username is reserved. Choose another one.")
    return name


def check_password(password: str) -> str:
    if len(password or "") < 8:
        raise AuthError("Use a password of at least 8 characters.")
    if len(password) > 200:
        raise AuthError("That password is too long.")
    return password


class Throttle:
    """Slows down guessing: after 5 failures within 15 minutes for one key
    (an address or a username), that key waits 30 s, doubling each time, up to
    15 minutes. A success clears the key."""

    def __init__(self, *, free: int = 5, window: float = 900.0, base: float = 30.0, cap: float = 900.0):
        self.free, self.window, self.base, self.cap = free, window, base, cap
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def wait(self, key: str, now: float | None = None) -> float:
        """Seconds this key must wait before trying again (0 = go ahead)."""
        now = now or time.time()
        with self._lock:
            times = [t for t in self._fails.get(key, []) if now - t < self.window]
            self._fails[key] = times
            if len(times) < self.free:
                return 0.0
            delay = min(self.cap, self.base * 2 ** (len(times) - self.free))
            return max(0.0, times[-1] + delay - now)

    def failed(self, key: str, now: float | None = None) -> None:
        with self._lock:
            self._fails.setdefault(key, []).append(now or time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
