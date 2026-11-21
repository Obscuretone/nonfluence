"""MariaDB storage for a hosted Nonfluence workspace."""

from collections import Counter
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import re
import unicodedata

import pymysql
from pymysql.cursors import DictCursor

from .store import Invalid, Store, validate_scope


WORD = re.compile(r"[^\W_]+")


def token(word):
    folded = unicodedata.normalize("NFD", word.casefold())
    return sha256("".join(c for c in folded if unicodedata.category(c) != "Mn").encode()).digest()


def term_counts(text):
    return Counter(token(word) for word in WORD.findall(unicodedata.normalize("NFC", text)))


def excerpt(markdown, terms):
    text = unicodedata.normalize("NFC", markdown)
    words = list(WORD.finditer(text))
    if not words:
        return ""
    match = next((i for i, word in enumerate(words) if token(word.group()) in terms), 0)
    start = max(0, match - 5)
    end = min(len(words), start + 18)
    return ("… " if start else "") + text[words[start].start():words[end - 1].end()] + (
        " …" if end < len(words) else ""
    )


class Database:
    """Expose the parameterized query interface shared with SQLite."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def sql(query):
        return query.replace("?", "%s").replace(" COLLATE NOCASE", " COLLATE utf8mb4_unicode_ci")

    def execute(self, query, values=()):
        cursor = self.connection.cursor()
        cursor.execute(self.sql(query), values)
        return cursor

    def executemany(self, query, values):
        with self.connection.cursor() as cursor:
            cursor.executemany(self.sql(query), list(values))


class MariaStore(Store):
    def __init__(self, config_path, space, *, write=False, classification="private", nira_url=None):
        validate_scope(space, classification)
        self.space = space
        self.write = write
        self.classification = classification
        self.nira_url = nira_url
        self.config = json.loads(Path(config_path).read_text())
        if (
            not isinstance(self.config, dict)
            or set(self.config) != {"unix_socket", "database", "user"}
            or not all(isinstance(value, str) and value for value in self.config.values())
            or not Path(self.config["unix_socket"]).is_absolute()
            or not all(re.fullmatch(r"[a-zA-Z0-9_]{1,64}", self.config[key]) for key in ("database", "user"))
        ):
            raise Invalid("Use a MariaDB config with a local socket, database and user.")
        with self.connect() as db:
            metadata = db.execute(
                "SELECT schema_version, classification FROM workspace_metadata WHERE singleton = 1"
            ).fetchone()
        if not metadata or metadata["schema_version"] != 1 or metadata["classification"] != classification:
            raise Invalid("The database does not match this Nonfluence classification or schema.")

    @contextmanager
    def connect(self, *, mutate=False):
        if mutate and not self.write:
            raise PermissionError("This Nonfluence session is read-only.")
        connection = pymysql.connect(
            **self.config, charset="utf8mb4", cursorclass=DictCursor, binary_prefix=True,
            connect_timeout=5, read_timeout=15, write_timeout=15,
        )
        db = Database(connection)
        try:
            db.execute("SET SESSION innodb_lock_wait_timeout = 10")
            db.execute("START TRANSACTION" if mutate else "START TRANSACTION READ ONLY")
            if mutate:
                # Serialize writers just as SQLite BEGIN IMMEDIATE does. Hierarchy
                # changes, revision checks and idempotent creates share this lock.
                db.execute("SELECT singleton FROM workspace_metadata WHERE singleton = 1 FOR UPDATE").fetchone()
            yield db
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def session_connection(self, *, mutate=False):
        return self.connect(mutate=mutate)

    def index_terms(self, db, target, title, markdown):
        title_counts, body_counts = term_counts(title), term_counts(markdown)
        db.execute("DELETE FROM page_terms WHERE page_id = ?", (target,))
        db.executemany(
            "INSERT INTO page_terms(token, page_id, title_hits, body_hits) VALUES (?, ?, ?, ?)",
            ((word, target, title_counts[word], body_counts[word]) for word in title_counts.keys() | body_counts.keys()),
        )

    def _index(self, db, target, refs):
        super()._index(db, target, refs)
        page = self._get(db, target)
        self.index_terms(db, target, page["title"], page["markdown"])

    def search(self, query, limit=20):
        if not isinstance(query, str) or len(query) > 300:
            raise Invalid("Search text must contain at most 300 characters.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise Invalid("Search limit must be between 1 and 50.")
        terms = tuple(term_counts(query))
        if not terms:
            return []
        placeholders = ", ".join("?" for _ in terms)
        with self.connect() as db:
            rows = list(db.execute(
                f"""
                SELECT p.id, p.title, p.revision, p.markdown
                FROM (
                    SELECT t.page_id, SUM(4 * t.title_hits + t.body_hits) AS weight
                    FROM page_terms t JOIN pages scoped ON scoped.id = t.page_id
                    WHERE scoped.space = ? AND t.token IN ({placeholders})
                    GROUP BY t.page_id HAVING COUNT(*) = ?
                    ORDER BY weight DESC, t.page_id LIMIT ?
                ) matches JOIN pages p ON p.id = matches.page_id
                ORDER BY matches.weight DESC, p.id
                """,
                (self.space, *terms, len(terms), limit),
            ))
        return [
            {"id": row["id"], "title": row["title"], "revision": row["revision"],
             "excerpt": excerpt(row["markdown"], terms)}
            for row in rows
        ]
