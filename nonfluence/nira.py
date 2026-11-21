"""Read the standalone Nira v2 SQLite schema through an explicitly configured source."""

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote, urlsplit


class ReadOnlyNira:
    provider = "nira"

    def __init__(self, path, web_url=None):
        self.path = Path(path).expanduser().resolve()
        self.web_url = None
        if web_url:
            url = urlsplit(web_url)
            allowed = url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1"))
            if not allowed or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("Use an HTTPS Nira web URL or a loopback HTTP URL.")
            self.web_url = web_url.rstrip("/")

    def resolve_many(self, keys):
        try:
            with closing(sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=1)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only = ON")
                db.execute("BEGIN")
                if db.execute("PRAGMA user_version").fetchone()[0] != 2:
                    return {}
                project = db.execute("SELECT value FROM settings WHERE key = 'default_project'").fetchone()
                if not project:
                    return {}
                return {key: self._lookup(db, project["value"], key) for key in keys}
        except sqlite3.Error:
            return {}

    def _lookup(self, db, project, reference):
        prefix, separator, number = reference.rpartition("-")
        ticket = None
        if not separator or prefix == project:
            column = "number" if separator else "id"
            ticket = db.execute(
                f"SELECT id, number, title, status, substr(body_md, 1, 240) AS preview "
                f"FROM tickets WHERE {column} = ?",
                (int(number),),
            ).fetchone()
        if ticket is None:
            return {"state": "broken", "provider": self.provider}
        display_key = f"{project}-{ticket['number']}"
        details = {
            "state": "resolved",
            "provider": self.provider,
            "stable_id": str(ticket["id"]),
            "display_key": display_key,
            "title": ticket["title"],
            "status": ticket["status"].replace("_", " ").capitalize(),
            "preview": ticket["preview"],
        }
        if self.web_url:
            details["url"] = f"{self.web_url}/tickets/{quote(display_key, safe='')}"
        return details
