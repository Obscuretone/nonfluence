"""Extract and resolve typed references from parsed Markdown links."""

import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit
from uuid import UUID

from markdown_it import MarkdownIt


MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
NIRA_KEY = re.compile(r"(?:[A-Za-z][A-Za-z0-9_-]*-)?[1-9][0-9]{0,17}\Z")
NIRA_HQ_PATH = re.compile(r"/browse/(project|issue)/([0-9a-fA-F-]{36})\Z")
GITHUB = re.compile(
    r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)"
    r"(?:(/issues/|/pull/)([1-9][0-9]*)|/commit/([0-9a-fA-F]{7,40}))?/?(?:#[^\s]*)?\Z"
)


@dataclass(frozen=True)
class Reference:
    kind: str
    target_id: str
    label: str = ""


def nira_key(value):
    return value.upper() if NIRA_KEY.fullmatch(value) else None


def nira_hq_url(value):
    if not value:
        return None
    url = value.rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)*", parsed.path)
            or re.search(r"[\s\\]", url)):
        raise ValueError("Configure the HTTPS Nira base URL, with an optional mount path.")
    return url


def classify(href, nira_url=None):
    if href.startswith(("page:", "/pages/")):
        _, _, target = href.partition(":" if href.startswith("page:") else "/pages/")
        try:
            return Reference("page", str(UUID(target)))
        except ValueError:
            return None
    if href.startswith("nira:"):
        target = nira_key(href.removeprefix("nira:"))
        if target is not None:
            return Reference("nira", target)
    hq = NIRA_HQ_PATH.fullmatch(href.removeprefix(nira_url)) if nira_url and href.startswith(nira_url) else None
    if hq:
        kind, target = hq.groups()
        try:
            return Reference(f"nira_hq_{kind}", str(UUID(target)))
        except ValueError:
            return None
    github = GITHUB.fullmatch(href)
    if github:
        owner, repo, route, number, commit = github.groups()
        if owner in (".", "..") or repo in (".", ".."):
            return None
        target = f"{owner.lower()}/{repo.lower()}"
        if commit:
            return Reference("github_commit", f"{target}/commit/{commit.lower()}")
        if route:
            kind = "github_pull" if route == "/pull/" else "github_issue"
            return Reference(kind, f"{target}{route}{number}")
        return Reference("github_repo", target)
    return None


def link_tokens(tokens):
    for block in tokens:
        if not block.children:
            continue
        children = iter(block.children)
        for token in children:
            if token.type == "link_open":
                label = []
                for child in children:
                    if child.type == "link_close":
                        break
                    if child.type in ("text", "code_inline", "image"):
                        label.append(child.content)
                yield token, "".join(label).strip()


def references(markdown, nira_url=None):
    found = {}
    for token, label in link_tokens(MARKDOWN.parse(markdown)):
        ref = classify(token.attrGet("href") or "", nira_url)
        if ref:
            found.setdefault((ref.kind, ref.target_id), Reference(ref.kind, ref.target_id, label))
    return list(found.values())


def render(markdown, resolved=(), *, page_prefix="", nira_url=None):
    env = {}
    tokens = MARKDOWN.parse(markdown, env)
    links = {(ref["kind"], ref["target_id"]): ref for ref in resolved}
    for token, _ in link_tokens(tokens):
        href = token.attrGet("href") or ""
        ref = classify(href, nira_url)
        if not ref:
            if href.startswith(("nira:", "page:")):
                token.attrs.pop("href", None)
                token.attrSet("title", "Invalid typed reference")
            continue
        detail = links.get((ref.kind, ref.target_id), {})
        resolved_url = detail.get("url")
        if resolved_url:
            url = resolved_url
        elif ref.kind == "page" and detail.get("state") != "broken":
            url = f"/pages/{ref.target_id}"
        elif ref.kind.startswith("github_"):
            url = f"https://github.com/{ref.target_id}"
        elif ref.kind.startswith("nira_hq_") and detail.get("state") != "broken":
            url = href
        else:
            url = None
        if ref.kind == "page" and url and url.startswith("/pages/"):
            url = page_prefix + url
        if url:
            token.attrSet("href", url)
        else:
            token.attrs.pop("href", None)
        token.attrSet("class", f"typed-reference {detail.get('state', 'unresolved')}")
        token.attrSet("title", detail.get("title") or ref.target_id)
    return MARKDOWN.renderer.render(tokens, MARKDOWN.options, env)


FIXTURE_TICKETS = {
    "NIRA-142": {
        "title": "Set up database VPS",
        "status": "In progress",
        "preview": "Prepare the database host and verify the backup and restore path.",
    }
}


class FixtureNira:
    def resolve_many(self, keys):
        return {
            key: {"state": "resolved", "provider": "fixture", **FIXTURE_TICKETS[key]}
            if key in FIXTURE_TICKETS else {"state": "broken", "provider": "fixture"}
            for key in keys
        }


def resolve(rows, nira=None, nira_hq=None, *, nira_url=None):
    result = []
    nira_keys = {row["target_id"] for row in rows if row["kind"] == "nira"}
    tickets = nira.resolve_many(nira_keys) if nira and nira_keys else {}
    hq_keys = [(row["kind"], row["target_id"]) for row in rows if row["kind"].startswith("nira_hq_")]
    hq_details = nira_hq.resolve_many(hq_keys) if nira_hq and hq_keys else {}
    for row in rows:
        kind, target = row["kind"], row["target_id"]
        if kind == "page":
            title = row["page_title"]
            details = (
                {"title": title, "url": f"/pages/{target}", "state": "resolved"}
                if title else {"state": "broken"}
            )
        elif kind == "nira":
            details = tickets.get(target, {"state": "unavailable", "provider": "nira"})
        elif kind.startswith("nira_hq_"):
            details = {"state": "unavailable", "provider": "nira-hq", **hq_details.get((kind, target), {})}
            if nira_url and details["state"] != "broken":
                details["url"] = f"{nira_url}/browse/{kind.removeprefix('nira_hq_')}/{target}"
        else:
            details = {"state": "unverified", "url": f"https://github.com/{quote(target, safe='/')}"}
        result.append({
            "kind": kind,
            "target_id": target,
            "title": row["label"] or target,
            **details,
        })
    return result
