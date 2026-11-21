"""Browser sign-in for the existing private workspace."""

import hmac
import json
import os
import re
import secrets
import time
from tempfile import NamedTemporaryFile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from flask import Blueprint, abort, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

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
    allow_proxy_identity: bool = True

    def __post_init__(self):
        if not self.username or not self.password_hash.startswith("pbkdf2:sha256:1000000$") or len(self.session_key) < 32:
            raise ValueError("The browser login credential is invalid.")

    @classmethod
    def from_credentials(cls):
        directory = os.environ.get("CREDENTIALS_DIRECTORY")
        if not directory:
            raise ValueError("Supply the browser login via systemd credentials.")
        return cls.from_file(Path(directory) / "browser-login", allow_proxy_identity=True)

    @classmethod
    def from_file(cls, path, *, allow_proxy_identity=False):
        entry = json.loads(path.read_text())
        return cls(**entry, allow_proxy_identity=allow_proxy_identity)


class OwnerSignup:
    def __init__(self, token_file, credentials):
        self.token = token_file.read_text().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", self.token):
            raise ValueError("Owner signup requires a random setup token.")
        self.credentials = credentials
        self.session_key = hmac.digest(self.token.encode(), b"nonfluence owner session", "sha256").hex()
        existing = self.account()
        if existing is not None:
            self.session_key = existing.session_key

    def account(self):
        try:
            return PasswordLogin.from_file(self.credentials)
        except FileNotFoundError:
            return None

    def create(self, email, password):
        entry = {
            "username": email,
            "password_hash": generate_password_hash(password, method="pbkdf2:sha256:1000000"),
            "session_key": self.session_key,
        }
        with NamedTemporaryFile(mode="w", dir=self.credentials.parent, prefix=".owner-", delete=False) as file:
            temporary = Path(file.name)
            try:
                json.dump(entry, file)
                file.flush()
                os.fsync(file.fileno())
                os.link(temporary, self.credentials)
            finally:
                temporary.unlink(missing_ok=True)
        return PasswordLogin(**entry)


def destination(path, workspace_prefix=""):
    if path and PAGE_PATH.fullmatch(path):
        return path
    if workspace_prefix and path and path.startswith(workspace_prefix):
        nested = path[len(workspace_prefix):]
        if nested == "/" or PAGE_PATH.fullmatch(nested):
            return path
    return "/"


def valid_csrf(expected, provided):
    return bool(expected and provided and provided.isascii() and hmac.compare_digest(expected, provided))


