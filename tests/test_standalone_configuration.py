import re
from unittest.mock import MagicMock

from nonfluence.browser_login import OwnerSignup, PasswordLogin
from nonfluence.nira_hq import ReadOnlyNiraHQ
from nonfluence.references import classify, nira_hq_url
from nonfluence.store import Store
from nonfluence.web import create_app


def test_owner_signup_is_one_time_and_persists(tmp_path):
    origin = "https://docs.example.test"
    token = "local-test-token-" + "A" * 32
    token_file = tmp_path / "setup-token"
    token_file.write_text(token)
    credentials = tmp_path / "browser-login"
    store = Store(tmp_path / "docs.sqlite3", "personal", write=True)
    page = store.create("Private page", "Private content")
    path = f"/pages/{page['id']}"
    signup = OwnerSignup(token_file, credentials)
    client = create_app(store, None, proxy_host="docs.example.test", browser_signup=signup).test_client()
    initial = client.get(path, base_url=origin, follow_redirects=True)
    assert "Create the owner account" in initial.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', initial.text)[1]
    data = {"email": "Owner@example.test", "password": "A long fixture password",
            "setup_token": token, "next": path, "csrf": csrf}
    assert client.post("/signup", base_url=origin, data={**data, "setup_token": "wrong"}).status_code == 403
    assert client.get("/api/pages", base_url=origin).status_code == 401
    assert client.post("/signup", base_url=origin, data=data).headers["Location"] == path
    assert client.post("/signup", base_url=origin, data=data).status_code == 404
    assert client.get(path, base_url=origin).status_code == 200
    assert credentials.stat().st_mode & 0o077 == 0

    account = PasswordLogin.from_file(credentials)
    client = create_app(store, None, proxy_host="docs.example.test", browser_auth=account).test_client()
    forged = {"X-Nonfluence-Verified-User": account.username}
    assert client.get("/api/pages", base_url=origin, headers=forged).status_code == 401
    login = client.get("/login", base_url=origin)
    csrf = re.search(r'name="csrf" value="([^"]+)"', login.text)[1]
    assert client.post("/login", base_url=origin, data={
        "username": data["email"], "password": data["password"], "csrf": csrf,
    }).status_code == 303
    assert client.get("/api/pages", base_url=origin).status_code == 200


def test_nira_reference_and_bearer_follow_the_configured_origin(tmp_path):
    origin = nira_hq_url("https://tickets.example.test/tracker/")
    credentials = tmp_path / "read-token"
    credentials.write_text("nira_" + "X" * 40)
    adapter = ReadOnlyNiraHQ(credentials, origin)
    adapter._opener = MagicMock()
    adapter._opener.open.return_value.__enter__.return_value.read.return_value = (
        b'{"id":"43f05651-2cb2-42a0-b2fd-ab65210a033e","key":"ONE-1","title":"Fixture","status":"ready"}'
    )
    target = "43f05651-2cb2-42a0-b2fd-ab65210a033e"
    link = f"{origin}/browse/issue/{target}"
    assert classify(link) is None
    assert classify(link, origin).target_id == target
    assert classify(link.replace("tickets.", "other."), origin) is None
    store = Store(tmp_path / "docs.sqlite3", "personal", write=True, nira_url=origin)
    page = store.create("Ticket notes", f"[Linked issue]({link})")
    details = create_app(store, None, nira_hq=adapter).test_client().get(f"/api/pages/{page['id']}").json
    assert details["references"][0]["status"] == "Ready"
    assert details["references"][0]["url"] == link
    submitted = adapter._opener.open.call_args.args[0]
    assert submitted.full_url == f"{origin}/api/issues/{target}"
    assert submitted.headers["Authorization"] == "Bearer " + credentials.read_text()
