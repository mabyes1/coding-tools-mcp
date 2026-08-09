from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt


OAUTH_CODE_TTL_SECONDS = 300
OAUTH_TOKEN_TTL_SECONDS = 24 * 60 * 60
OAUTH_REFRESH_TOKEN_TTL_SECONDS = 365 * 24 * 60 * 60
OAUTH_MAX_BODY_BYTES = 8_192
OAUTH_GRANT_TYPE_AUTHORIZATION_CODE = "authorization_code"
OAUTH_GRANT_TYPE_REFRESH_TOKEN = "refresh_token"
# Advertised in AS metadata and used to narrow DCR requests. The token endpoint
# implements authorization_code only — adding an entry here requires a matching
# branch in handle_oauth_token, not just a wider check.
OAUTH_GRANT_TYPES_SUPPORTED = (OAUTH_GRANT_TYPE_AUTHORIZATION_CODE, OAUTH_GRANT_TYPE_REFRESH_TOKEN)
OAUTH_RESPONSE_TYPES_SUPPORTED = ("code",)
MAX_REDIRECT_URIS = 10
MAX_REGISTERED_CLIENTS = 1_024
MAX_PENDING_CODES = 256


def _token_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OAuthStateStore:
    """Small durable OAuth store for the single-machine private deployment.

    JWT access tokens remain stateless and are signed by the persisted token
    secret.  This store keeps the pieces that otherwise disappear whenever the
    Windows service restarts: dynamically registered clients, pending PKCE
    authorization codes, and refresh-token families.  Raw tokens are never
    written to disk.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_clients (
                    client_id TEXT PRIMARY KEY,
                    redirect_uris TEXT NOT NULL,
                    token_endpoint_auth_method TEXT NOT NULL,
                    client_name TEXT,
                    secret_digest TEXT,
                    issued_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_pending_codes (
                    code_digest TEXT PRIMARY KEY,
                    code_challenge TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    state TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    server_url TEXT NOT NULL,
                    resource TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
                    token_digest TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    issued_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS oauth_refresh_family_idx
                    ON oauth_refresh_tokens(family_id);
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.commit()
            self._connection.close()

    def load_clients(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT client_id, redirect_uris, token_endpoint_auth_method, client_name, secret_digest, issued_at "
                "FROM oauth_clients ORDER BY issued_at"
            ).fetchall()
        clients: list[dict[str, Any]] = []
        for row in rows:
            try:
                redirect_uris = json.loads(str(row["redirect_uris"]))
            except (TypeError, json.JSONDecodeError):
                continue
            clients.append(
                {
                    "client_id": str(row["client_id"]),
                    "redirect_uris": tuple(redirect_uris),
                    "token_endpoint_auth_method": str(row["token_endpoint_auth_method"]),
                    "client_name": row["client_name"],
                    "secret_digest": row["secret_digest"],
                    "issued_at": int(row["issued_at"]),
                }
            )
        return clients

    def save_client(self, client: "OAuthClient") -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO oauth_clients "
                "(client_id, redirect_uris, token_endpoint_auth_method, client_name, secret_digest, issued_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(client_id) DO UPDATE SET "
                "redirect_uris=excluded.redirect_uris, "
                "token_endpoint_auth_method=excluded.token_endpoint_auth_method, "
                "client_name=excluded.client_name, secret_digest=excluded.secret_digest, "
                "issued_at=excluded.issued_at",
                (
                    client.client_id,
                    json.dumps(list(client.redirect_uris), separators=(",", ":")),
                    client.token_endpoint_auth_method,
                    client.client_name,
                    client.secret_digest,
                    client.issued_at,
                ),
            )
            self._connection.commit()

    def put_pending_code(self, code: str, data: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._connection.execute("DELETE FROM oauth_pending_codes WHERE expires_at <= ?", (now,))
            self._connection.execute(
                "INSERT OR REPLACE INTO oauth_pending_codes "
                "(code_digest, code_challenge, client_id, redirect_uri, state, expires_at, server_url, resource) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _token_digest(code),
                    data["code_challenge"],
                    data["client_id"],
                    data["redirect_uri"],
                    data.get("state", ""),
                    float(data["expires_at"]),
                    data["server_url"],
                    data["resource"],
                ),
            )
            self._connection.commit()

    def consume_pending_code(self, code: str) -> dict[str, Any] | None:
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT code_challenge, client_id, redirect_uri, state, expires_at, server_url, resource "
                "FROM oauth_pending_codes WHERE code_digest = ?",
                (_token_digest(code),),
            ).fetchone()
            self._connection.execute("DELETE FROM oauth_pending_codes WHERE code_digest = ?", (_token_digest(code),))
            self._connection.commit()
        if row is None or float(row["expires_at"]) < now:
            return None
        return {
            "code_challenge": str(row["code_challenge"]),
            "client_id": str(row["client_id"]),
            "redirect_uri": str(row["redirect_uri"]),
            "state": str(row["state"]),
            "expires_at": float(row["expires_at"]),
            "server_url": str(row["server_url"]),
            "resource": str(row["resource"]),
        }

    def issue_refresh_token(self, client_id: str, *, ttl: int, family_id: str | None = None) -> tuple[str, float, str]:
        token = secrets.token_urlsafe(48)
        family = family_id or secrets.token_urlsafe(24)
        issued_at = time.time()
        expires_at = issued_at + ttl
        with self._lock:
            self._connection.execute("DELETE FROM oauth_refresh_tokens WHERE expires_at <= ?", (issued_at,))
            self._connection.execute(
                "INSERT INTO oauth_refresh_tokens "
                "(token_digest, client_id, family_id, issued_at, expires_at, used_at, revoked) "
                "VALUES (?, ?, ?, ?, ?, NULL, 0)",
                (_token_digest(token), client_id, family, issued_at, expires_at),
            )
            self._connection.commit()
        return token, expires_at, family

    def consume_refresh_token(self, token: str, client_id: str) -> tuple[bool, str | None]:
        digest = _token_digest(token)
        now = time.time()
        with self._lock:
            row = self._connection.execute(
                "SELECT client_id, family_id, expires_at, used_at, revoked "
                "FROM oauth_refresh_tokens WHERE token_digest = ?",
                (digest,),
            ).fetchone()
            if row is None or str(row["client_id"]) != client_id:
                return False, None
            family_id = str(row["family_id"])
            if bool(row["revoked"]) or row["used_at"] is not None or float(row["expires_at"]) <= now:
                self._connection.execute(
                    "UPDATE oauth_refresh_tokens SET revoked = 1 WHERE family_id = ?", (family_id,)
                )
                self._connection.commit()
                return False, None
            self._connection.execute(
                "UPDATE oauth_refresh_tokens SET used_at = ? WHERE token_digest = ?", (now, digest)
            )
            self._connection.commit()
        return True, family_id


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str
    client_name: str | None = None
    secret_digest: str | None = None
    issued_at: int = field(default_factory=lambda: int(time.time()))

    def accepts_redirect(self, redirect_uri: str) -> bool:
        return redirect_uri in self.redirect_uris

    def verifies_secret(self, secret: str) -> bool:
        if self.token_endpoint_auth_method == "none":
            return not secret
        if self.secret_digest is None or not secret:
            return False
        return secrets.compare_digest(self.secret_digest, _secret_digest(secret))


class OAuthClientRegistry:
    """Thread-safe RFC 7591 client registry for one server process."""

    def __init__(self, state_store: OAuthStateStore | None = None) -> None:
        self._clients: dict[str, OAuthClient] = {}
        self._lock = threading.Lock()
        self._state_store = state_store
        if state_store is not None:
            for data in state_store.load_clients():
                try:
                    client = OAuthClient(
                        client_id=str(data["client_id"]),
                        redirect_uris=validate_redirect_uris(list(data["redirect_uris"])),
                        token_endpoint_auth_method=str(data["token_endpoint_auth_method"]),
                        client_name=data.get("client_name"),
                        secret_digest=data.get("secret_digest"),
                        issued_at=int(data["issued_at"]),
                    )
                except (TypeError, ValueError):
                    continue
                self._clients[client.client_id] = client

    def add_preregistered(
        self,
        client_id: str,
        redirect_uris: tuple[str, ...],
        *,
        client_secret: str | None,
    ) -> None:
        redirects = validate_redirect_uris(list(redirect_uris))
        method = "client_secret_post" if client_secret is not None else "none"
        client = OAuthClient(
            client_id=client_id,
            redirect_uris=redirects,
            token_endpoint_auth_method=method,
            secret_digest=_secret_digest(client_secret) if client_secret is not None else None,
        )
        with self._lock:
            self._clients[client_id] = client
        if self._state_store is not None:
            self._state_store.save_client(client)

    def register(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirects = validate_redirect_uris(metadata.get("redirect_uris"))
        requested_grant_types = metadata.get("grant_types", list(OAUTH_GRANT_TYPES_SUPPORTED))
        requested_response_types = metadata.get("response_types", list(OAUTH_RESPONSE_TYPES_SUPPORTED))
        if not isinstance(requested_grant_types, list) or not all(
            isinstance(item, str) for item in requested_grant_types
        ):
            raise ValueError("grant_types must be an array of strings")
        grant_types = tuple(item for item in OAUTH_GRANT_TYPES_SUPPORTED if item in requested_grant_types)
        if not grant_types:
            raise ValueError("grant_types must include at least one supported value")
        if not isinstance(requested_response_types, list) or not all(
            isinstance(item, str) for item in requested_response_types
        ):
            raise ValueError("response_types must be an array of strings")
        response_types = tuple(item for item in OAUTH_RESPONSE_TYPES_SUPPORTED if item in requested_response_types)
        if not response_types:
            raise ValueError("response_types must include at least one supported value")
        method = str(metadata.get("token_endpoint_auth_method") or "none")
        if method not in {"none", "client_secret_post", "client_secret_basic"}:
            raise ValueError("unsupported token_endpoint_auth_method")
        with self._lock:
            if len(self._clients) >= MAX_REGISTERED_CLIENTS:
                raise ValueError("dynamic client registration limit reached")
            client_id = secrets.token_urlsafe(24)
            while client_id in self._clients:
                client_id = secrets.token_urlsafe(24)
            client_secret = secrets.token_urlsafe(32) if method != "none" else None
            client = OAuthClient(
                client_id=client_id,
                redirect_uris=redirects,
                token_endpoint_auth_method=method,
                client_name=_optional_text(metadata.get("client_name"), 200),
                secret_digest=_secret_digest(client_secret) if client_secret is not None else None,
            )
            self._clients[client_id] = client
            if self._state_store is not None:
                self._state_store.save_client(client)
        response: dict[str, Any] = {
            "client_id": client.client_id,
            "client_id_issued_at": client.issued_at,
            "redirect_uris": list(client.redirect_uris),
            "grant_types": list(grant_types),
            "response_types": list(response_types),
            "token_endpoint_auth_method": client.token_endpoint_auth_method,
        }
        if client.client_name:
            response["client_name"] = client.client_name
        if client_secret is not None:
            response["client_secret"] = client_secret
            response["client_secret_expires_at"] = 0
        return response

    def get(self, client_id: str) -> OAuthClient | None:
        with self._lock:
            return self._clients.get(client_id)

    def accepts_redirect(self, client_id: str, redirect_uri: str) -> bool:
        client = self.get(client_id)
        return client is not None and client.accepts_redirect(redirect_uri)

    def authenticates(self, client_id: str, client_secret: str, auth_method: str) -> bool:
        client = self.get(client_id)
        return (
            client is not None
            and client.token_endpoint_auth_method == auth_method
            and client.verifies_secret(client_secret)
        )


@dataclass(frozen=True)
class OAuthConfig:
    password: str
    server_url: str | None
    token_secret: bytes
    token_ttl: int = OAUTH_TOKEN_TTL_SECONDS
    refresh_token_ttl: int = OAUTH_REFRESH_TOKEN_TTL_SECONDS
    state_store: OAuthStateStore | None = None
    registry: OAuthClientRegistry = field(default_factory=OAuthClientRegistry)
    pending_codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_codes_lock: threading.Lock = field(default_factory=threading.Lock)

    def put_pending_code(self, code: str, data: dict[str, Any]) -> None:
        if self.state_store is not None:
            self.state_store.put_pending_code(code, data)
            return
        with self.pending_codes_lock:
            self.pending_codes[code] = data

    def consume_pending_code(self, code: str) -> dict[str, Any] | None:
        if self.state_store is not None:
            return self.state_store.consume_pending_code(code)
        with self.pending_codes_lock:
            return self.pending_codes.pop(code, None)

    def close(self) -> None:
        if self.state_store is not None:
            self.state_store.close()


def validate_redirect_uris(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_REDIRECT_URIS:
        raise ValueError(f"redirect_uris must contain between 1 and {MAX_REDIRECT_URIS} entries")
    redirects: list[str] = []
    for item in value:
        if not isinstance(item, str) or len(item) > 2048:
            raise ValueError("redirect_uri must be a string of at most 2048 characters")
        parsed = urllib.parse.urlsplit(item)
        if parsed.fragment or not parsed.scheme or not parsed.netloc or not parsed.hostname:
            raise ValueError("redirect_uri must be an absolute URI without a fragment")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("redirect_uri must not contain user information")
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("HTTP redirect_uri is allowed only for loopback hosts")
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("redirect_uri must use HTTPS or loopback HTTP")
        redirects.append(item)
    if len(set(redirects)) != len(redirects):
        raise ValueError("redirect_uris must be unique")
    return tuple(redirects)


def verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", code_verifier):
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, code_challenge)


def valid_pkce_challenge(code_challenge: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge) is not None


def create_access_token(config: OAuthConfig, server_url: str, *, client_id: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": server_url,
            "aud": server_url,
            "sub": client_id,
            "client_id": client_id,
            "iat": now,
            "exp": now + config.token_ttl,
            "scope": "mcp",
        },
        config.token_secret,
        algorithm="HS256",
    )


def validate_access_token(token: str, config: OAuthConfig, server_url: str) -> bool:
    return access_token_client_id(token, config, server_url) is not None


def access_token_client_id(token: str, config: OAuthConfig, server_url: str) -> str | None:
    """Return the registered OAuth client id for a valid bearer token."""
    try:
        claims = jwt.decode(
            token,
            config.token_secret,
            algorithms=["HS256"],
            audience=server_url,
            issuer=server_url,
        )
    except jwt.PyJWTError:
        return None
    client_id = claims.get("client_id")
    if not isinstance(client_id, str) or config.registry.get(client_id) is None:
        return None
    return client_id


def _secret_digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _optional_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:maximum]
