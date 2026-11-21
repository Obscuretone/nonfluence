import base64

from nonfluence.confluence import ConfluenceCredentials
from nonfluence.store import Store
from nonfluence.web import create_app


def test_confluence_pages_use_native_history_and_scope(tmp_path):
    store = Store(tmp_path / "docs.sqlite3", "personal", write=True)
    email, token = "owner@example.test", "X" * 40
    headers = {"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()}
    client = create_app(store, None, confluence_credentials=ConfluenceCredentials(email, token, True)).test_client()
    base = "/wiki/api/v2"
    assert client.get(base + "/spaces").status_code == 401
    space = client.get(base + "/spaces", headers=headers).json["results"][0]["id"]
    create = {"spaceId": space, "status": "current", "title": "Fixture page",
              "body": {"representation": "storage", "value": "<p>Hello <strong>world</strong></p>"}}
    result = client.post(base + "/pages", json=create, headers=headers)
    assert result.status_code == 200, result.text
    original = result.json
    target = base + "/pages/" + original["id"]
    assert "<strong>world</strong>" in client.get(target + "?body-format=storage", headers=headers).json["body"]["storage"]["value"]
    edit = {**create, "id": original["id"], "title": "Renamed page", "version": {"number": 2}}
    assert client.put(target, json=edit, headers=headers).json["version"]["number"] == 2
    assert client.put(target, json=edit, headers=headers).status_code == 409
    assert client.get(target + "?version=1", headers=headers).json["title"] == "Fixture page"
    search = client.get("/wiki/rest/api/search", query_string={"cql": 'type=page AND text ~ "world"'}, headers=headers)
    assert search.json["results"][0]["content"]["id"] == original["id"]
    assert [item["number"] for item in client.get(target + "/versions", headers=headers).json["results"]] == [2, 1]
    assert store.list_pages()[0]["title"] == "Renamed page"
    reader = create_app(store, None, confluence_credentials=ConfluenceCredentials(email, token)).test_client()
    assert reader.get(target, headers=headers).status_code == 200
    assert reader.post(base + "/pages", json=create, headers=headers).status_code == 403
