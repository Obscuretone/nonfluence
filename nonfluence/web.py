"""Loopback browser interface for a single Nonfluence space."""

from flask import Flask, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from .references import render, resolve
from .store import Conflict, Invalid, Missing, page_fields


def create_app(
    store, nira, *, nira_hq=None, proxy_host=None, confidential_auth=None,
    browser_auth=None, browser_identity=None, workspace_prefix="", browser_session_store=None,
):
    if proxy_host and confidential_auth is None and browser_auth is None:
        raise PermissionError("A hosted workspace requires an explicit authentication mode.")
    if (store.classification == "confidential") != (confidential_auth is not None):
        raise PermissionError("A confidential database requires owner sign-in on a separate origin.")
    if confidential_auth is not None and (not proxy_host or nira is not None or nira_hq is not None):
        raise PermissionError("Confidential workspaces require a configured host and local page data.")
    if browser_auth is not None and (not proxy_host or confidential_auth is not None):
        raise PermissionError("Browser password sign-in requires its own hosted workspace.")
    if browser_identity is not None and browser_auth is None:
        raise PermissionError("Browser passkey sign-in requires hosted browser sessions.")
    if browser_session_store is not None and (not browser_auth or not workspace_prefix or browser_identity):
        raise PermissionError("A mounted workspace requires the host's browser sessions.")
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=1_250_000,
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
    )
    if proxy_host:
        app.config["TRUSTED_HOSTS"] = [proxy_host]
    if confidential_auth is not None:
        from .confidential import protect_workspace

        protect_workspace(app, confidential_auth, proxy_host, store)
    if browser_auth is not None:
        from .browser_login import protect_shared_workspace, protect_workspace

        if browser_session_store is None:
            protect_workspace(app, browser_auth, store, identity=browser_identity, host=proxy_host,
                              workspace_prefix=workspace_prefix)
        else:
            protect_shared_workspace(app, browser_auth, browser_session_store)

    @app.after_request
    def protect(response):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'; img-src 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(Invalid)
    @app.errorhandler(Missing)
    @app.errorhandler(Conflict)
    @app.errorhandler(PermissionError)
    def operation_error(error):
        status = {Invalid: 400, Missing: 404, Conflict: 409, PermissionError: 403}
        return jsonify(error=str(error)), status[type(error)]

    def payload():
        if request.headers.get("Origin") != request.host_url.rstrip("/"):
            abort(403, "Use the Nonfluence interface from its local origin.")
        value = request.get_json()
        if not isinstance(value, dict):
            raise Invalid("Send a JSON object.")
        return value

    @app.get("/")
    @app.get("/pages/<uuid:target>")
    def workspace(target=None):
        return render_template(
            "workspace.html", space=store.space, writable=store.write, classification=store.classification,
            workspace_prefix=workspace_prefix,
        )

    @app.get("/api/pages")
    def pages():
        return jsonify(pages=store.list_pages())

    @app.get("/api/search")
    def search():
        return jsonify(pages=store.search(request.args.get("q", "")))

    @app.get("/api/pages/<target>")
    def page(target):
        result, rows = store.read(target)
        result["references"] = resolve(rows, nira, nira_hq)
        result["html"] = render(result["markdown"], result["references"], page_prefix=request.script_root)
        for ref in result["references"]:
            if ref["kind"] == "page" and (ref.get("url") or "").startswith("/pages/"):
                ref["url"] = request.script_root + ref["url"]
        return jsonify(result)

    @app.get("/api/pages/<target>/history")
    def history(target):
        return jsonify(revisions=store.history(target))

    @app.get("/api/pages/<target>/history/<int:revision>")
    def version(target, revision):
        result = store.revision(target, revision)
        result["html"] = render(result["markdown"], page_prefix=request.script_root)
        return jsonify(result)

    @app.post("/api/preview")
    def preview():
        _, markdown = page_fields("Preview", payload().get("markdown"))
        return jsonify(html=render(markdown, page_prefix=request.script_root))

    @app.post("/api/pages")
    def create():
        data = payload()
        return jsonify(store.create(
            data.get("title"), data.get("markdown"), data.get("parent_id"), data.get("request_id"),
        )), 201

    @app.put("/api/pages/<target>")
    def update(target):
        data = payload()
        return jsonify(store.update(
            target, data.get("expected_revision"), data.get("title"), data.get("markdown"),
            parent=data.get("parent_id"), move="parent_id" in data,
        ))

    return app
