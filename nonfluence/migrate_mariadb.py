"""Copy and verify a stopped workspace in an empty MariaDB database."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .mariadb_store import MariaStore, term_counts
from .store import DATABASE_IDS, Invalid


# Explicit columns keep the original keys, timestamps and link order.
TABLES = {
    "pages": ("id, space, title, markdown, parent_id, revision, updated_at", "id"),
    "revisions": ("page_id, revision, title, markdown, parent_id, updated_at", "page_id, revision"),
    "create_requests": ("space, request_id, page_id", "space, request_id"),
    "page_references": ("rowid, source_id, kind, target_id, label", "rowid"),
    "owner_sessions": ("digest, owner, expires", "digest"),
}


def snapshot(path, classification):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        if db.execute("PRAGMA application_id").fetchone() != (DATABASE_IDS[classification],):
            raise Invalid("The SQLite source has the wrong workspace classification.")
        if db.execute("PRAGMA user_version").fetchone() != (1,):
            raise Invalid("Unsupported SQLite schema version.")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise Invalid("The SQLite source failed integrity verification.")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise Invalid("The SQLite source contains inconsistent relationships.")
        rows = {
            table: db.execute(f"SELECT {columns} FROM {table} ORDER BY {order}").fetchall()
            for table, (columns, order) in TABLES.items() if table != "owner_sessions"
        }
    # An old session snapshot can contain cookies revoked after the backup.
    rows["owner_sessions"] = []
    return rows


def insert(db, table, rows):
    columns = TABLES[table][0]
    params = ", ".join("?" for _ in columns.split(","))
    if rows:
        db.executemany(f"INSERT INTO {table} ({columns}) VALUES ({params})", rows)


def copy(source, store):
    rows = snapshot(source, store.classification)
    with store.connect(mutate=True) as db:
        occupied = any(db.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() for name in TABLES)
        if not occupied:
            insert(db, "pages", (
                (key, space, title, markdown, None, revision, updated)
                for key, space, title, markdown, parent, revision, updated in rows["pages"]
            ))
            for key, space, title, markdown, parent, revision, updated in rows["pages"]:
                if parent is not None:
                    db.execute("UPDATE pages SET parent_id = ? WHERE id = ?", (parent, key))
                store.index_terms(db, key, title, markdown)
            for table in ("revisions", "create_requests", "page_references"):
                insert(db, table, rows[table])
        for table, (columns, order) in TABLES.items():
            saved = [tuple(row.values()) for row in db.execute(f"SELECT {columns} FROM {table} ORDER BY {order}")]
            if saved != rows[table]:
                raise Invalid(f"Verification failed for {table}. The target was not overwritten.")
        expected_terms = set()
        for key, _, title, markdown, _, _, _ in rows["pages"]:
            title_counts = term_counts(title)
            body_counts = term_counts(markdown)
            for token in title_counts.keys() | body_counts.keys():
                expected_terms.add((key, token, title_counts[token], body_counts[token]))
        saved_terms = {
            (row["page_id"], row["token"], row["title_hits"], row["body_hits"])
            for row in db.execute("SELECT page_id, token, title_hits, body_hits FROM page_terms")
        }
        if saved_terms != expected_terms:
            raise Invalid("Verification failed for the MariaDB search index.")
    return {**{table: len(records) for table, records in rows.items()}, "indexed_terms": len(expected_terms)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--mariadb-config", type=Path, required=True)
    parser.add_argument("--classification", choices=DATABASE_IDS, default="private")
    args = parser.parse_args()
    store = MariaStore(args.mariadb_config, "personal", write=True, classification=args.classification)
    print(json.dumps(copy(args.sqlite, store)))


if __name__ == "__main__":
    main()
