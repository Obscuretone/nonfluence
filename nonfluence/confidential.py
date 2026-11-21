"""Owner-only OpenID Connect login for an isolated confidential workspace."""

import hmac
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.flask_client import OAuth
from authlib.integrations.base_client.errors import OAuthError
from flask import Blueprint, abort, redirect, request, session, url_for
from joserfc.errors import JoseError
from requests import RequestException

from .sessions import BrowserSessions, configure_cookie_state


COOKIE = "__Host-nonfluence-confidential"
SESSION_DURATION = 15 * 60
AUTH_FRESHNESS = 5 * 60


@dataclass(frozen=True)
class OwnerIdentity:
    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    owner_subject: str
    session_key: str = field(repr=False)

    def __post_init__(self):
        url = urlsplit(self.issuer)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or self.issuer.endswith("/")):
            raise ValueError("Use the exact HTTPS issuer URL from the custom provider.")
        if not self.client_id or not self.client_secret or not self.owner_subject or len(self.session_key) < 32:
            raise ValueError("Set the client ID, client secret, owner subject, and a random session key.")

    @classmethod
    def from_environment(cls, session_key=None):
        credentials = os.getenv("CREDENTIALS_DIRECTORY")
        if credentials:
            secret = (Path(credentials) / "oidc-client-secret").read_text().strip()
            key = session_key or (Path(credentials) / "oidc-session-key").read_text().strip()
        else:
            secret = os.getenv("NONFLUENCE_OIDC_CLIENT_SECRET", "")
            key = session_key or os.getenv("NONFLUENCE_OIDC_SESSION_KEY", "")
        return cls(
            issuer=os.getenv("NONFLUENCE_OIDC_ISSUER", ""),
            client_id=os.getenv("NONFLUENCE_OIDC_CLIENT_ID", ""),
            client_secret=secret,
            owner_subject=os.getenv("NONFLUENCE_OIDC_OWNER_SUBJECT", ""),
            session_key=key,
        )


def owner_oidc(app, identity, host, complete_login, destination, *, max_age=0):
    if not host or urlsplit(identity.issuer).hostname == host:
        raise ValueError("Use a confidential hostname distinct from the identity provider.")
    redirect_uri = f"https://{host}/auth/callback"
    client = OAuth(app).register(
        "workspace_identity",
        client_id=identity.client_id,
        client_secret=identity.client_secret,
        server_metadata_url=identity.issuer + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid", "code_challenge_method": "S256", "default_timeout": 6},
    )
    routes = Blueprint("owner", __name__, url_prefix="/auth")

    @routes.get("/sign-in")
    def sign_in():
        session.clear()
        session["after_oidc"] = destination(request.args.get("next", ""))
        try:
            if client.load_server_metadata().get("issuer") != identity.issuer:
                abort(503, "The identity provider returned an unexpected issuer.")
            return client.authorize_redirect(redirect_uri, max_age=max_age)
        except (OAuthError, RequestException, ValueError):
            abort(503, "Sign-in is temporarily unavailable.")

    @routes.get("/callback")
    def callback():
        try:
            claims = client.authorize_access_token(
                claims_options={
                    "iss": {"essential": True, "value": identity.issuer},
                    "auth_time": {"essential": True},
                },
                leeway=30,
            ).get("userinfo")
        except (OAuthError, JoseError, RequestException, ValueError):
            session.clear()
            abort(401, "Sign-in failed. Start again from the workspace.")
        target = session.pop("after_oidc", "/")
        session.clear()
        now = int(time.time())
        auth_time = claims.get("auth_time") if claims else None
        if (
            not claims or claims.get("iss") != identity.issuer or claims.get("sub") != identity.owner_subject
            or type(auth_time) is not int or not now - max(AUTH_FRESHNESS, max_age) <= auth_time <= now + 30
        ):
            abort(403, "This workspace requires a fresh sign-in from the owner.")
        return complete_login(target, claims)

    return routes


def protect_workspace(app, identity, host, db_path):
    configure_cookie_state(app, "__Host-nonfluence-oidc", identity.session_key, AUTH_FRESHNESS)
    sessions = BrowserSessions(db_path, identity.session_key, SESSION_DURATION)
    owner = identity.issuer + "\n" + identity.owner_subject

    def complete_login(target, claims):
        sessions.revoke(request.cookies.get(COOKIE))
        response = redirect(target, code=303)
        response.set_cookie(
            COOKIE, sessions.issue(owner), max_age=SESSION_DURATION, secure=True, httponly=True, samesite="Lax",
        )
        return response

    routes = owner_oidc(app, identity, host, complete_login, lambda _: "/")

    def logout_token():
        return sessions.logout_token(request.cookies.get(COOKIE, ""))

    @app.before_request
    def require_owner():
        if not request.is_secure:
            abort(403)
        if request.endpoint in {"owner.sign_in", "owner.callback"}:
            return
        if sessions.valid(owner, request.cookies.get(COOKIE)):
            return
        if request.endpoint == "workspace":
            return redirect(url_for("owner.sign_in"), code=303)
        abort(401)

    @app.context_processor
    def owner_logout_token():
        return {"owner_logout_token": logout_token(), "sign_out_url": url_for("owner.sign_out")}

    @routes.post("/sign-out")
    def sign_out():
        provided = request.form.get("logout_token", "")
        if not provided.isascii() or not hmac.compare_digest(logout_token(), provided):
            abort(403)
        sessions.revoke(request.cookies.get(COOKIE))
        session.clear()
        response = app.make_response(("Signed out. Close this tab to finish.", 200))
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="Lax")
        return response

    app.register_blueprint(routes)
