"""Select a local SQLite fixture or the configured hosted MariaDB workspace."""

from pathlib import Path

from .store import Invalid, Store

HOSTED_SQLITE = Path("/var/lib/nonfluence/pages.sqlite3")
HOSTED_CONFIG = HOSTED_SQLITE.with_name("mariadb.json")


def open_store(path, config, space, *, write=False, classification="private", nira_url=None):
    # Previously saved private MCP commands named the old hosted SQLite path.
    # Route that exact path through the same configured backend as the web service.
    if config is None and path is not None and Path(path).expanduser().resolve() == HOSTED_SQLITE:
        if not HOSTED_CONFIG.is_file():
            raise Invalid("Hosted MariaDB configuration is missing. Use a separate path for SQLite recovery.")
        config = HOSTED_CONFIG
    if config is None:
        return Store(path, space, write=write, classification=classification, nira_url=nira_url)
    try:
        from pymysql import MySQLError
        from .mariadb_store import MariaStore
    except ImportError:
        raise Invalid("Install the MariaDB driver for hosted storage.") from None
    try:
        return MariaStore(config, space, write=write, classification=classification, nira_url=nira_url)
    except MySQLError:
        raise Invalid("Could not open the configured MariaDB workspace.") from None