def protect_workspace(app, login, db_path, *, identity=None, host=None, workspace_prefix="", signup=None):
    session_key = signup.session_key if signup is not None else login.session_key

    def account():
        return signup.account() if signup is not None else login
    configure_cookie_state(app, "__Host-nonfluence-login", session_key, lifetime=600)
    app.config["MAX_FORM_MEMORY_SIZE"] = 16_384
    browser_sessions = BrowserSessions(db_path, session_key, MAX_AGE)
    routes = Blueprint("browser_login", __name__)

    def next_page(target):
        return destination(target, workspace_prefix)

    def complete_login(target, claims=None):
        duration = MAX_AGE if claims is None else min(PASSKEY_AGE, int(claims["exp"]) - int(time.time()))
        if duration <= 0:
            abort(401, "The identity session expired. Sign in again.")
        owner = account()
        if owner is None:
            abort(403)
        browser_sessions.revoke(request.cookies.get(COOKIE))
        response = redirect(target, code=303)
        response.set_cookie(
            COOKIE, browser_sessions.issue(owner.username, duration), max_age=duration,
            secure=True, httponly=True, samesite="Lax",
        )
        return response

    if identity:
        from .confidential import owner_oidc

        app.register_blueprint(owner_oidc(app, identity, host, complete_login, next_page, max_age=3600))

    def logout_token():
        return browser_sessions.logout_token(request.cookies.get(COOKIE, ""))

    @app.before_request
    def require_login():
        if not request.is_secure:
            abort(403)
        if request.blueprint == "confluence":
            return
        if request.endpoint in {"browser_login.sign_in", "browser_login.sign_up"}:
            return
        if identity and request.endpoint in {"owner.sign_in", "owner.callback"}:
            return
        if request.endpoint == "static" and request.view_args["filename"] in SIGN_IN_ASSETS:
            return
        owner = account()
        if owner and browser_sessions.valid(owner.username, request.cookies.get(COOKIE)):
            return
        if owner and owner.allow_proxy_identity and request.path.startswith("/api/") and (
            request.headers.get("X-Nonfluence-Verified-User") == owner.username
        ):
            return
        if request.endpoint == "workspace":
            return redirect(url_for("browser_login.sign_in", next=request.path), code=303)
        abort(401, "Sign in to Nonfluence.")

    @app.context_processor
    def logout_form_fields():
        return {"owner_logout_token": logout_token(), "sign_out_url": url_for("browser_login.sign_out")}

    @routes.route("/login", methods=["GET", "POST"], strict_slashes=False)
    def sign_in():
        target = next_page(request.values.get("next", ""))
        owner = account()
        if owner is None:
            return redirect(url_for("browser_login.sign_up", next=target), code=303)
        if browser_sessions.valid(owner.username, request.cookies.get(COOKIE)):
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
                email_login = "@" in owner.username
                if email_login:
                    username = username.strip().lower()
                if len(password) <= 512 and check_password_hash(owner.password_hash, password):
                    if hmac.compare_digest(owner.username.encode(), username.encode()):
                        session.clear()
                        return complete_login(target)
                error = "The username or password did not match."
                status = 401
        session["login_csrf"] = token
        passkey_url = url_for("owner.sign_in", next=target) if identity else None
        return render_template(
            "sign-in.html", csrf=token, next_path=target, error=error, passkey_url=passkey_url,
            email_login="@" in owner.username,
        ), status

    if signup is not None:
        @routes.route("/signup", methods=["GET", "POST"])
        def sign_up():
            if signup.account() is not None:
                abort(404)
            target = next_page(request.values.get("next", ""))
            token = session.get("login_csrf") or secrets.token_urlsafe(24)
            error = None
            status = 200
            if request.method == "POST":
                email = request.form.get("email", "").strip().lower()
                password = request.form.get("password", "")
                supplied = request.form.get("setup_token", "")
                if not valid_csrf(session.get("login_csrf"), request.form.get("csrf", "")):
                    error, status = "This setup form expired. Try again.", 400
                elif not valid_csrf(signup.token, supplied):
                    error, status = "The setup token did not match.", 403
                elif (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
                      or not 15 <= len(password) <= 128):
                    error, status = "Enter an email and a password of at least 15 characters.", 400
                else:
                    try:
                        signup.create(email, password)
                    except FileExistsError:
                        abort(409, "Owner setup is unavailable.")
                    session.clear()
                    return complete_login(target)
            session["login_csrf"] = token
            return render_template("sign-in.html", csrf=token, next_path=target,
                                   error=error, signup=True), status

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


def protect_shared_workspace(app, login, session_store):
    browser_sessions = BrowserSessions(session_store, login.session_key, MAX_AGE)

    @app.before_request
    def require_login():
        if not request.is_secure:
            abort(403)
        if browser_sessions.valid(login.username, request.cookies.get(COOKIE)):
            return
        if request.endpoint == "workspace":
            target = request.script_root + request.path
            return redirect("/login?" + urlencode({"next": target}), code=303)
        abort(401, "Sign in to Nonfluence.")

    @app.context_processor
    def logout_form_fields():
        return {
            "owner_logout_token": browser_sessions.logout_token(request.cookies.get(COOKIE, "")),
            "sign_out_url": "/logout",
        }
