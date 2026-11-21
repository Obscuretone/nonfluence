import asyncio
import sqlite3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from nonfluence.__main__ import DEMO, GUIDE, RUNBOOK
from nonfluence.nira import ReadOnlyNira
from nonfluence.references import FixtureNira, references, render, resolve
from nonfluence.store import Conflict, Invalid, Missing, Store
from nonfluence.web import create_app


class NonfluenceTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "pages with spaces.sqlite3"
        self.store = Store(self.path, "personal", write=True)
        self.store.seed(DEMO)

    def test_rename_and_move_preserve_links_and_history(self):
        initial, _ = self.store.read(RUNBOOK)
        revision = self.store.update(
            RUNBOOK, 1, "Restore guide", "Instructions for récupération.", move=True,
        )
        self.assertEqual(revision["id"], initial["id"])
        self.assertIsNone(revision["parent_id"])
        self.assertEqual(revision["revision"], 2)
        guide, rows = self.store.read(GUIDE)
        refs = resolve(rows, FixtureNira())
        self.assertEqual(refs[0]["title"], "Restore guide")
        self.assertIn(f'href="/pages/{RUNBOOK}"', render(guide["markdown"], refs))
        backlink, = self.store.sources("page", RUNBOOK, 10)
        self.assertEqual(backlink["id"], GUIDE)
        self.assertEqual(self.store.sources("page", GUIDE, 10), [])
        self.assertEqual(self.store.revision(RUNBOOK, 1)["title"], "Database runbook")
        self.assertEqual(self.store.search("recuperation")[0]["id"], RUNBOOK)
        self.assertEqual(self.store.search("checklist"), [])
        with self.assertRaises(Conflict):
            self.store.update(RUNBOOK, 1, "Stale edit", "")
        with self.assertRaises(Invalid):
            self.store.update(RUNBOOK, 2, "Try a cycle", "", parent=RUNBOOK, move=True)

    def test_retry_and_concurrent_edits(self):
        request_id = str(uuid4())
        with ThreadPoolExecutor(max_workers=2) as workers:
            submissions = list(workers.map(
                lambda _: self.store.create("Idempotent", "Alpha article", request_id=request_id), range(2),
            ))
        self.assertEqual(submissions[0]["id"], submissions[1]["id"])
        target = submissions[0]["id"]
        with self.assertRaises(Conflict):
            self.store.create("Changed payload", "", request_id=request_id)

        def attempt(title):
            try:
                return self.store.update(target, 1, title, "")["revision"]
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(attempt, ("One", "Two")))
        self.assertCountEqual(outcomes, [2, None])
        self.assertEqual(len(self.store.history(target)), 2)
        self.assertEqual(self.store.search("alpha"), [])

    def test_scope_applies_everywhere(self):
        other = Store(self.path, "private", write=True)
        hidden = other.create("Private record", f"PrivateKeyword [guide](page:{GUIDE})")
        target = hidden["id"]
        self.assertEqual(self.store.search("PrivateKeyword"), [])
        self.assertEqual([page["id"] for page in self.store.sources("page", GUIDE, 10)], [RUNBOOK])
        self.assertTrue(all(page["id"] != target for page in self.store.list_pages()))
        with self.assertRaises(Missing):
            self.store.read(target)
        with self.assertRaises(Missing):
            self.store.history(target)
        with self.assertRaises(Missing):
            self.store.create("Wrong parent", "", parent=target)
        with self.assertRaises(Missing):
            self.store.update(target, 1, "Wrong space", "")
        with self.assertRaises(Invalid):
            self.store.update(GUIDE, 1, "Cycle", "", parent=RUNBOOK, move=True)
        source = self.store.create("Scoped source", f"[Hidden](page:{target})")
        _, rows = self.store.read(source["id"])
        self.assertEqual(resolve(rows)[0]["state"], "broken")
        readonly = Store(self.path, "personal")
        with self.assertRaises(PermissionError):
            readonly.create("Forbidden", "")
        with readonly.connect() as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM pages")

    def test_typed_references_follow_markdown_parsing(self):
        body = f"""
[Guide *page*][guide] and [Duplicate](page:{GUIDE}).

[Ticket](nira:TEAM-OPS-23) and [Example](nira:NIRA-142)

```
[Code-only link](nira:NIRA-999)
```

<script>raw html</script>

[guide]: page:{GUIDE}
"""
        matches = references(body)
        self.assertEqual([(ref.kind, ref.target_id) for ref in matches], [
            ("page", GUIDE), ("nira", "TEAM-OPS-23"), ("nira", "NIRA-142"),
        ])
        self.assertEqual(matches[0].label, "Guide page")
        html = render(body)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="nira:', html)

    def test_readonly_nira_reads_committed_status_and_uses_stable_id(self):
        nira_path = self.path.with_name("disposable nira.sqlite3")
        with closing(sqlite3.connect(nira_path)) as fixture:
            fixture.executescript(
                """
                PRAGMA user_version = 2;
                CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE tickets(id INTEGER PRIMARY KEY, number INTEGER UNIQUE, title TEXT,
                    status TEXT, body_md TEXT);
                INSERT INTO settings VALUES ('default_project', 'TEAM-OPS');
                INSERT INTO tickets VALUES (9, 31, 'Add a backup', 'in_progress', 'Local test description.');
                """
            )
            adapter = ReadOnlyNira(nira_path, "http://localhost:8765")
            first = adapter.resolve_many({"9", "TEAM-OPS-31", "WRONG-31", "31"})
            self.assertEqual(first["9"]["status"], "In progress")
            self.assertEqual(first["9"]["stable_id"], "9")
            self.assertEqual(first["9"]["url"], "http://localhost:8765/tickets/TEAM-OPS-31")
            self.assertEqual(first["9"], first["TEAM-OPS-31"])
            self.assertEqual(first["WRONG-31"]["state"], "broken")
            self.assertEqual(first["31"]["state"], "broken")
            fixture.execute("UPDATE settings SET value = 'NEW' WHERE key = 'default_project'")
            fixture.execute("UPDATE tickets SET status = 'closed' WHERE id = 9")
            fixture.commit()
            second = adapter.resolve_many({"9", "TEAM-OPS-31"})
            self.assertEqual(second["9"]["status"], "Closed")
            self.assertEqual(second["9"]["display_key"], "NEW-31")
            self.assertEqual(second["TEAM-OPS-31"]["state"], "broken")
        missing_path = nira_path.with_name("missing.sqlite3")
        self.assertEqual(ReadOnlyNira(missing_path).resolve_many({"9"}), {})
        self.assertFalse(missing_path.exists())

    def test_http_editor_preview_and_write_scope(self):
        client = create_app(self.store, FixtureNira()).test_client()
        origin = {"Origin": "http://localhost"}
        self.assertEqual(client.get("/").status_code, 200)
        with client.get("/static/workspace.js") as script:
            self.assertEqual(script.status_code, 200)
        self.assertEqual(client.get("/", headers={"Host": "other.example"}).status_code, 400)
        self.assertEqual(client.post("/api/pages", json={}).status_code, 403)
        preview = client.post("/api/preview", json={"markdown": "## Test"}, headers=origin)
        self.assertIn("<h2>Test</h2>", preview.json["html"])
        response = client.post(
            "/api/pages", json={"title": "New", "markdown": "unique passage", "parent_id": GUIDE}, headers=origin,
        )
        self.assertEqual(response.status_code, 201)
        target = response.json["id"]
        self.assertEqual(client.get("/api/search?q=unique").json["pages"][0]["id"], target)
        self.assertEqual(client.get(f"/api/pages/{target}/history/1").json["title"], "New")
        self.assertEqual(client.get(f"/api/pages/{GUIDE}").json["references"][1]["status"], "In progress")
        readonly = create_app(Store(self.path, "personal"), None).test_client()
        self.assertEqual(readonly.post("/api/pages", json={"title": "No", "markdown": ""}, headers=origin).status_code, 403)

    def test_real_stdio_mcp(self):
        other = Store(self.path, "private", write=True).create("Secret title", "Never searchable")

        async def exercise():
            base = ["-B", "-m", "nonfluence", "--db", str(self.path), "--fixture-nira", "mcp"]
            first = StdioServerParameters(command=sys.executable, args=base)
            async with stdio_client(first) as (rx, tx):
                async with ClientSession(rx, tx) as session:
                    info = await session.initialize()
                    self.assertEqual(info.serverInfo.name, "Nonfluence")
                    tools = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertNotIn("create_page", tools)
                    denied = await session.call_tool("create_page", {"title": "Denied", "markdown": ""})
                    self.assertTrue(denied.isError)
                    read = await session.call_tool("read_page", {"id": GUIDE})
                    self.assertFalse(read.isError, read.content)
                    self.assertEqual(read.structuredContent["references"][1]["status"], "In progress")
                    related = await session.call_tool("find_backlinks", {"reference": "nira:NIRA-142"})
                    self.assertEqual(related.structuredContent["pages"][0]["id"], GUIDE)
                    hidden = await session.call_tool("read_page", {"id": other["id"]})
                    self.assertTrue(hidden.isError)

            second = StdioServerParameters(command=sys.executable, args=[*base, "--write"])
            async with stdio_client(second) as (rx, tx):
                async with ClientSession(rx, tx) as session:
                    await session.initialize()
                    created = await session.call_tool("create_page", {"title": "MCP page", "markdown": "Quasar"})
                    self.assertFalse(created.isError)
                    target = created.structuredContent["id"]
                    changed = await session.call_tool("update_page", {
                        "id": target, "expected_revision": 1, "title": "Renamed", "markdown": "Quasar",
                    })
                    self.assertEqual(changed.structuredContent["revision"], 2)
                    hits = await session.call_tool("search_pages", {"query": "Quasar"})
                    self.assertEqual(hits.structuredContent["pages"][0]["id"], target)
                    hidden = await session.call_tool("update_page", {
                        "id": other["id"], "expected_revision": 1, "title": "Wrong", "markdown": "",
                    })
                    self.assertTrue(hidden.isError)

        asyncio.run(asyncio.wait_for(exercise(), 20))


if __name__ == "__main__":
    unittest.main()
