import base64
import hashlib
import importlib.util
import json
import re
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from nonfluence.mcp_server import make_server
from nonfluence.store import Invalid, Store
from nonfluence.web import create_app

CONFIDENTIAL_EXTRA = importlib.util.find_spec("authlib") is not None
if CONFIDENTIAL_EXTRA:
    from joserfc import jwt
    from joserfc.jwk import RSAKey
    from requests import Response

    from nonfluence.confidential import COOKIE, SESSION_DURATION, OwnerIdentity


class DatabaseIsolationTests(unittest.TestCase):
    def test_classification_is_checked_on_disk_and_at_entry_points(self):
        with TemporaryDirectory() as folder:
            private = Store(Path(folder) / "private.sqlite3", "personal", write=True)
            confidential = Store(
                Path(folder) / "confidential.sqlite3", "personal", write=True, classification="confidential",
            )
            entry = confidential.create("Evidence title", "Confidential body terms")
            self.assertEqual(private.list_pages(), [])
            self.assertEqual(private.search("Evidence"), [])
            with self.assertRaises(Invalid):
                Store(confidential.path, "personal")
            with self.assertRaises(Invalid):
                Store(private.path, "personal", write=True, classification="confidential")
            with self.assertRaises(PermissionError):
                create_app(confidential, None)
            with self.assertRaises(PermissionError):
                make_server(confidential, None)
            ordinary = create_app(private, None).test_client()
            self.assertEqual(ordinary.get(f"/api/pages/{entry['id']}").status_code, 404)
            self.assertEqual(ordinary.get(f"/api/pages/{entry['id']}/history").status_code, 404)


