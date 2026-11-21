"""Run the local browser, stdio MCP server, or fixture seed."""

import argparse
import sqlite3
from pathlib import Path

from .nira import ReadOnlyNira
from .nira_hq import ReadOnlyNiraHQ
from .references import FixtureNira
from .storage import open_store


GUIDE = "256e0c86-8e31-4f37-9e0b-f66d4d428e12"
RUNBOOK = "1ab3b1df-506b-444c-9644-24554499d1be"
DEMO = (
    (
        GUIDE, "Infrastructure architecture",
        f"""Nonfluence keeps infrastructure notes and runbooks close to the work they describe.

## Service map

| Service | Role |
| --- | --- |
| Nira | Track the work |
| Nonfluence | Document how it works |

## Database

Follow the [Database runbook](page:{RUNBOOK}) for the restore procedure. The example ticket
[NIRA-142](nira:NIRA-142) tracks database host setup. The example [monitoring pull request](https://github.com/example/platform/pull/28)
shows how a GitHub URL becomes a typed reference.

## Next review

- Confirm the restore procedure against a disposable database.
- Record the outcome with the runbook.
""",
        None,
    ),
    (
        RUNBOOK, "Database runbook",
        f"""Use a disposable database to verify that an encrypted backup can be restored.

## Restore checklist

1. Select a backup and record its timestamp.
2. Restore into an isolated test database.
3. Verify the schema, expected records, and application connectivity.
4. Record the elapsed time against the recovery objective.

See [Infrastructure architecture](page:{GUIDE}) for service ownership.
""",
        GUIDE,
    ),
)


def main():
    parser = argparse.ArgumentParser(prog="python -m nonfluence")
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--db", type=Path, default=Path(".nonfluence/pages.sqlite3"))
    storage.add_argument("--mariadb-config", type=Path)
    parser.add_argument("--space", default="personal", help="space slug for this connection")
    nira_source = parser.add_mutually_exclusive_group()
    nira_source.add_argument("--fixture-nira", action="store_true", help="resolve illustrative Nira tickets offline")
    nira_source.add_argument("--nira-db", type=Path, help="read an authorized standalone Nira v2 SQLite database")
    parser.add_argument("--nira-web-url", help="optional standalone Nira base URL for clickable ticket links")
    parser.add_argument("--nira-hq-token-file", type=Path, help="file holding an authorized Nira HQ read-only token")
    commands = parser.add_subparsers(dest="command", required=True)
    web = commands.add_parser("serve", help="start the local browser interface")
    web.add_argument("--port", type=int, default=8787)
    web.add_argument("--read-only", action="store_true")
    mcp = commands.add_parser("mcp", help="start the stdio MCP server")
    mcp.add_argument("--write", action="store_true", help="enable write tools for the configured space")
    commands.add_parser("seed", help="add example pages to a local database")
    args = parser.parse_args()
    if args.nira_web_url and not args.nira_db:
        parser.error("--nira-web-url requires --nira-db.")
    if args.nira_db and not args.mariadb_config and args.db.expanduser().resolve() == args.nira_db.expanduser().resolve():
        parser.error("Choose a different database for Nonfluence.")
    if args.command == "seed" and args.space != "personal":
        parser.error("The fixture pages use the personal space.")
    write = args.command == "seed" or (
        args.command == "serve" and not args.read_only
    ) or (args.command == "mcp" and args.write)
    try:
        nira = FixtureNira() if args.fixture_nira else None
        if args.nira_db:
            nira = ReadOnlyNira(args.nira_db, args.nira_web_url)
        nira_hq = ReadOnlyNiraHQ(args.nira_hq_token_file) if args.nira_hq_token_file else None
        store = open_store(args.db, args.mariadb_config, args.space, write=write)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.error(str(error))
    if args.command == "seed":
        store.seed(DEMO)
        print("Example pages are ready.")
    elif args.command == "serve":
        from .web import create_app

        create_app(store, nira, nira_hq=nira_hq).run(host="127.0.0.1", port=args.port, threaded=False)
    else:
        from .mcp_server import make_server

        make_server(store, nira, nira_hq=nira_hq).run(transport="stdio")


if __name__ == "__main__":
    main()
