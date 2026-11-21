"""Browser sign-in for the existing private workspace."""

import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from .sessions import BrowserSessions, configure_cookie_state


COOKIE = "__Host-nonfluence"
MAX_AGE = 8 * 60 * 60
PASSKEY_AGE = 5 * 60
PAGE_PATH = re.compile(r"/pages/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
SIGN_IN_ASSETS = {"sign-in.css", "obscuretone.svg", "favicon.ico", "apple-touch-icon.png"}


@dataclass(frozen=True)
class PasswordLogin:
    username: str
    password_hash: str = field(repr=False)
    session_key: str = field(repr=False)

    def __post_init__(self):
        if not self.username or not self.password_hash.startswith("pbkdf2:sha256:1000000$") or len(self.session_key) < 32:
            raise ValueError("The browser login credential is invalid.")

    @classmethod
    def from_credentials(cls):
        directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if not directory:
            raise ValueError("Supply the browser login via systemd credentials.")
        entry = json.loads((Path(directory) / "browser-login").read_text())
        return cls(**entry)


def destination(path):
    return path if path and PAGE_PATH.fullmatch(path) else "/"


def valid_csrf(expected, provided):
    return bool(expected and provided and provided.isascii() and hmac.compare_digest(expected, provided))


def protect_workspace(app, login, db_path, *, identity=None, host=None):
    configure_cookie_state(app, "__Host-nonfluence-login", login.session_key, lifetime=600)
    app.config["MAX_FORM_MEMORY_SIZE"] = 16_384
    browser_sessions = BrowserSessions(db_path, login.session_key, MAX_AGE)
    routes = Blueprint("browser_login", __name__)

    def complete_login(target, claims=None):
        duration = MAX_AGE if claims is None else min(PASSKEY_AGE, int(claims["exp"]) - int(time.time()))
        if duration <= 0:
            abort(401, "The identity session expired. Sign in again.")
        browser_sessions.revoke(request.cookies.get(COOKIE))
        response = redirect(target, code=303)
        response.set_cookie(
            COOKIE, browser_sessions.issue(login.username, duration), max_age=duration,
            secure=True, httponly=True, samesite="Lax",
        )
        return response

    if identity:
        from .confidential import owner_oidc

        app.register_blueprint(owner_oidc(app, identity, host, complete_login, destination, max_age=3600))

    def logout_token():
        return browser_sessions.logout_token(request.cookies.get(COOKIE, ""))

    @app.before_request
    def require_login():
        if not request.is_secure:
            abort(403)
        if request.endpoint == "browser_login.sign_in":
            return
        if identity and request.endpoint in {"owner.sign_in", "owner.callback"}:
            return
        if request.endpoint == "static" and request.view_args["filename"] in SIGN_IN_ASSETS:
            return
        if request.path.startswith("/api/") and request.headers.get("X-Nonfluence-Verified-User") == login.username:
            return
        if browser_sessions.valid(login.username, request.cookies.get(COOKIE)):
            return
        if request.endpoint == "workspace":
            return redirect(url_for("browser_login.sign_in", next=request.path), code=303)
        abort(401, "Sign in to Nonfluence.")

    @app.context_processor
    def logout_form_fields():
        return {"owner_logout_token": logout_token(), "sign_out_url": url_for("browser_login.sign_out")}

    @routes.route("/login", methods=["GET", "POST"], strict_slashes=False)
    def sign_in():
        target = destination(request.values.get("next", ""))
        if browser_sessions.valid(login.username, request.cookies.get(COOKIE)):
            return redirect(target, code=303)
        token = session.get("login_csrf") or secrets.token_urlsafe(24)
        error = None
        status = 200
        if request.method == "POST":
            if not valid_csrf(session.get("login_csrf"), request.form.get("csrf", "")):
                error = "This sign-in form expired. Try again."
                status = 400
            else:
                username = request.form.get("username", "")
                password = request.form.get("password", "")
                if len(password) <= 512 and check_password_hash(login.password_hash, password):
                    if hmac.compare_digest(login.username.encode(), username.encode()):
                        session.clear()
                        return complete_login(target)
                error = "The username or password did not match."
                status = 401
        session["login_csrf"] = token
        passkey_url = url_for("owner.sign_in", next=target) if identity else None
        return render_template(
            "sign-in.html", csrf=token, next_path=target, error=error, passkey_url=passkey_url,
        ), status

    @routes.post("/logout")
    def sign_out():
        if not valid_csrf(logout_token(), request.form.get("logout_token", "")):
            abort(403)
        browser_sessions.revoke(request.cookies.get(COOKIE))
        session.clear()
        response = redirect(url_for("browser_login.sign_in"), code=303)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="Lax")
        return response

    app.register_blueprint(routes)
