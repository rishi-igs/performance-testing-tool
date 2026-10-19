"""Accounts, sessions and API tokens.

Passwords are hashed with PBKDF2-HMAC-SHA256 (600,000 iterations, random salt, constant-time
comparison). Session cookies and API tokens are random 32-byte values; only their SHA-256 is
stored. Repeated failed logins for a user name or address are refused for 15 minutes.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

ITERATIONS = 600_000
ROLES = {"viewer": 1, "tester": 2, "admin": 3}
SESSION_COOKIE = "session"
SESSION_HOURS = 12
TOKEN_PREFIX = "pt_"
MIN_PASSWORD = 10
MAX_PASSWORD = 256
MAX_FAILURES, FAILURE_WINDOW = 5, 15 * 60
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._@-]{1,63}$")


def normalize_username(name: str) -> str:
    """User names are compared without case: 'Alice' and 'alice' are the same account."""
    return name.strip().lower()


def username_problem(name: str) -> str | None:
    if not USERNAME.match(name):
        return "use 2 to 64 letters, digits, dots, dashes, underscores or @, starting with a letter or digit"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(candidate.hex(), digest)


def dummy_verify(password: str) -> bool:
    """Spend the same time as a real check, so answers do not reveal which user names exist."""
    hashlib.pbkdf2_hmac("sha256", password.encode(), b"\x00" * 16, ITERATIONS)
    return False


def new_secret(prefix: str = "") -> str:
    return prefix + secrets.token_hex(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_problem(password: str, username: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"use at least {MIN_PASSWORD} characters"
    if len(password) > MAX_PASSWORD:
        return f"use at most {MAX_PASSWORD} characters"
    if password.lower() == username.lower():
        return "the password must not be the user name"
    return None


class LoginThrottle:
    """Counts failed logins per user name and per client address."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > FAILURE_WINDOW:
            q.popleft()
        return q

    def blocked_for(self, *keys: str) -> int:
        now = time.time()
        with self._lock:
            waits = [int(FAILURE_WINDOW - (now - q[0])) + 1 for k in keys if len(q := self._recent(k, now)) >= MAX_FAILURES]
        return max(waits, default=0)

    def failed(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures[k].append(time.time())

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)


class Principal(str):
    """Who is calling: a str (the audit name, e.g. 'alice' or 'anonymous@127.0.0.1') with a role.

    `projects` is None for everyone who sees every project (admins, the open and API-key modes).
    """

    user_id: str | None
    role: str
    projects: set[str] | None
    mode: str

    def __new__(cls, name: str, *, user_id: str | None = None, role: str = "admin",
                projects: set[str] | None = None, mode: str = "open") -> "Principal":
        obj = super().__new__(cls, name)
        obj.user_id, obj.role, obj.projects, obj.mode = user_id, role, projects, mode
        return obj

    def can(self, role: str) -> bool:
        return ROLES[self.role] >= ROLES[role]

    def sees(self, project_id: str) -> bool:
        return self.projects is None or project_id in self.projects
