"""Confluence Cloud REST for published pages in one configured workspace."""

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from flask import Blueprint, abort, jsonify, request
from werkzeug.exceptions import HTTPException

from .references import MARKDOWN
from .store import Conflict, Invalid, Missing

API = "/wiki/api/v2"
SEARCH = "/wiki/rest/api/search"
PAGE_STATUS = "current"


@dataclass(frozen=True)
class ConfluenceCredentials:
    email: str
    token: str = field(repr=False)
    write: bool = False

    @classmethod
    def from_file(cls, path):
        entry = cls(**json.loads(path.read_text()))
        if (not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", entry.email)
                or not re.fullmatch(r"[A-Za-z0-9_~.-]{32,256}", entry.token) or type(entry.write) is not bool):
            raise ValueError("Configure a Confluence API email and a random token.")
        return entry

    def authenticates(self, request):
        auth = request.authorization
        if auth is None or auth.type != "basic" or auth.username is None or auth.password is None:
            return False
        return (hmac.compare_digest(auth.username.lower().encode(), self.email.lower().encode())
                and hmac.compare_digest(auth.password.encode(), self.token.encode()))


def number(identifier):
    return str(UUID(identifier).int >> 75)


def version_record(record):
    return {"number": record["revision"], "createdAt": record["updated_at"],
            "message": "", "minorEdit": False, "authorId": ""}


