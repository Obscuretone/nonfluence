"""Server-side browser sessions with revocable, host-only cookies."""

import hmac
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import timedelta


def configure_cookie_state(app, name, key, lifetime=300):
    app.config.update(
        SECRET_KEY=key,
        SESSION_COOKIE_NAME=name,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_PATH="/",
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=lifetime),
    )


class BrowserSessions:
    def __init__(self, storage, key, lifetime):
        self.key = key.encode()
        self.lifetime = lifetime
        shared_connection = getattr(storage, "session_connection", None)
        if shared_connection is not None:
            self.connect = shared_connection
            return
        db_path = getattr(storage, "path", storage)
        self.path = db_path.with_name(db_path.name + ".sessions")
        self.connect = self._local_connection
        with self.connect(mutate=True) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS owner_sessions (
                    digest TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires INTEGER NOT NULL
                )
            """)

    @contextmanager
    def _local_connection(self, *, mutate=False):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def digest(self, token):
        return hmac.new(self.key, b"session:" + token.encode(), "sha256").hexdigest()

    def logout_token(self, token):
        return hmac.new(self.key, b"logout:" + token.encode(), "sha256").hexdigest()

    def issue(self, owner, lifetime=None):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self.connect(mutate=True) as db:
            db.execute("DELETE FROM owner_sessions WHERE expires <= ?", (now,))
            db.execute("INSERT INTO owner_sessions VALUES (?, ?, ?)",
                       (self.digest(token), owner, now + (self.lifetime if lifetime is None else lifetime)))
        return token

    def valid(self, owner, token):
        if not token or len(token) != 43:
            return False
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM owner_sessions WHERE digest = ? AND owner = ? AND expires > ?",
                (self.digest(token), owner, int(time.time())),
            ).fetchone() is not None

    def revoke(self, token):
        if token and len(token) == 43:
            with self.connect(mutate=True) as db:
                db.execute("DELETE FROM owner_sessions WHERE digest = ?", (self.digest(token),))
