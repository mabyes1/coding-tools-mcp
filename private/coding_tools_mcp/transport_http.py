from __future__ import annotations

import functools
import json
import os
import threading
import time
from collections import deque
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .envutils import ENV_PREFIX


# These defaults are intentionally tuned for ChatGPT Web's reconnect pattern:
# stale transport sessions are cheap, bounded, and never allowed to consume
# the process indefinitely.  They are configuration values so the single-user
# service can be adjusted without another source edit.
MAX_HTTP_SESSIONS = 256
# Keep an idle MCP transport alive across normal thinking/tool gaps. In-flight
# requests still have a shorter watchdog below, while owner/global caps evict
# idle records before they can exhaust the process.
HTTP_SESSION_TTL_SECONDS = 5 * 60
HTTP_IN_FLIGHT_TTL_SECONDS = 90
MAX_HTTP_SESSIONS_PER_OWNER = 64
MCP_ENDPOINT_PATH = "/mcp"
MAX_HTTP_REQUEST_BYTES = 1_048_576


def http_base_for_bind_host(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def write_http_body_safely(handler: Any, body: bytes) -> bool:
    """Treat a client disappearing mid-response as a normal disconnect."""
    try:
        handler.wfile.write(body)
        return True
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        handler.close_connection = True
        return False


def first_header_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def first_form_value(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key)
    return values[0] if values else ""


def forwarded_header_param(value: str | None, name: str) -> str:
    first = first_header_value(value)
    for part in first.split(";"):
        key, sep, raw = part.strip().partition("=")
        if sep and key.lower() == name:
            return raw.strip().strip('"')
    return ""


def safe_external_host(host: str) -> str:
    host = host.strip()
    if not host or any(ch.isspace() or ch in "/\\@?#" for ch in host):
        return ""
    try:
        parsed = urllib.parse.urlsplit(f"//{host}")
        _ = parsed.port
    except ValueError:
        return ""
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return ""
    return host


def _normalize_modern_discover_payload(payload: Any) -> Any:
    """Normalize the server/discover wire shape to final MCP 2026-07-28."""
    if not isinstance(payload, dict):
        return payload
    raw_result = payload.get("result")
    if not isinstance(raw_result, dict) or "supportedVersions" not in raw_result:
        return payload
    # Discovery is the only response with supportedVersions.  The final
    # 2026-07-28 wire format requires cache hints and moved server identity
    # from the RC top-level serverInfo field into the reserved result _meta.
    result = dict(raw_result)
    result.setdefault("resultType", "complete")
    result.setdefault("ttlMs", 0)
    result.setdefault("cacheScope", "private")
    server_info = result.pop("serverInfo", None)
    if isinstance(server_info, dict):
        raw_meta = result.get("_meta")
        meta = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        meta.setdefault("io.modelcontextprotocol/serverInfo", server_info)
        result["_meta"] = meta
    normalized = dict(payload)
    normalized["result"] = result
    return normalized


def json_response_payload(payload: Any) -> bytes:
    payload = _normalize_modern_discover_payload(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@functools.lru_cache(maxsize=8)
def _configured_allowed_origins(raw: str) -> frozenset[str]:
    return frozenset(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


def is_allowed_origin(origin: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    normalized = origin.rstrip("/")
    configured = _configured_allowed_origins(os.environ.get(f"{ENV_PREFIX}_ALLOWED_ORIGINS", ""))
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"} or normalized in configured


def is_loopback_bind_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", ""}


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int((os.environ.get(name) or "").strip())
    except ValueError:
        return default
    return value if minimum <= value <= maximum else default


def _close_runtime(runtime: Any) -> None:
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


class SessionCapacityError(RuntimeError):
    """Raised only when every bounded session slot is currently in use."""

    retry_after = 5


class SlidingWindowRateLimiter:
    """Small process-local limiter for a single public personal endpoint."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self.rejected = 0

    def allow(self, key: str, *, limit: int, window_seconds: float) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                self.rejected += 1
                return False
            events.append(now)
            if len(self._events) > 1024:
                self._events = {
                    name: values for name, values in self._events.items() if values and values[-1] > cutoff
                }
            return True


@dataclass(frozen=True)
class HTTPSessionBinding:
    runtime: Any
    session_id: str


@dataclass
class HTTPSessionRecord:
    runtime: Any
    session_id: str
    last_seen: float
    owner: str | None
    in_flight: int = 0
    in_flight_since: float | None = None


class HTTPSessionManager:
    """Own bounded HTTP runtimes while sharing the execution registry.

    Each MCP transport session still receives its own Runtime handshake state,
    protocol version and session id.  Runtimes created by the HTTP server share
    the server's execution registry, so expiring a stale Web GPT connection no
    longer kills or hides an in-flight command from the next connection.
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        max_sessions: int | None = None,
        session_ttl_seconds: int | None = None,
        in_flight_ttl_seconds: int | None = None,
        max_sessions_per_owner: int | None = None,
    ) -> None:
        self._factory = factory
        self.max_sessions = max_sessions or _bounded_env_int(
            "CODING_TOOLS_MCP_MAX_HTTP_SESSIONS", MAX_HTTP_SESSIONS, 8, 4096
        )
        self.session_ttl_seconds = session_ttl_seconds or _bounded_env_int(
            "CODING_TOOLS_MCP_HTTP_SESSION_TTL_SECONDS", HTTP_SESSION_TTL_SECONDS, 30, 86_400
        )
        self.in_flight_ttl_seconds = in_flight_ttl_seconds or _bounded_env_int(
            "CODING_TOOLS_MCP_HTTP_IN_FLIGHT_TTL_SECONDS", HTTP_IN_FLIGHT_TTL_SECONDS, 60, 3_600
        )
        self.max_sessions_per_owner = max_sessions_per_owner or _bounded_env_int(
            "CODING_TOOLS_MCP_MAX_HTTP_SESSIONS_PER_OWNER", MAX_HTTP_SESSIONS_PER_OWNER, 1, 256
        )
        self._sessions: dict[str, HTTPSessionRecord] = {}
        self._lock = threading.Lock()
        self._creating = 0
        self._closed = False
        self._expired = 0
        self._stale_in_flight_evicted = 0
        self._capacity_evicted = 0
        self._rejected = 0

    def create(self, owner: str | None = None, *, acquire: bool = False) -> HTTPSessionBinding:
        stale_records: list[HTTPSessionRecord] = []
        self.prune()
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP session manager is closed")
            owner_records = [record for record in self._sessions.values() if record.owner == owner]
            victim: HTTPSessionRecord | None = None
            if owner is not None and len(owner_records) >= self.max_sessions_per_owner:
                idle_owner_records = [record for record in owner_records if record.in_flight == 0]
                if idle_owner_records:
                    victim = min(idle_owner_records, key=lambda record: record.last_seen)
                else:
                    self._rejected += 1
                    raise SessionCapacityError("maximum active HTTP sessions for owner reached")
            elif len(self._sessions) + self._creating >= self.max_sessions:
                idle_records = [record for record in self._sessions.values() if record.in_flight == 0]
                if idle_records:
                    victim = min(idle_records, key=lambda record: record.last_seen)
                else:
                    self._rejected += 1
                    raise SessionCapacityError("maximum HTTP session count reached")
            if victim is not None:
                self._sessions.pop(victim.session_id, None)
                stale_records.append(victim)
                self._capacity_evicted += 1
            if len(self._sessions) + self._creating >= self.max_sessions:
                self._rejected += 1
                raise SessionCapacityError("maximum HTTP session count reached")
            self._creating += 1
        for record in stale_records:
            _close_runtime(record.runtime)

        runtime: Any | None = None
        installed = False
        try:
            runtime = self._factory()
            if owner is not None:
                runtime.state_owner = owner
            session_id = str(runtime.http_session_id)
            now = time.time()
            record = HTTPSessionRecord(
                runtime=runtime,
                session_id=session_id,
                last_seen=now,
                owner=owner,
                in_flight=1 if acquire else 0,
                in_flight_since=now if acquire else None,
            )
            with self._lock:
                if self._closed:
                    raise RuntimeError("HTTP session manager is closed")
                if session_id in self._sessions:
                    raise RuntimeError("duplicate HTTP session identifier")
                self._sessions[session_id] = record
                installed = True
            return HTTPSessionBinding(runtime=runtime, session_id=session_id)
        finally:
            with self._lock:
                self._creating -= 1
            if runtime is not None and not installed:
                _close_runtime(runtime)

    def get(self, session_id: str) -> HTTPSessionBinding | None:
        self.prune()
        with self._lock:
            if self._closed:
                return None
            record = self._sessions.get(session_id)
            if record is None:
                return None
            record.last_seen = time.time()
            return HTTPSessionBinding(runtime=record.runtime, session_id=record.session_id)

    def acquire(self, session_id: str) -> HTTPSessionBinding | None:
        self.prune()
        with self._lock:
            if self._closed:
                return None
            record = self._sessions.get(session_id)
            if record is None:
                return None
            now = time.time()
            record.last_seen = now
            if record.in_flight == 0:
                record.in_flight_since = now
            record.in_flight += 1
            return HTTPSessionBinding(runtime=record.runtime, session_id=record.session_id)

    def release(self, session_id: str) -> None:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return
            record.in_flight = max(0, record.in_flight - 1)
            record.last_seen = time.time()
            if record.in_flight == 0:
                record.in_flight_since = None

    def delete(self, session_id: str) -> bool:
        with self._lock:
            record = self._sessions.pop(session_id, None)
        if record is None:
            return False
        _close_runtime(record.runtime)
        return True

    def prune(self) -> None:
        now = time.time()
        cutoff = now - self.session_ttl_seconds
        in_flight_cutoff = now - self.in_flight_ttl_seconds
        with self._lock:
            expired_ids = [
                session_id
                for session_id, record in self._sessions.items()
                if record.in_flight == 0 and record.last_seen < cutoff
            ]
            stale_in_flight_ids = [
                session_id
                for session_id, record in self._sessions.items()
                if record.in_flight > 0
                and record.in_flight_since is not None
                and record.in_flight_since < in_flight_cutoff
            ]
            expired_records = [self._sessions.pop(session_id) for session_id in expired_ids]
            stale_records = [self._sessions.pop(session_id) for session_id in stale_in_flight_ids]
            self._expired += len(expired_records)
            self._stale_in_flight_evicted += len(stale_records)
        for record in expired_records + stale_records:
            _close_runtime(record.runtime)

    def stats(self) -> dict[str, int | float]:
        self.prune()
        now = time.time()
        with self._lock:
            ages = [max(0.0, now - record.last_seen) for record in self._sessions.values()]
            in_flight_ages = [
                max(0.0, now - record.in_flight_since)
                for record in self._sessions.values()
                if record.in_flight > 0 and record.in_flight_since is not None
            ]
            return {
                "active": len(self._sessions),
                "in_flight": sum(record.in_flight for record in self._sessions.values()),
                "creating": self._creating,
                "max": self.max_sessions,
                "ttl_seconds": self.session_ttl_seconds,
                "in_flight_ttl_seconds": self.in_flight_ttl_seconds,
                "oldest_age_seconds": max(ages, default=0.0),
                "oldest_in_flight_seconds": max(in_flight_ages, default=0.0),
                "expired": self._expired,
                "stale_in_flight_evicted": self._stale_in_flight_evicted,
                "capacity_evicted": self._capacity_evicted,
                "rejected": self._rejected,
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            records = list(self._sessions.values())
            self._sessions.clear()
        for record in records:
            _close_runtime(record.runtime)