class StorageMarkdown(HTMLParser):
    """Convert the supported Confluence storage elements into editable Markdown."""

    blocks = {"p": "", "h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "h5": "##### ", "h6": "###### "}
    marks = {"strong": "**", "b": "**", "em": "*", "i": "*", "code": "`"}

    def __init__(self):
        super().__init__()
        self.output = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in self.blocks:
            self.output.append("\n\n" + self.blocks[tag])
        elif tag in self.marks:
            self.output.append(self.marks[tag])
        elif tag == "br":
            self.output.append("  \n")
            return
        elif tag in {"ul", "ol"}:
            self.output.append("\n")
        elif tag == "li":
            self.output.append("  " * max(0, sum(t in {"ul", "ol"} for t, _ in self.stack) - 1))
            self.output.append("1. " if self.stack and self.stack[-1][0] == "ol" else "- ")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            parsed = urlsplit(href)
            if (not href or re.search(r"[\s<>()\\]", href)
                    or parsed.scheme not in {"", "https", "http", "mailto", "page", "nira"}):
                raise Invalid("Unsupported storage link.")
            self.output.append("[")
            self.stack.append((tag, href))
            return
        else:
            raise Invalid(f"Unsupported Confluence storage element <{tag}>.")
        self.stack.append((tag, None))

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if not self.stack or self.stack[-1][0] != tag:
            raise Invalid("Confluence storage markup must be balanced.")
        _, value = self.stack.pop()
        if tag in self.marks:
            self.output.append(self.marks[tag])
        elif tag == "a":
            self.output.append(f"]({value})")
        elif tag in self.blocks:
            self.output.append("\n\n")
        elif tag in {"ul", "ol", "li"}:
            self.output.append("\n")

    def handle_data(self, content):
        content = re.sub(r"\s+", " ", content)
        if content.isspace() and (not self.stack or self.stack[-1][0] in {"ul", "ol"}):
            return
        if not self.stack or self.stack[-1][0] != "code":
            content = re.sub(r"([\\`*_\[\]<>])", r"\\\1", content)
        self.output.append(content)

    def markdown(self, storage):
        try:
            self.feed(storage)
            self.close()
        except ValueError as error:
            raise Invalid("Invalid Confluence storage markup.") from error
        if self.stack:
            raise Invalid("Confluence storage markup must be balanced.")
        return re.sub(r"\n{3,}", "\n\n", "".join(self.output)).strip()


def body_markdown(fields):
    body = fields.get("body")
    if not isinstance(body, dict):
        raise Invalid("Supply a Confluence storage body.")
    body = body.get("storage", body)
    value = body.get("value") if isinstance(body, dict) else None
    if not isinstance(value, str) or body.get("representation", "storage") != "storage":
        raise Invalid("Only the storage body representation is supported.")
    return StorageMarkdown().markdown(value)


def offset_of(cursor):
    if not cursor:
        return 0
    try:
        value = base64.b64decode(cursor, altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeError):
        raise Invalid("Invalid cursor.") from None
    if not re.fullmatch(r"[0-9]{1,8}", value):
        raise Invalid("Invalid cursor.")
    return int(value)


def register_confluence(app, store, credentials):
    space_id = str(int.from_bytes(hashlib.sha256(store.space.encode()).digest()[:7], "big") >> 3)
    routes = Blueprint("confluence", __name__)

    @routes.before_request
    def authorize():
        write = request.method in {"POST", "PUT", "DELETE"}
        if not credentials.authenticates(request):
            abort(401, "Authentication required.")
        if write and not credentials.write:
            abort(403, "Write access required.")

    @routes.errorhandler(HTTPException)
    @routes.errorhandler(Invalid)
    @routes.errorhandler(Missing)
    @routes.errorhandler(Conflict)
    @routes.errorhandler(PermissionError)
    def api_error(error):
        code = (error.code if isinstance(error, HTTPException) else
                {Invalid: 400, Missing: 404, Conflict: 409, PermissionError: 403}[type(error)])
        body = jsonify(errors=[{"status": code, "title": error.description if isinstance(error, HTTPException) else str(error)}])
        if code == 401:
            body.headers["WWW-Authenticate"] = 'Basic realm="Nonfluence"'
        return body, code

    def links(record):
        external = number(record["id"])
        return {"webui": f"{request.script_root}/pages/{record['id']}",
                "self": f"{request.script_root}{API}/pages/{external}"}

    def record(page, *, with_body=False):
        output = {"id": number(page["id"]), "status": PAGE_STATUS, "title": page["title"], "spaceId": space_id,
                  "parentId": number(page["parent_id"]) if page["parent_id"] else None,
                  "parentType": "page" if page["parent_id"] else None,
                  "version": version_record(page), "_links": links(page)}
        if with_body:
            output["body"] = {"storage": {"representation": "storage", "value": MARKDOWN.render(page["markdown"])}}
        return output

    def find_page(external):
        matches = [page["id"] for page in store.list_pages() if number(page["id"]) == str(external)]
        if len(matches) != 1:
            raise Missing("Page not found.")
        return matches[0]

    def validate_space(supplied):
        if supplied != space_id:
            raise Missing("Space not found.")

    def fields():
        value = request.get_json()
        if not isinstance(value, dict) or value.get("status", PAGE_STATUS) != PAGE_STATUS:
            raise Invalid("Supply a published page.")
        return value

    def requested_body():
        requested = request.args.get("body-format")
        if requested not in {None, "storage"}:
            raise Invalid("Only the storage body format is supported.")
        return requested == "storage"

    def page_of(items, offset, limit, **parameters):
        body = {"results": items, "_links": {"base": request.host_url.rstrip("/")}}
        if len(items) > limit:
            cursor = base64.urlsafe_b64encode(str(offset + limit).encode()).decode()
            next_url = request.script_root + request.path + "?" + urlencode({**parameters, "limit": limit, "cursor": cursor})
            body["results"] = items[:limit]
            body["_links"]["next"] = next_url
        response = jsonify(body)
        if "next" in body["_links"]:
            response.headers["Link"] = f"<{body['_links']['next']}>; rel=\"next\""
        return response

    def paging():
        limit = request.args.get("limit", default=25, type=int)
        if not 1 <= limit <= 50:
            raise Invalid("Use a limit between 1 and 50.")
        return offset_of(request.args.get("cursor")), limit

    @routes.get(API + "/spaces")
    def spaces():
        key = request.args.getlist("keys")
        item = {"id": space_id, "key": store.space, "name": store.space, "type": "global", "status": PAGE_STATUS}
        return jsonify(results=[item] if not key or store.space in key else [],
                       _links={"base": request.host_url.rstrip("/")})

    @routes.get(API + "/pages")
    @routes.get(API + "/spaces/<space>/pages")
    def pages(space=None):
        if space is not None:
            validate_space(space)
        for candidate in request.args.getlist("space-id"):
            validate_space(candidate)
        offset, limit = paging()
        title = request.args.get("title")
        with_body = requested_body()
        matches = [page for page in store.list_pages() if title is None or page["title"] == title]
        results = [
            record(store.read(page["id"])[0] if with_body else page, with_body=with_body)
            for page in matches[offset:offset + limit + 1]
        ]
        return page_of(results, offset, limit, **{k: v for k, v in request.args.items()
                                                  if k in {"title", "body-format", "space-id"}})

    @routes.get(API + "/pages/<int:external>")
    def page(external):
        target = find_page(external)
        if "version" in request.args:
            revision = request.args.get("version", type=int)
            if revision is None:
                raise Invalid("Invalid page version.")
            entry = {**store.revision(target, revision), "id": target}
        else:
            entry = store.read(target)[0]
        return jsonify(record(entry, with_body=requested_body()))

    @routes.post(API + "/pages")
    def create():
        data = fields()
        if set(data) - {"spaceId", "status", "title", "parentId", "body"}:
            raise Invalid("Unsupported create-page fields.")
        validate_space(data.get("spaceId"))
        parent = find_page(data["parentId"]) if data.get("parentId") else None
        target = store.create(data.get("title"), body_markdown(data), parent)
        return jsonify(record(target, with_body=True))

    @routes.put(API + "/pages/<int:external>")
    def update(external):
        data = fields()
        if (set(data) - {"id", "status", "title", "spaceId", "parentId", "body", "version"}
                or data.get("id") != str(external) or not isinstance(data.get("version"), dict)):
            raise Invalid("Supply the matching page ID and next version.")
        if "spaceId" in data:
            validate_space(data["spaceId"])
        expected = data["version"].get("number")
        if type(expected) is not int or expected < 2:
            raise Invalid("Supply the next version number.")
        parent = find_page(data["parentId"]) if data.get("parentId") else None
        target = store.update(find_page(external), expected - 1, data.get("title"), body_markdown(data),
                              parent=parent, move="parentId" in data)
        return jsonify(record(target, with_body=True))

    @routes.get(API + "/pages/<int:external>/versions")
    def versions(external):
        offset, limit = paging()
        page_versions = store.history(find_page(external))[offset:offset + limit + 1]
        return page_of([version_record(item) for item in page_versions], offset, limit)

    @routes.get(SEARCH)
    def search():
        cql = request.args.get("cql", "")
        pattern = r'type\s*=\s*page(?:\s+AND\s+(title|text)\s*~\s*"(.*?)")?'
        match = re.fullmatch(pattern, cql, re.I)
        if not match:
            raise Invalid('Use type = page with an optional title or text contains expression.')
        field, term = match.groups()
        offset, limit = paging()
        found = store.list_pages() if not term else ([
            page for page in store.list_pages() if term.casefold() in page["title"].casefold()
        ] if field.lower() == "title" else [
            {**store.read(page["id"])[0], "excerpt": page["excerpt"]}
            for page in store.search(term, 50)
        ])
        items = []
        for page in found[offset:offset + limit + 1]:
            content = {**record(page), "type": "page"}
            items.append({"content": content, "title": page["title"], "url": content["_links"]["webui"],
                          "excerpt": page.get("excerpt", ""), "lastModified": page["updated_at"]})
        result = page_of(items, offset, limit, cql=cql)
        body = result.get_json()
        body.update(limit=limit, size=len(body["results"]), start=offset,
                    cqlQuery=cql, totalSize=len(found), searchDuration=0)
        response = jsonify(body)
        if "Link" in result.headers:
            response.headers["Link"] = result.headers["Link"]
        return response

    app.register_blueprint(routes)
