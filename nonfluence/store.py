"""SQLite storage shared by the browser and MCP."""

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .references import references


class Invalid(ValueError):
    pass


class Missing(LookupError):
    pass


class Conflict(RuntimeError):
    pass


def page_id(value):
    if not isinstance(value, str):
        raise Invalid("Use a UUID for the page ID.")
    try:
        return str(UUID(value))
    except ValueError:
        raise Invalid("Use a UUID for the page ID.") from None


def page_fields(title, markdown):
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160:
        raise Invalid("The title must contain 1 to 160 characters.")
    if not isinstance(markdown, str) or len(markdown) > 100_000:
        raise Invalid("Markdown must contain at most 100,000 characters.")
    return title.strip(), markdown


def validate_scope(space, classification):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", space):
        raise Invalid("Use a space slug with 1 to 64 lowercase letters, digits or hyphens.")
    if classification not in DATABASE_IDS:
        raise Invalid("Unsupported Nonfluence classification.")


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY,
    space TEXT NOT NULL,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    parent_id TEXT REFERENCES pages(id),
    revision INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS page_space_parent ON pages(space, parent_id);
CREATE TABLE IF NOT EXISTS revisions (
    page_id TEXT NOT NULL REFERENCES pages(id),
    revision INTEGER NOT NULL,
    title TEXT NOT NULL,
    markdown TEXT NOT NULL,
    parent_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(page_id, revision)
);
CREATE TABLE IF NOT EXISTS create_requests (
    space TEXT NOT NULL,
    request_id TEXT NOT NULL,
    page_id TEXT NOT NULL REFERENCES pages(id),
    PRIMARY KEY(space, request_id)
);
CREATE TABLE IF NOT EXISTS page_references (
    source_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY(source_id, kind, target_id)
);
CREATE INDEX IF NOT EXISTS reference_target ON page_references(kind, target_id);
CREATE VIRTUAL TABLE IF NOT EXISTS page_search USING fts5(
    title, markdown, content='pages', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS page_insert AFTER INSERT ON pages BEGIN
    INSERT INTO page_search(rowid, title, markdown) VALUES (new.rowid, new.title, new.markdown);
END;
CREATE TRIGGER IF NOT EXISTS page_delete AFTER DELETE ON pages BEGIN
    INSERT INTO page_search(page_search, rowid, title, markdown)
        VALUES ('delete', old.rowid, old.title, old.markdown);
END;
CREATE TRIGGER IF NOT EXISTS page_update AFTER UPDATE OF title, markdown ON pages BEGIN
    INSERT INTO page_search(page_search, rowid, title, markdown)
        VALUES ('delete', old.rowid, old.title, old.markdown);
    INSERT INTO page_search(rowid, title, markdown) VALUES (new.rowid, new.title, new.markdown);
END;
PRAGMA application_id = {application_id};
PRAGMA user_version = 1;
COMMIT;
"""

PAGE_SUMMARY = "id, title, parent_id, revision, updated_at"
DATABASE_IDS = {"private": 1313231702, "confidential": 0x4E46434F}


class Store:
    def __init__(self, path, space, *, write=False, classification="private"):
        validate_scope(space, classification)
        self.path = Path(path).expanduser().resolve()
        self.space = space
        self.write = write
        self.classification = classification
        application_id = DATABASE_IDS[classification]
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0 and not db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                    db.executescript(SCHEMA.format(application_id=application_id))
        with self.connect() as db:
            if (
                db.execute("PRAGMA application_id").fetchone()[0] != application_id
                or db.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise Invalid("The database does not match this Nonfluence classification or schema.")

    @contextmanager
    def connect(self, *, mutate=False):
        if mutate and not self.write:
            raise PermissionError("This Nonfluence session is read-only.")
        mode = "rwc" if self.write else "ro"
        db = sqlite3.connect(f"{self.path.as_uri()}?mode={mode}", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if mutate else "BEGIN")
            with db:
                yield db
        finally:
            db.close()

    def _get(self, db, target):
        row = db.execute("SELECT * FROM pages WHERE id = ? AND space = ?", (target, self.space)).fetchone()
        if row is None:
            raise Missing("Page not found in this space.")
        return row

    def _sources(self, db, kind, target, limit=None):
        sql = """
            SELECT p.id, p.title, p.parent_id, p.revision, p.updated_at
            FROM page_references r JOIN pages p ON p.id = r.source_id
            WHERE p.space = ? AND r.kind = ? AND r.target_id = ?
            ORDER BY p.title COLLATE NOCASE, p.id
        """
        params = (self.space, kind, target)
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [dict(row) for row in db.execute(sql, params)]

    def sources(self, kind, target, limit):
        with self.connect() as db:
            if kind == "page":
                self._get(db, target)
            return self._sources(db, kind, target, limit)

    def list_pages(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                f"SELECT {PAGE_SUMMARY} FROM pages WHERE space = ? ORDER BY title COLLATE NOCASE, id",
                (self.space,),
            )]

    def read(self, target):
        target = page_id(target)
        with self.connect() as db:
            page = dict(self._get(db, target))
            page["backlinks"] = self._sources(db, "page", target)
            refs = [dict(row) for row in db.execute(
                """
                SELECT r.kind, r.target_id, r.label, p.title AS page_title
                FROM page_references r
                LEFT JOIN pages p ON r.kind = 'page' AND p.id = r.target_id AND p.space = ?
                WHERE r.source_id = ?
                ORDER BY r.rowid
                """,
                (self.space, target),
            )]
        return page, refs

    def history(self, target):
        target = page_id(target)
        with self.connect() as db:
            self._get(db, target)
            return [dict(row) for row in db.execute(
                """
                SELECT revision, title, parent_id, updated_at
                FROM revisions WHERE page_id = ? ORDER BY revision DESC
                """,
                (target,),
            )]

    def revision(self, target, revision):
        target = page_id(target)
        with self.connect() as db:
            self._get(db, target)
            row = db.execute(
                "SELECT * FROM revisions WHERE page_id = ? AND revision = ?", (target, revision)
            ).fetchone()
        if row is None:
            raise Missing("Revision not found.")
        return dict(row)

    def search(self, query, limit=20):
        if not isinstance(query, str) or len(query) > 300:
            raise Invalid("Search text must contain at most 300 characters.")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise Invalid("Search limit must be between 1 and 50.")
        terms = re.findall(r"[^\W_]+", query)
        if not terms:
            return []
        match = " AND ".join(f'"{term}"' for term in terms)
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT p.id, p.title, p.revision, snippet(page_search, 1, '', '', ' … ', 18) AS excerpt
                FROM page_search JOIN pages p ON p.rowid = page_search.rowid
                WHERE page_search MATCH ? AND p.space = ?
                ORDER BY bm25(page_search, 4.0, 1.0), p.id LIMIT ?
                """,
                (match, self.space, limit),
            )
            return [dict(row) for row in rows]

    def _parent(self, db, target, parent):
        if parent is None:
            return None
        parent = page_id(parent)
        ancestor = parent
        while ancestor is not None:
            if ancestor == target:
                raise Invalid("A page cannot be nested under itself or its descendants.")
            ancestor = self._get(db, ancestor)["parent_id"]
        return parent

    def _index(self, db, target, refs):
        db.execute(
            """
            INSERT INTO revisions(page_id, revision, title, markdown, parent_id, updated_at)
            SELECT id, revision, title, markdown, parent_id, updated_at FROM pages WHERE id = ?
            """,
            (target,),
        )
        db.execute("DELETE FROM page_references WHERE source_id = ?", (target,))
        db.executemany(
            "INSERT INTO page_references(source_id, kind, target_id, label) VALUES (?, ?, ?, ?)",
            ((target, ref.kind, ref.target_id, ref.label) for ref in refs),
        )

    def _insert(self, db, target, title, markdown, parent):
        parent = self._parent(db, target, parent)
        db.execute(
            """
            INSERT INTO pages(id, space, title, markdown, parent_id, revision, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (target, self.space, title, markdown, parent, timestamp()),
        )
        self._index(db, target, references(markdown))
        return dict(self._get(db, target))

    def create(self, title, markdown, parent=None, request_id=None):
        title, markdown = page_fields(title, markdown)
        parent = page_id(parent) if parent is not None else None
        request_id = page_id(request_id) if request_id is not None else None
        with self.connect(mutate=True) as db:
            if request_id is not None:
                prior = db.execute(
                    """
                    SELECT v.page_id, v.title, v.markdown, v.parent_id
                    FROM create_requests r JOIN revisions v ON r.page_id = v.page_id AND v.revision = 1
                    WHERE r.space = ? AND r.request_id = ?
                    """,
                    (self.space, request_id),
                ).fetchone()
                if prior:
                    if (title, markdown, parent) != (prior["title"], prior["markdown"], prior["parent_id"]):
                        raise Conflict("This request ID was used with different page content.")
                    return dict(self._get(db, prior["page_id"]))
            page = self._insert(db, str(uuid4()), title, markdown, parent)
            if request_id is not None:
                db.execute("INSERT INTO create_requests VALUES (?, ?, ?)", (self.space, request_id, page["id"]))
            return page

    def update(self, target, expected_revision, title, markdown, *, parent=None, move=False):
        target = page_id(target)
        title, markdown = page_fields(title, markdown)
        if type(expected_revision) is not int or expected_revision < 1:
            raise Invalid("Expected revision must be a positive integer.")
        with self.connect(mutate=True) as db:
            existing = self._get(db, target)
            if existing["revision"] != expected_revision:
                raise Conflict("This page has changed. Read the current revision before saving.")
            parent = self._parent(db, target, parent) if move else existing["parent_id"]
            if (title, markdown, parent) == (existing["title"], existing["markdown"], existing["parent_id"]):
                return dict(existing)
            db.execute(
                """
                UPDATE pages SET title = ?, markdown = ?, parent_id = ?, revision = revision + 1,
                    updated_at = ? WHERE id = ? AND space = ?
                """,
                (title, markdown, parent, timestamp(), target, self.space),
            )
            self._index(db, target, references(markdown))
            return dict(self._get(db, target))

    def seed(self, entries):
        with self.connect(mutate=True) as db:
            for target, title, markdown, parent in entries:
                if db.execute("SELECT 1 FROM pages WHERE space = ? AND id = ?", (self.space, target)).fetchone():
                    continue
                self._insert(db, target, title, markdown, parent)