@unittest.skipUnless(CONFIDENTIAL_EXTRA, "install the confidential extra to exercise OIDC")
class ConfidentialLoginTests(unittest.TestCase):
    issuer = "https://admin.example.test"
    origin = "https://vault.example.test"
    client_id = "nonfluence-vault"
    owner_subject = "owner-account-1"

    @classmethod
    def setUpClass(cls):
        cls.provider_key = RSAKey.generate_key(parameters={"kid": "fixture-issuer-key"})

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(
            Path(folder.name) / "vault.sqlite3", "confidential", write=True, classification="confidential",
        )
        self.page = self.store.create("Synthetic employment record", "synthetic confidential material")
        app = create_app(
            self.store, None, proxy_host="vault.example.test",
            confidential_auth=OwnerIdentity(self.issuer, self.client_id, "test-secret", self.owner_subject, "a" * 48),
        )
        self.browser = app.test_client()
        self.test_claims = {}
        self.exchanges = 0
        transport = patch("requests.sessions.Session.send", side_effect=self.provider)
        transport.start()
        self.addCleanup(transport.stop)

    def provider(self, request, **kwargs):
        self.assertTrue(request.url.startswith(self.issuer + "/"))
        route = urlsplit(request.url).path
        if route == "/.well-known/openid-configuration":
            payload = {
                "issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token", "jwks_uri": self.issuer + "/keys",
                "id_token_signing_alg_values_supported": ["RS256"],
            }
        elif route == "/keys":
            payload = {"keys": [self.provider_key.as_dict()]}
        elif route == "/token":
            self.exchanges += 1
            fields = parse_qs(request.body)
            self.assertEqual(fields["redirect_uri"], [self.origin + "/auth/callback"])
            verifier = hashlib.sha256(fields["code_verifier"][0].encode()).digest()
            self.assertEqual(self.challenge, base64.urlsafe_b64encode(verifier).decode().rstrip("="))
            now = int(time.time())
            claims = {
                "iss": self.issuer, "sub": self.owner_subject, "aud": self.client_id,
                "nonce": self.nonce, "auth_time": now, "iat": now, "exp": now + 180,
            }
            claims.update(self.test_claims)
            if claims.get("auth_time") is None:
                del claims["auth_time"]
            token = jwt.encode({"alg": "RS256", "kid": "fixture-issuer-key"}, claims, self.provider_key)
            payload = {"access_token": "fixture-access-token", "id_token": token, "token_type": "Bearer"}
        else:
            self.fail(f"Unexpected OIDC request: {route}")
        response = Response()
        response.status_code = 200
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
        return response

    def get(self, path):
        return self.browser.get(path, base_url=self.origin)

    def begin_login(self, *, path="/auth/sign-in", max_age="0"):
        result = self.get(path)
        self.assertEqual(result.status_code, 302)
        authorization_url = urlsplit(result.location)
        self.assertEqual(authorization_url.netloc, "admin.example.test")
        fields = parse_qs(authorization_url.query)
        self.assertEqual(fields["code_challenge_method"], ["S256"])
        self.assertEqual(fields["max_age"], [max_age])
        self.assertEqual(fields["scope"], ["openid"])
        self.challenge = fields["code_challenge"][0]
        self.nonce = fields["nonce"][0]
        return fields["state"][0]

    def callback(self, state):
        return self.browser.get(
            "/auth/callback", base_url=self.origin,
            query_string={"state": state, "code": "issuer-fixture-code"},
        )

    def test_owner_can_edit_and_logout_revokes_the_cookie(self):
        target = self.page["id"]
        for path in ("/api/pages", f"/api/pages/{target}", f"/api/pages/{target}/history", "/api/search?q=synthetic"):
            response = self.get(path)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(b"synthetic confidential", response.data)
        self.assertEqual(self.get(f"/pages/{target}").location, "/auth/sign-in")

        response = self.callback(self.begin_login())
        self.assertEqual(response.status_code, 303)
        cookie = self.browser.get_cookie(COOKIE, domain="vault.example.test")
        self.assertTrue(cookie.http_only and cookie.secure and cookie.origin_only)
        self.assertEqual(self.get(f"/api/pages/{target}").json["title"], self.page["title"])
        updated = self.browser.put(
            f"/api/pages/{target}", base_url=self.origin, headers={"Origin": self.origin},
            json={"title": "Revised synthetic record", "markdown": "updated", "expected_revision": 1},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json["revision"], 2)
        logout = self.browser.post("/auth/sign-out", base_url=self.origin, data={"logout_token": "wrong"})
        self.assertEqual(logout.status_code, 403)
        html = self.get("/").text
        self.assertIn("Confidential", html)
        csrf = re.search(r'name="logout_token" value="([0-9a-f]+)"', html)[1]
        logout = self.browser.post("/auth/sign-out", base_url=self.origin, data={"logout_token": csrf})
        self.assertEqual(logout.status_code, 200)
        self.browser.set_cookie(COOKIE, cookie.value, domain="vault.example.test", secure=True)
        self.assertEqual(self.get("/api/pages").status_code, 401)

    def test_non_owner_or_stale_authentication_cannot_open_the_workspace(self):
        for overrides in (
            {"sub": "invited-guest"},
            {"auth_time": int(time.time()) - 900},
            {"auth_time": None},
        ):
            self.test_claims = overrides
            result = self.callback(self.begin_login())
            self.assertIn(result.status_code, (401, 403))
            self.assertEqual(self.get("/api/pages").status_code, 401)
        self.assertEqual(self.exchanges, 3)

    def test_invalid_state_nonce_issuer_and_audience_are_rejected(self):
        self.begin_login()
        self.assertEqual(self.callback("wrong").status_code, 401)
        self.assertEqual(self.exchanges, 0)
        for overrides in (
            {"nonce": "wrong"},
            {"aud": "different-client"},
            {"iss": "https://other.example.test"},
        ):
            self.test_claims = overrides
            self.assertEqual(self.callback(self.begin_login()).status_code, 401)
            self.assertEqual(self.get("/api/pages").status_code, 401)

    def test_session_expires_on_the_server(self):
        self.assertEqual(self.callback(self.begin_login()).status_code, 303)
        cached_time = int(time.time())
        with patch("nonfluence.confidential.time.time", return_value=cached_time + SESSION_DURATION + 1):
            self.assertEqual(self.get("/api/pages").status_code, 401)

    def test_private_workspace_uses_the_same_issuer_and_honors_token_expiry(self):
        from werkzeug.security import generate_password_hash

        from nonfluence.browser_login import COOKIE as PRIVATE_COOKIE
        from nonfluence.browser_login import PasswordLogin

        store = Store(self.store.path.with_name("private.sqlite3"), "personal", write=True)
        page = store.create("Ordinary page", "Private notes")
        password = PasswordLogin(
            "owner", generate_password_hash("fixture", method="pbkdf2:sha256:1000000"), "b" * 48,
        )
        identity = OwnerIdentity(self.issuer, self.client_id, "fixture", self.owner_subject, "b" * 48)
        self.browser = create_app(
            store, None, proxy_host="vault.example.test", browser_auth=password, browser_identity=identity,
        ).test_client()
        page_path = f"/pages/{page['id']}"
        self.assertIn("Sign in with a passkey", self.get("/login").text)
        state = self.begin_login(path="/auth/sign-in?next=" + page_path, max_age="3600")
        result = self.callback(state)
        self.assertEqual(result.location, page_path)
        self.assertIsNotNone(self.browser.get_cookie(PRIVATE_COOKIE, domain="vault.example.test"))
        self.assertEqual(self.get("/api/pages").status_code, 200)
        later = int(time.time()) + 181
        with patch("nonfluence.confidential.time.time", return_value=later):
            self.assertEqual(self.get("/api/pages").status_code, 401)
