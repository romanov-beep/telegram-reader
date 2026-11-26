import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from telegram_reader.auth import Auth, AuthError


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = dict(public_url="https://reader.example", state_directory=self.tmp.name,
                           owner_secret_sha256=digest("owner-only-secret"),
                           desktop_token_sha256=digest("desktop-secret"),
                           oauth_clients=[dict(client_id="public", redirect_uris=["https://client.example/callback"]),
                                          dict(client_id="other", redirect_uris=["http://localhost:8800/cb"]),
                                          dict(client_id="private", redirect_uris=["https://private.example/cb"],
                                               client_secret_sha256=digest("client-secret"))])
        self.auth = Auth(self.config)
        self.verifier = "v" * 43
        self.challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()

    def tearDown(self):
        self.auth.db.close()
        self.tmp.cleanup()

    def start(self, **updates):
        params = dict(client_id="public", redirect_uri="https://client.example/callback",
                      response_type="code", resource=self.auth.resource, scope="telegram.read",
                      state="opaque-state", code_challenge_method="S256", code_challenge=self.challenge)
        params.update(updates)
        return self.auth.authorize_start(params)

    def code(self, **updates):
        flow = self.start(**updates)
        url = self.auth.authorize_finish(flow["ticket"], flow["cookie"], "owner-only-secret")
        response = parse_qs(urlsplit(url).query)
        self.assertEqual(response["state"], ["opaque-state"])
        self.assertEqual(response["iss"], [self.auth.issuer])
        return response["code"][0]

    def exchange(self, code, **updates):
        params = dict(grant_type="authorization_code", code=code, code_verifier=self.verifier,
                      client_id="public", redirect_uri="https://client.example/callback", resource=self.auth.resource)
        params.update(updates)
        return self.auth.grant(params)

    def refresh(self, token, **updates):
        params = dict(grant_type="refresh_token", refresh_token=token,
                      client_id="public", resource=self.auth.resource)
        params.update(updates)
        return self.auth.grant(params)

    def test_code_bindings_and_replay(self):
        code = self.code()
        for change in ({"resource": "https://evil.example/mcp"}, {"code_verifier": "x" * 43},
                       {"client_id": "other"}, {"redirect_uri": "https://client.example/other"},
                       {"resource": None}, {"scope": "admin"}):
            with self.assertRaises(AuthError):
                self.exchange(code, **change)
        tokens = self.exchange(code)
        self.assertTrue(self.auth.verify_bearer(tokens["access_token"]))
        with self.assertRaises(AuthError):
            self.exchange(code)

    def test_csrf_owner_and_single_use(self):
        flow = self.start()
        with self.assertRaises(AuthError):
            self.auth.authorize_finish(flow["ticket"], "wrong-cookie", "owner-only-secret")
        with self.assertRaises(AuthError):
            self.auth.authorize_finish(flow["ticket"], flow["cookie"], "wrong-owner")
        self.auth.authorize_finish(flow["ticket"], flow["cookie"], "owner-only-secret")
        with self.assertRaises(AuthError):
            self.auth.authorize_finish(flow["ticket"], flow["cookie"], "owner-only-secret")

    def test_failed_session_cannot_lock_out_owner(self):
        bad, legitimate = self.start(), self.start()
        for _ in range(8):
            with self.assertRaises(AuthError):
                self.auth.authorize_finish(bad["ticket"], bad["cookie"], "wrong-owner")
        result = self.auth.authorize_finish(legitimate["ticket"], legitimate["cookie"], "owner-only-secret")
        self.assertIn("code=", result)
        # A valid key can also recover a session whose failure budget was exhausted.
        self.assertIn("code=", self.auth.authorize_finish(bad["ticket"], bad["cookie"], "owner-only-secret"))

    def test_state_permissions_and_symlinks(self):
        directory = Path(self.tmp.name) / "existing"
        directory.mkdir(mode=0o755)
        instance = Auth(dict(self.config, state_directory=str(directory)))
        instance.db.close()
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((directory / "auth.sqlite").stat().st_mode & 0o777, 0o600)
        link = Path(self.tmp.name) / "linked"
        link.symlink_to(directory, target_is_directory=True)
        with self.assertRaises(ValueError):
            Auth(dict(self.config, state_directory=str(link)))
        other = Path(self.tmp.name) / "database_link"
        other.mkdir()
        (other / "auth.sqlite").symlink_to(directory / "auth.sqlite")
        with self.assertRaises(OSError):
            Auth(dict(self.config, state_directory=str(other)))

    def test_rotation_reuse_revokes_family(self):
        original = self.exchange(self.code())
        with self.assertRaises(AuthError):
            self.refresh(original["refresh_token"], client_id="other")
        with self.assertRaises(AuthError):
            self.refresh(original["refresh_token"], resource="https://evil.example/mcp")
        new = self.refresh(original["refresh_token"])
        self.assertTrue(self.auth.verify_bearer(new["access_token"]))
        with self.assertRaises(AuthError):
            self.refresh(original["refresh_token"])
        self.assertFalse(self.auth.verify_bearer(new["access_token"]))
        with self.assertRaises(AuthError):
            self.refresh(new["refresh_token"])

    def test_revocation_and_desktop(self):
        tokens = self.exchange(self.code())
        self.auth.revoke(dict(client_id="other", token=tokens["refresh_token"]))
        self.assertTrue(self.auth.verify_bearer(tokens["access_token"]))
        self.auth.revoke(dict(client_id="public", token=tokens["refresh_token"]))
        self.assertFalse(self.auth.verify_bearer(tokens["access_token"]))
        with self.assertRaises(AuthError):
            self.refresh(tokens["refresh_token"])
        self.assertTrue(self.auth.verify_bearer("desktop-secret"))
        self.assertFalse(self.auth.verify_bearer("unknown"))

    def test_expiry(self):
        import time
        now = time.time()
        flow = self.start()
        with patch("telegram_reader.auth.time.time", return_value=now + 301):
            with self.assertRaises(AuthError):
                self.auth.authorize_finish(flow["ticket"], flow["cookie"], "owner-only-secret")
        code = self.code()
        with patch("telegram_reader.auth.time.time", return_value=now + 61):
            with self.assertRaises(AuthError):
                self.exchange(code)
        tokens = self.exchange(self.code())
        with patch("telegram_reader.auth.time.time", return_value=now + 3601):
            self.assertFalse(self.auth.verify_bearer(tokens["access_token"]))
        with patch("telegram_reader.auth.time.time", return_value=now + 30 * 86400 + 1):
            with self.assertRaises(AuthError):
                self.refresh(tokens["refresh_token"])

    def test_hashes_only_persisted(self):
        flow = self.start()
        code = self.code()
        tokens = self.exchange(code)
        rows = "\n".join(self.auth.db.iterdump())
        for secret in (flow["ticket"], flow["cookie"], code, tokens["access_token"],
                       tokens["refresh_token"], "owner-only-secret", "desktop-secret", "client-secret"):
            self.assertNotIn(secret, rows)
        self.assertIn(digest(tokens["access_token"]), rows)

    def test_confidential_client(self):
        code = self.code(client_id="private", redirect_uri="https://private.example/cb")
        params = dict(grant_type="authorization_code", code=code, code_verifier=self.verifier,
                      redirect_uri="https://private.example/cb", resource=self.auth.resource)
        with self.assertRaises(AuthError):
            self.auth.grant(dict(params, client_id="private"))
        header = "Basic " + base64.b64encode(b"private:client-secret").decode()
        tokens = self.auth.grant(params, header)
        with self.assertRaises(AuthError):
            self.refresh(tokens["refresh_token"], client_id="private")
        self.auth.grant(dict(grant_type="refresh_token", refresh_token=tokens["refresh_token"],
                             resource=self.auth.resource, client_id="private", client_secret="client-secret"))

    def test_concurrent_code_exchange_is_atomic(self):
        code = self.code()
        def exchange_once(_):
            try:
                self.exchange(code)
                return True
            except AuthError:
                return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(exchange_once, range(4))), 1)

    def test_metadata_and_limits(self):
        self.assertNotIn("registration_endpoint", self.auth.metadata())
        self.assertEqual(self.auth.protected_metadata()["resource"], self.auth.resource)
        for _ in range(60):
            self.start()
        with self.assertRaises(AuthError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 429)
        self.assertLessEqual(self.auth.db.execute("SELECT COUNT(*) FROM limits").fetchone()[0], 5)


if __name__ == "__main__":
    unittest.main()
