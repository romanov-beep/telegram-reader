"""Single-owner, pre-registered-client OAuth 2.1 with opaque hashed credentials."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode


class AuthError(Exception):
    def __init__(self, error, description, status=400):
        super().__init__(description)
        self.status, self.error, self.description = status, error, description


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _eq(left, right):
    return hmac.compare_digest(_hash(left), _hash(right))


class Auth:
    SCOPE = "telegram.read"

    def __init__(self, config):
        self.resource = config["public_url"].rstrip("/") + "/mcp"
        self.issuer = config["public_url"].rstrip("/")
        self.clients = {c["client_id"]: c for c in config["oauth_clients"]}
        self.owner_hash = config["owner_secret_sha256"]
        self.desktop_hash = config.get("desktop_token_sha256")
        directory = Path(config["state_directory"])
        if directory.is_symlink():
            raise ValueError("State directory must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(directory_fd, 0o700)
            database_fd = os.open("auth.sqlite", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                                  0o600, dir_fd=directory_fd)
            try:
                os.fchmod(database_fd, 0o600)
            finally:
                os.close(database_fd)
        finally:
            os.close(directory_fd)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(directory / "auth.sqlite"), check_same_thread=False,
                                  isolation_level=None, timeout=15)
        (directory / "auth.sqlite").chmod(0o600)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS pending (
              ticket TEXT PRIMARY KEY, cookie TEXT, client TEXT, redirect TEXT,
              challenge TEXT, state TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS codes (
              digest TEXT PRIMARY KEY, client TEXT, redirect TEXT, challenge TEXT,
              resource TEXT, scope TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS access_tokens (
              digest TEXT PRIMARY KEY, client TEXT, family TEXT, resource TEXT,
              scope TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS refresh_tokens (
              digest TEXT PRIMARY KEY, client TEXT, family TEXT, resource TEXT,
              scope TEXT, expires REAL, used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS limits (
              name TEXT PRIMARY KEY, start REAL, count INTEGER);
            CREATE INDEX IF NOT EXISTS pending_expiry ON pending(expires);
            CREATE INDEX IF NOT EXISTS codes_expiry ON codes(expires);
            CREATE INDEX IF NOT EXISTS access_expiry ON access_tokens(expires);
            CREATE INDEX IF NOT EXISTS refresh_expiry ON refresh_tokens(expires);
            CREATE INDEX IF NOT EXISTS access_family ON access_tokens(family);
            CREATE INDEX IF NOT EXISTS refresh_family ON refresh_tokens(family);
        """)

    @contextmanager
    def _transaction(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def _cleanup(self):
        now = time.time()
        for table in ("pending", "codes", "access_tokens", "refresh_tokens"):
            self.db.execute("DELETE FROM " + table + " WHERE expires <= ?", (now,))
        self.db.execute("DELETE FROM limits WHERE start <= ?", (now - 300,))

    def _rate(self, name, maximum, window=60):
        # Separate committed transaction: rejected requests cannot roll back counters.
        with self._transaction():
            now = time.time()
            self.db.execute("DELETE FROM limits WHERE start <= ?", (now - 300,))
            row = self.db.execute("SELECT start,count FROM limits WHERE name=?", (name,)).fetchone()
            start, count = row if row and row[0] > now - window else (now, 0)
            self.db.execute("INSERT OR REPLACE INTO limits VALUES (?,?,?)", (name, start, count + 1))
        if count >= maximum:
            raise AuthError("temporarily_unavailable", "Too many requests; try again later", 429)

    def metadata(self):
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + "/authorize",
            "token_endpoint": self.issuer + "/token",
            "revocation_endpoint": self.issuer + "/revoke",
            "response_types_supported": ["code"],
            "authorization_response_iss_parameter_supported": True,
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": [self.SCOPE],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
        }

    def protected_metadata(self):
        return {"resource": self.resource, "authorization_servers": [self.issuer],
                "scopes_supported": [self.SCOPE], "bearer_methods_supported": ["header"]}

    def _resource(self, params):
        if params.get("resource") != self.resource:
            raise AuthError("invalid_target", "The exact MCP resource is required")

    def authorize_start(self, params):
        self._rate("start", 60)
        client = self.clients.get(params.get("client_id"))
        redirect = params.get("redirect_uri")
        if not client or redirect not in client["redirect_uris"]:
            raise AuthError("invalid_request", "Unknown client or redirect URI")
        self._resource(params)
        if params.get("scope") != self.SCOPE:
            raise AuthError("invalid_scope", "Only telegram.read is supported")
        if params.get("response_type") != "code":
            raise AuthError("unsupported_response_type", "Only code is supported")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            raise AuthError("invalid_request", "S256 PKCE is required")
        state = params.get("state", "")
        if len(state) > 2048:
            raise AuthError("invalid_request", "State is too long")
        ticket, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._transaction():
            self._cleanup()
            if self.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0] >= 256:
                raise AuthError("temporarily_unavailable", "Too many pending authorizations", 429)
            self.db.execute("INSERT INTO pending VALUES (?,?,?,?,?,?,?)", (
                _hash(ticket), _hash(cookie), client["client_id"], redirect, challenge, state, time.time() + 300))
        return {"ticket": ticket, "cookie": cookie, "client_id": client["client_id"], "redirect_uri": redirect}

    def authorize_finish(self, ticket, cookie, owner_secret):
        with self._transaction():
            self._cleanup()
            row = self.db.execute("SELECT cookie,client,redirect,challenge,state FROM pending WHERE ticket=?",
                                  (_hash(ticket),)).fetchone()
        if not row or not hmac.compare_digest(row[0], _hash(cookie)):
            self._rate("invalid_approve", 120)
            raise AuthError("invalid_request", "Invalid or expired authorization session")
        if not hmac.compare_digest(_hash(owner_secret), self.owner_hash):
            self._rate("owner_failure", 120)
            self._rate("owner_failure:" + _hash(ticket), 5, 300)
            raise AuthError("access_denied", "Invalid owner approval secret", 403)
        # Correct owner credentials remain usable even when other sessions are abused.
        with self._transaction():
            self._cleanup()
            if not self.db.execute("DELETE FROM pending WHERE ticket=? AND expires>?", (_hash(ticket), time.time())).rowcount:
                raise AuthError("invalid_request", "Invalid or expired authorization session")
            code = secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO codes VALUES (?,?,?,?,?,?,?)", (_hash(code), row[1], row[2], row[3], self.resource, self.SCOPE, time.time() + 60))
        # Preserve registered URI query exactly and append only protocol response fields.
        return row[2] + ("&" if "?" in row[2] else "?") + urlencode({"code": code, "state": row[4], "iss": self.issuer})

    def _client(self, params, basic_header):
        client_id, secret = params.get("client_id"), params.get("client_secret")
        if basic_header:
            if secret is not None or not basic_header.startswith("Basic "):
                raise AuthError("invalid_client", "Invalid client authentication", 401)
            try:
                from urllib.parse import unquote_plus
                decoded = base64.b64decode(basic_header[6:], validate=True).decode("utf-8")
                ident, secret = decoded.split(":", 1)
                ident, secret = unquote_plus(ident), unquote_plus(secret)
            except (ValueError, UnicodeError):
                raise AuthError("invalid_client", "Invalid client authentication", 401)
            if client_id is not None and not _eq(client_id, ident):
                raise AuthError("invalid_client", "Conflicting client identifiers", 401)
            client_id = ident
        client = self.clients.get(client_id)
        expected = client.get("client_secret_sha256") if client else None
        if not client or (expected and (secret is None or not hmac.compare_digest(_hash(secret), expected))) or (not expected and secret is not None):
            raise AuthError("invalid_client", "Invalid client authentication", 401)
        return client_id

    def _issue(self, client, family=None):
        family = family or secrets.token_hex(24)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        self.db.execute("INSERT INTO access_tokens VALUES (?,?,?,?,?,?)", (_hash(access), client, family, self.resource, self.SCOPE, now + 3600))
        self.db.execute("INSERT INTO refresh_tokens VALUES (?,?,?,?,?,?,0)", (_hash(refresh), client, family, self.resource, self.SCOPE, now + 30 * 86400))
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600, "scope": self.SCOPE}

    def _revoke_family(self, family):
        self.db.execute("DELETE FROM access_tokens WHERE family=?", (family,))
        self.db.execute("DELETE FROM refresh_tokens WHERE family=?", (family,))

    def grant(self, params, basic_header=None):
        self._rate("grant", 120)
        client = self._client(params, basic_header)
        self._resource(params)
        if "scope" in params and params["scope"] != self.SCOPE:
            raise AuthError("invalid_scope", "Only telegram.read is supported")
        error = None
        with self._transaction():
            self._cleanup()
            kind = params.get("grant_type")
            if kind == "authorization_code":
                digest = _hash(params.get("code", ""))
                row = self.db.execute("SELECT client,redirect,challenge,resource,scope FROM codes WHERE digest=?", (digest,)).fetchone()
                verifier = params.get("code_verifier", "")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if not row or row[0] != client or row[1] != params.get("redirect_uri") or row[3] != self.resource or row[4] != self.SCOPE or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier) or not _eq(row[2], challenge):
                    raise AuthError("invalid_grant", "Invalid authorization code or PKCE binding")
                self.db.execute("DELETE FROM codes WHERE digest=?", (digest,))
                result = self._issue(client)
            elif kind == "refresh_token":
                digest = _hash(params.get("refresh_token", ""))
                row = self.db.execute("SELECT client,family,resource,scope,used FROM refresh_tokens WHERE digest=?", (digest,)).fetchone()
                if not row or row[0] != client or row[2] != self.resource or row[3] != self.SCOPE:
                    raise AuthError("invalid_grant", "Invalid refresh token")
                if row[4]:
                    self._revoke_family(row[1])
                    error = AuthError("invalid_grant", "Refresh token reuse; authorization revoked")
                else:
                    self.db.execute("UPDATE refresh_tokens SET used=1 WHERE digest=?", (digest,))
                    result = self._issue(client, row[1])
            else:
                raise AuthError("unsupported_grant_type", "Unsupported grant type")
        if error:
            raise error
        return result

    def verify_bearer(self, rawtoken):
        digest = _hash(rawtoken)
        if self.desktop_hash and hmac.compare_digest(digest, self.desktop_hash):
            return True
        with self._transaction():
            self._cleanup()
            row = self.db.execute("SELECT resource,scope FROM access_tokens WHERE digest=? AND expires>?", (digest, time.time())).fetchone()
            return bool(row and row[0] == self.resource and row[1] == self.SCOPE)

    def revoke(self, params, basic_header=None):
        self._rate("revoke", 120)
        client = self._client(params, basic_header)
        digest = _hash(params.get("token", ""))
        with self._transaction():
            self._cleanup()
            for table in ("access_tokens", "refresh_tokens"):
                row = self.db.execute("SELECT family FROM " + table + " WHERE digest=? AND client=?", (digest, client)).fetchone()
                if row:
                    self._revoke_family(row[0])
                    break
        return {}
