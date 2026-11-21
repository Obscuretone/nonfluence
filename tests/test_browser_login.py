import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from werkzeug.security import generate_password_hash

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
