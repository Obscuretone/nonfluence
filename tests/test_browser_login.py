import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from werkzeug.security import generate_password_hash
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.test import Client

from nonfluence.browser_login import COOKIE, PasswordLogin
from nonfluence.store import Store
from nonfluence.web import create_app


class BrowserLoginTests(unittest.TestCase):
    origin = "https://docs.example.test"
    password = "password used by the test only"

    @classmethod
    def setUpClass(cls):
        cls.credentials = PasswordLogin(
            "owner", generate_password_hash(cls.password, method="pbkdf2:sha256:1000000"), "a" * 48,
        )

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "pages.sqlite3", "personal", write=True)
        self.directory = Path(folder.name)
        self.personal_store = store
        self.page = store.create("Existing article", "Private material")
        self.site = create_app(store, None, proxy_host="docs.example.test", browser_auth=self.credentials)
        self.client = self.site.test_client()

    def get(self, route):
        return self.client.get(route, base_url=self.origin)

    def post(self, route, fields):
        return self.client.post(route, data=fields, base_url=self.origin)

    def csrf(self):
        return re.search(r'name="csrf" value="([^"]+)"', self.get("/login").text)[1]

    def test_login_form_preserves_page_path_and_revokes_logout(self):
        page_url = f"/pages/{self.page['id']}"
        self.assertIn("/login?next=", self.get(page_url).location)
        self.assertEqual(self.get("/api/pages").status_code, 401)
        self.assertIsNone(self.get("/api/pages").headers.get("WWW-Authenticate"))
        self.assertEqual(self.get("/static/workspace.js").status_code, 401)
        with self.get("/static/sign-in.css") as style:
            self.assertEqual(style.status_code, 200)
        with self.get("/static/obscuretone.svg") as logo:
            self.assertEqual(logo.status_code, 200)
        with self.get("/static/favicon.ico") as favicon:
            self.assertEqual(favicon.status_code, 200)
        result = self.post("/login", {
            "username": "owner", "password": self.password, "csrf": self.csrf(), "next": page_url,
        })
        self.assertEqual(result.location, page_url)
        cookie = self.client.get_cookie(COOKIE, domain="docs.example.test")
        self.assertTrue(cookie.http_only and cookie.secure and cookie.origin_only)
        self.assertEqual(self.get(f"/api/pages/{self.page['id']}").status_code, 200)
        workspace = self.get(page_url)
        logout_csrf = re.search(r'name="logout_token" value="([^"]+)"', workspace.text)[1]
        self.assertEqual(self.post("/logout", {"logout_token": "bad"}).status_code, 403)
        self.assertEqual(self.post("/logout", {"logout_token": logout_csrf}).location, "/login")
        self.client.set_cookie(COOKIE, cookie.value, domain="docs.example.test", secure=True)
        self.assertEqual(self.get("/api/pages").status_code, 401)

    def test_rejects_wrong_credentials_csrf_and_external_redirects(self):
        self.assertEqual(self.post("/login", {"username": "owner", "password": self.password}).status_code, 400)
        result = self.post("/login", {"username": "owner", "password": "wrong", "csrf": self.csrf()})
        self.assertEqual(result.status_code, 401)
        self.assertIsNone(result.headers.get("WWW-Authenticate"))
        self.assertIn("did not match", result.text)
        result = self.post("/login", {
            "username": "owner", "password": self.password, "csrf": self.csrf(), "next": "//example.invalid",
        })
        self.assertEqual(result.location, "/")

    def test_legacy_proxy_identity_only_opens_api(self):
        auth = {"X-Nonfluence-Verified-User": "owner"}
        self.assertEqual(self.client.get("/api/pages", headers=auth, base_url=self.origin).status_code, 200)
        self.assertEqual(self.client.get("/", headers=auth, base_url=self.origin).status_code, 303)

    def test_company_space_shares_owner_sign_in_and_isolates_pages(self):
        company_store = Store(self.directory / "company.sqlite3", "plugins", write=True)
        company_target = company_store.create("Company research", "Private company marker")
        company_home = company_store.create("Company home", f"[Research](page:{company_target['id']})")
        shared = {"proxy_host": "docs.example.test", "browser_auth": self.credentials, "workspace_prefix": "/plugins"}
        personal = create_app(self.personal_store, None, **shared)
        company = create_app(company_store, None, browser_session_store=self.personal_store, **shared)
        self.client = Client(DispatcherMiddleware(personal, {"/plugins": company}))
        page_path = f"/plugins/pages/{company_home['id']}"
        self.assertIn("/login?next=%2Fplugins%2Fpages%2F", self.get(page_path).location)
        self.assertEqual(self.get("/plugins/api/pages").status_code, 401)
        forged = self.client.get("/plugins/api/pages", headers={"X-Nonfluence-Verified-User": "owner"},
                                 base_url=self.origin)
        self.assertEqual(forged.status_code, 401)
        login = self.post("/login", {
            "username": "owner", "password": self.password, "csrf": self.csrf(), "next": page_path,
        })
        self.assertEqual(login.location, page_path)
        shell = self.get(page_path)
        self.assertEqual(shell.status_code, 200)
        self.assertIn('data-workspace-path="/plugins"', shell.text)
        self.assertIn("Plugin company", self.get("/").text)
        self.assertNotIn(company_home["id"], self.get("/api/pages").text)
        self.assertNotIn(self.page["id"], self.get("/plugins/api/pages").text)
        self.assertEqual(self.get(f"/plugins/api/pages/{self.page['id']}").status_code, 404)
        self.assertEqual(self.get(f"/api/pages/{company_home['id']}").status_code, 404)
        result = self.get(f"/plugins/api/pages/{company_home['id']}").json
        self.assertIn(f'href="/plugins/pages/{company_target["id"]}"', result["html"])
        self.assertIn(company_target["id"], self.get("/plugins/api/search?q=marker").text)
        self.assertNotIn(company_target["id"], self.get("/api/search?q=marker").text)
        token = re.search(r'name="logout_token" value="([^"]+)"', shell.text)[1]
        self.assertEqual(self.post("/logout", {"logout_token": token}).status_code, 303)
        self.assertEqual(self.get("/plugins/api/pages").status_code, 401)
