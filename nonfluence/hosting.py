"""Serve one space on a private socket behind the VPS's authenticated TLS proxy."""

import argparse
import os
import re
from pathlib import Path

from waitress import serve
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from .references import nira_hq_url
from .storage import open_store
from .web import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, help="external TLS hostname")
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--db", type=Path)
    storage.add_argument("--mariadb-config", type=Path)
    parser.add_argument("--space", default="personal")
    parser.add_argument("--plugins-mariadb-config", type=Path, help="mount the plugin company workspace")
    parser.add_argument("--socket", required=True, type=Path)
    login_mode = parser.add_mutually_exclusive_group(required=True)
    login_mode.add_argument("--confidential", action="store_true", help="require the owner to sign in through custom SSO")
    login_mode.add_argument("--browser-login", action="store_true", help="enable the private workspace login form")
    parser.add_argument("--confluence-credentials", type=Path, help="private Confluence API token file")
    parser.add_argument("--owner-credentials", type=Path, help="private standalone browser credential file")
    parser.add_argument("--owner-setup", type=Path, help="private one-time setup token file")
    parser.add_argument("--browser-oidc", action="store_true", help="offer the shared passkey provider on browser sign-in")
    args = parser.parse_args()
    nira_hq_credential = os.getenv("NONFLUENCE_NIRA_HQ_TOKEN_FILE")
    if not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)+", args.host):
        parser.error("Provide an exact DNS hostname.")
    auth = None
    browser_auth = None
    browser_signup = None
    if args.owner_setup and (not args.owner_credentials or args.browser_oidc or args.plugins_mariadb_config):
        parser.error("--owner-setup requires --owner-credentials and a standalone workspace.")
    if args.owner_credentials and not args.browser_login:
        parser.error("--owner-credentials requires --browser-login.")
    browser_identity = None
    classification = "private"
    if args.confidential:
        from .confidential import OwnerIdentity

        try:
            auth = OwnerIdentity.from_environment()
        except (OSError, ValueError) as error:
            parser.error(str(error))
        classification = "confidential"
    try:
        nira_url = nira_hq_url(os.getenv("NONFLUENCE_NIRA_HQ_URL")) if classification == "private" else None
    except ValueError as error:
        parser.error(str(error))
    nira_hq = None
    if nira_hq_credential and classification == "private":
        from .nira_hq import ReadOnlyNiraHQ

        try:
            nira_hq = ReadOnlyNiraHQ(nira_hq_credential, nira_url)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    if args.browser_login:
        from .browser_login import OwnerSignup, PasswordLogin

        try:
            if args.owner_setup:
                browser_signup = OwnerSignup(args.owner_setup, args.owner_credentials)
            elif args.owner_credentials:
                browser_auth = PasswordLogin.from_file(args.owner_credentials)
            else:
                browser_auth = PasswordLogin.from_credentials()
        except (OSError, TypeError, ValueError) as error:
            parser.error(str(error))
    if args.browser_oidc:
        from .confidential import OwnerIdentity

        if browser_auth is None:
            parser.error("--browser-oidc requires --browser-login.")
        try:
            browser_identity = OwnerIdentity.from_environment(session_key=browser_auth.session_key)
        except (OSError, ValueError) as error:
            parser.error(str(error))
    try:
        store = open_store(args.db, args.mariadb_config, args.space, write=True, classification=classification, nira_url=nira_url)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.plugins_mariadb_config and (not browser_auth or args.space != "personal" or not args.mariadb_config):
        parser.error("The plugin company workspace requires the hosted personal MariaDB workspace.")
    confluence_credentials = None
    if args.confluence_credentials:
        from .confluence import ConfluenceCredentials

        if args.confidential:
            parser.error("Confluence API access is only available for the private workspace.")
        try:
            confluence_credentials = ConfluenceCredentials.from_file(args.confluence_credentials)
        except (OSError, TypeError, ValueError) as error:
            parser.error(str(error))
    prefix = "/plugins" if args.plugins_mariadb_config else ""
    application = create_app(store, None, nira_hq=nira_hq, proxy_host=args.host, confidential_auth=auth,
                             browser_auth=browser_auth, browser_identity=browser_identity, workspace_prefix=prefix,
                             browser_signup=browser_signup, confluence_credentials=confluence_credentials)
    if args.plugins_mariadb_config:
        try:
            plugins = open_store(None, args.plugins_mariadb_config, "plugins", write=True, nira_url=nira_url)
        except (OSError, ValueError) as error:
            parser.error(str(error))
        if plugins.config["database"] == store.config["database"]:
            parser.error("The plugin company workspace requires its own database.")
        company = create_app(plugins, None, nira_hq=nira_hq, proxy_host=args.host, browser_auth=browser_auth,
                             workspace_prefix=prefix, browser_session_store=store)
        application = DispatcherMiddleware(application, {prefix: company})
    serve(
        application,
        unix_socket=str(args.socket), unix_socket_perms="660", url_scheme="https",
        threads=2, connection_limit=40, channel_timeout=20, max_request_body_size=1_250_000,
        expose_tracebacks=False,
    )


if __name__ == "__main__":
    main()
