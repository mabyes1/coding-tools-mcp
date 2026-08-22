from __future__ import annotations

import hashlib
import http.server
import json
import os
import posixpath
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, cast

from . import __version__
from .envutils import ENV_PREFIX
from .errors import JsonRpcError, summarize_exception
from .execution import MAX_ACTIVE_EXEC_SESSIONS
from .oauth import access_token_client_id, validate_access_token
from .oauth_http import OAuthHTTPMixin
from .protocol import (
    PROTOCOL_VERSION,
    STATELESS_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    dispatch_rpc,
    jsonrpc_error,
    protocol_version_is_supported,
    response_id,
    validate_rpc_envelope,
)
from .runtime_meta import SERVER_NAME, SERVER_TITLE, runtime_build_identity, runtime_version
from .tool_catalog import tool_annotations
from .transport_http import (
    MAX_HTTP_REQUEST_BYTES,
    MCP_ENDPOINT_PATH,
    HTTPSessionManager,
    SessionCapacityError,
    SlidingWindowRateLimiter,
    is_allowed_origin,
    json_response_payload,
    write_http_body_safely as _write_http_body_safely,
)
from .workspace import workspace_catalog_from_env


Runtime = Any


def _server_card_auth(runtime: Any, *, oauth_base_url: str | None = None) -> dict[str, Any]:
    if runtime.oauth_enabled():
        cfg = runtime.oauth_config
        assert cfg is not None
        base = (oauth_base_url or cfg.server_url or "").rstrip("/")
        return {
            "type": "oauth2",
            "scheme": "Bearer",
            "header": "Authorization",
            "authorizationUrl": f"{base}/oauth/authorize",
            "tokenUrl": f"{base}/oauth/token",
        }
    if runtime.auth_token is not None:
        return {"type": "bearer", "scheme": "Bearer", "header": "Authorization"}
    return {"type": "none", "scheme": None, "header": None}


def server_card_payload(runtime: Any, *, oauth_base_url: str | None = None) -> dict[str, Any]:
    names = runtime.exposed_tool_names()
    annotations = {name: tool_annotations(name, fake_readonly=False) for name in names}
    read_only = [name for name in names if annotations[name].get("readOnlyHint") is True]
    mutating = [name for name in names if annotations[name].get("readOnlyHint") is not True]
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "server": {
            "name": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": runtime_version(),
        },
        "transport": {
            "type": "streamable_http",
            "endpoint": MCP_ENDPOINT_PATH,
            "methods": ["POST", "DELETE", "OPTIONS"],
        },
        "auth": _server_card_auth(runtime, oauth_base_url=oauth_base_url),
        "tools": {
            "count": len(names),
            "names": names,
            "readOnlyHintTrue": read_only,
            "readOnlyHintFalse": mutating,
            "annotationOverride": ("fake_readonly" if runtime.fake_readonly_annotations else None),
        },
        "capabilities": {"tools": {"listChanged": True}},
    }


class MCPHandler(OAuthHTTPMixin, http.server.BaseHTTPRequestHandler):
    server_version = f"CodingToolsMCP/{__version__}"
    protocol_version = "HTTP/1.1"
    timeout = 90

    @property
    def runtime(self) -> Runtime:
        return cast(Runtime, getattr(self, "_runtime", self.server.control_runtime))  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args, file=sys.stderr)

    def send_rpc_error(
        self,
        code: int,
        message: str,
        *,
        status: int = 400,
        request_id: str | int | None = None,
        data: Any = None,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_json(
            jsonrpc_error(request_id, code, message, data),
            status=status,
            extra_headers=extra_headers,
            head_only=head_only,
        )

    def do_GET(self) -> None:
        self.handle_metadata_request(head_only=False)

    def do_HEAD(self) -> None:
        self.handle_metadata_request(head_only=True)

    def do_DELETE(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if posixpath.normpath(request_path) != MCP_ENDPOINT_PATH:
            self.send_json({"error": "Unknown endpoint"}, status=404)
            return
        if not self.is_authorized():
            self.send_unauthorized()
            return
        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id or not self.server.sessions.delete(session_id):  # type: ignore[attr-defined]
            self.send_rpc_error(-32001, "Unknown MCP session", status=404)
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_cors_headers()
        self.end_headers()

    def do_OPTIONS(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if posixpath.normpath(request_path) not in {
            MCP_ENDPOINT_PATH,
            "/.well-known/mcp.json",
            "/.well-known/mcp/server-card.json",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/oauth/authorize",
            "/oauth/token",
            "/oauth/register",
        }:
            self.send_json({"error": "Unknown endpoint"}, status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not is_allowed_origin(origin):
            self.send_json({"error": "Origin denied"}, status=403)
            return
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_cors_headers()
        self.end_headers()

    def _read_mcp_body(self) -> bytes | None:
        """Read an MCP request body with both fixed-length and chunked HTTP framing."""

        transfer_encoding = self.headers.get("Transfer-Encoding", "").strip().lower()
        raw_length = self.headers.get("Content-Length")
        if transfer_encoding:
            codings = [item.strip() for item in transfer_encoding.split(",") if item.strip()]
            if codings != ["chunked"] or raw_length is not None:
                self.close_connection = True
                self.send_rpc_error(-32600, "Only chunked Transfer-Encoding is supported", status=400)
                return None
            body = bytearray()
            while True:
                line = self.rfile.readline(8192)
                if not line or not line.endswith(b"\r\n"):
                    self.close_connection = True
                    self.send_rpc_error(-32700, "Malformed chunked request body")
                    return None
                size_text = line[:-2].split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    self.close_connection = True
                    self.send_rpc_error(-32700, "Malformed chunk size")
                    return None
                if size < 0 or len(body) + size > MAX_HTTP_REQUEST_BYTES:
                    self.close_connection = True
                    self.send_rpc_error(
                        -32600,
                        "Request body exceeds maximum size",
                        status=413,
                        data={"max_bytes": MAX_HTTP_REQUEST_BYTES},
                    )
                    return None
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(8192)
                        if not trailer:
                            self.close_connection = True
                            self.send_rpc_error(-32700, "Malformed chunked trailers")
                            return None
                        if trailer in {b"\r\n", b"\n"}:
                            return bytes(body)
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    self.close_connection = True
                    self.send_rpc_error(-32700, "Malformed chunked request body")
                    return None
                body.extend(chunk)
        if raw_length is None:
            self.close_connection = True
            self.send_rpc_error(-32600, "Content-Length or chunked Transfer-Encoding is required", status=411)
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self.close_connection = True
            self.send_rpc_error(-32600, "Content-Length must be a non-negative integer")
            return None
        if length < 0:
            self.close_connection = True
            self.send_rpc_error(-32600, "Content-Length must be a non-negative integer")
            return None
        if length > MAX_HTTP_REQUEST_BYTES:
            self.close_connection = True
            self.send_rpc_error(
                -32600,
                "Request body exceeds maximum size",
                status=413,
                data={"max_bytes": MAX_HTTP_REQUEST_BYTES},
            )
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            self.send_rpc_error(-32700, "Incomplete request body")
            return None
        return body

    def handle_metadata_request(self, *, head_only: bool) -> None:
        request_path = self.path.split("?", 1)[0]
        normalized = posixpath.normpath(request_path)
        if normalized == "/.well-known/oauth-authorization-server":
            self.handle_oauth_as_metadata(head_only=head_only)
            return
        if normalized == "/.well-known/oauth-protected-resource":
            self.handle_oauth_resource_metadata(head_only=head_only)
            return
        if normalized == "/oauth/authorize" and not head_only:
            self.handle_oauth_authorize_get()
            return
        if normalized == MCP_ENDPOINT_PATH:
            origin = self.headers.get("Origin")
            if origin and not is_allowed_origin(origin):
                self.send_json({"error": "Origin denied"}, status=403, head_only=head_only)
                return
            if not self.is_authorized():
                self.send_unauthorized(head_only=head_only)
                return
            if not head_only and "text/event-stream" in self.headers.get("Accept", "").lower():
                session_id = self.headers.get("Mcp-Session-Id")
                if not session_id or self.server.sessions.get(session_id) is None:  # type: ignore[attr-defined]
                    self.send_rpc_error(-32001, "Unknown MCP session", status=404)
                    return
                self.handle_tool_notification_stream(session_id)
                return
            self.send_rpc_error(
                -32000,
                "GET /mcp requires Accept: text/event-stream and a valid Mcp-Session-Id",
                status=405,
                extra_headers={"Allow": "POST, DELETE"},
                head_only=head_only,
            )
            return
        if normalized in {"/.well-known/mcp.json", "/.well-known/mcp/server-card.json"}:
            self.send_json(server_card_payload(self.runtime, oauth_base_url=self.oauth_base_url()), head_only=head_only)
            return
        self.send_json({"error": "Unknown endpoint"}, status=404, head_only=head_only)

    def handle_tool_notification_stream(self, session_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Mcp-Session-Id", session_id)
        self.send_cors_headers()
        self.end_headers()
        try:
            self.connection.settimeout(None)
        except OSError:
            pass
        generation = self.server.tool_list_generation  # type: ignore[attr-defined]
        try:
            while True:
                if self.server.sessions.get(session_id) is None:  # type: ignore[attr-defined]
                    return
                next_generation = self.server.wait_for_tool_list_change(generation, timeout=15.0)  # type: ignore[attr-defined]
                if next_generation != generation:
                    payload = json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}},
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.wfile.write(b"event: message\n")
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    generation = next_generation
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self) -> None:
        request_path = self.path.split("?", 1)[0]
        normalized = posixpath.normpath(request_path)
        if normalized == "/oauth/authorize":
            self.handle_oauth_authorize_post()
            return
        if normalized == "/oauth/token":
            self.handle_oauth_token()
            return
        if normalized == "/oauth/register":
            self.handle_oauth_register()
            return
        if normalized != MCP_ENDPOINT_PATH:
            self.send_rpc_error(-32601, "Unknown endpoint", status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not is_allowed_origin(origin):
            self.close_connection = True
            self.send_rpc_error(-32600, "Origin denied", status=403)
            return
        if not self.is_authorized():
            self.close_connection = True
            self.send_unauthorized()
            return
        if self.headers.get_content_type().lower() != "application/json":
            self.close_connection = True
            self.send_rpc_error(-32600, "Content-Type must be application/json", status=415)
            return
        protocol_version = self.headers.get("MCP-Protocol-Version")
        if protocol_version and not protocol_version_is_supported(protocol_version):
            self.close_connection = True
            self.send_rpc_error(
                -32600,
                "Unsupported MCP protocol version",
                data={"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "received": protocol_version},
            )
            return
        body = self._read_mcp_body()
        if body is None:
            return
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_rpc_error(-32700, "Parse error")
            return
        if isinstance(request, list):
            self.send_rpc_error(-32600, "JSON-RPC batch requests are not supported by Streamable HTTP")
            return
        if not isinstance(request, dict):
            self.send_rpc_error(-32600, "Invalid Request")
            return
        try:
            validate_rpc_envelope(request)
        except JsonRpcError as exc:
            self.send_rpc_error(exc.code, exc.message, status=200, request_id=response_id(request), data=exc.data)
            return
        method = request.get("method")
        session_id = self.headers.get("Mcp-Session-Id")
        request_params = request.get("params") if isinstance(request.get("params"), dict) else {}
        request_meta = request_params.get("_meta") if isinstance(request_params.get("_meta"), dict) else {}
        stateless_request = (
            method == "server/discover"
            or protocol_version == STATELESS_PROTOCOL_VERSION
            or request_meta.get("io.modelcontextprotocol/protocolVersion") == STATELESS_PROTOCOL_VERSION
        )
        created_session = False
        leased_session_id: str | None = None
        if stateless_request:
            self._runtime = self.server.control_runtime  # type: ignore[attr-defined]
        elif method == "initialize":
            if session_id:
                self.send_rpc_error(-32600, "initialize must not include Mcp-Session-Id", request_id=request.get("id"))
                return
            owner = self.session_owner() or f"ip:{self.client_address[0]}"
            if not self.server.rate_limiter.allow(f"mcp-initialize:{owner}", limit=30, window_seconds=60):
                self.send_rpc_error(
                    -32000,
                    "Too many MCP initialize requests",
                    status=429,
                    request_id=request.get("id"),
                    extra_headers={"Retry-After": "10"},
                )
                return
            try:
                binding = self.server.sessions.create(self.session_owner(), acquire=True)  # type: ignore[attr-defined]
                self._runtime = binding.runtime
                self._mcp_session_id = binding.session_id
                leased_session_id = binding.session_id
            except SessionCapacityError as exc:
                self.send_rpc_error(
                    -32000,
                    str(exc),
                    status=429,
                    request_id=request.get("id"),
                    extra_headers={"Retry-After": str(exc.retry_after)},
                )
                return
            except RuntimeError as exc:
                self.send_rpc_error(-32000, str(exc), status=503, request_id=request.get("id"))
                return
            self._send_session_header = True
            created_session = True
        elif session_id:
            binding = self.server.sessions.acquire(session_id)  # type: ignore[attr-defined]
            if binding is None:
                self.send_rpc_error(-32001, "Unknown MCP session", status=404, request_id=response_id(request))
                return
            self._runtime = binding.runtime
            self._mcp_session_id = binding.session_id
            leased_session_id = binding.session_id
            self._send_session_header = True
            if protocol_version and protocol_version != self.runtime.protocol_version:
                self.server.sessions.release(binding.session_id)  # type: ignore[attr-defined]
                leased_session_id = None
                self.send_rpc_error(
                    -32600,
                    "MCP-Protocol-Version does not match the initialized session",
                    request_id=request.get("id"),
                    data={"expected": self.runtime.protocol_version, "received": protocol_version},
                )
                return
        elif method == "ping":
            self._runtime = self.server.control_runtime  # type: ignore[attr-defined]
        else:
            self.send_rpc_error(-32002, "Server not initialized", request_id=request.get("id"))
            return
        try:
            response = self.handle_rpc(request)
            if created_session and response is not None and "error" in response:
                self.server.sessions.delete(self._mcp_session_id)  # type: ignore[attr-defined]
                self._send_session_header = False
                leased_session_id = None
            if response is None:
                self.send_response(202)
                if getattr(self, "_send_session_header", False):
                    self.send_header("Mcp-Session-Id", self._mcp_session_id)
                self.send_header("Content-Length", "0")
                self.send_cors_headers()
                self.end_headers()
                return
            self.send_json(response)
        finally:
            if leased_session_id is not None:
                self.server.sessions.release(leased_session_id)  # type: ignore[attr-defined]

    def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if request.get("method") == "server/discover":
                return {
                    "jsonrpc": "2.0",
                    "id": response_id(request),
                    "result": {
                        "resultType": "complete",
                        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": runtime_version()},
                        "instructions": self.runtime.project_context.server_instructions(),
                    },
                }
            return dispatch_rpc(self.runtime, request)
        except Exception as exc:  # noqa: BLE001
            error_message, error_leaves = summarize_exception(exc)
            return jsonrpc_error(
                response_id(request),
                -32603,
                error_message,
                {"exception_type": exc.__class__.__name__, "leaf_errors": error_leaves},
            )

    def is_authorized(self) -> bool:
        if not self.runtime.auth_enabled():
            return True
        header = self.headers.get("Authorization", "").strip()
        if self.runtime.auth_token is not None:
            if secrets.compare_digest(header, f"Bearer {self.runtime.auth_token}"):
                return True
        if self.runtime.oauth_config is not None and header.startswith("Bearer "):
            token = header[len("Bearer "):]
            base = self.oauth_base_url()
            for audience in self.oauth_resource_urls():
                if validate_access_token(
                    token,
                    self.runtime.oauth_config,
                    base,
                    audience=audience,
                ):
                    return True
        return False

    def session_owner(self) -> str | None:
        """Return a non-sensitive stable owner key for session quotas."""

        header = self.headers.get("Authorization", "").strip()
        if not header:
            return None
        if header.startswith("Bearer ") and self.runtime.oauth_config is not None:
            token = header[len("Bearer ") :]
            base = self.oauth_base_url()
            for audience in self.oauth_resource_urls():
                client_id = access_token_client_id(
                    token,
                    self.runtime.oauth_config,
                    base,
                    audience=audience,
                )
                if client_id:
                    return "oauth-client:" + hashlib.sha256(client_id.encode("utf-8")).hexdigest()
        return hashlib.sha256(header.encode("utf-8")).hexdigest()

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and is_allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, DELETE, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Accept, Authorization, Content-Type, MCP-Protocol-Version, Mcp-Session-Id",
            )

    def send_json(
        self,
        payload: Any,
        *,
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        body = json_response_payload(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_send_session_header", False) and getattr(self, "_mcp_session_id", None):
            self.send_header("Mcp-Session-Id", self._mcp_session_id)
        self.send_cors_headers()
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            _write_http_body_safely(self, body)


class MCPHealthHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"CodingToolsMCPHealth/{__version__}"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] in {"/", "/index.html"}:
            body = (
                "<!doctype html><meta charset='utf-8'><title>Coding Tools MCP</title>"
                "<style>body{font:15px system-ui;margin:2rem;max-width:60rem}"
                "pre{background:#f4f4f4;padding:1rem;overflow:auto}</style>"
                "<h1>Coding Tools MCP</h1><pre id='status'>loading…</pre>"
                "<script>fetch('/healthz').then(r=>r.json()).then(v=>status.textContent="
                "JSON.stringify(v,null,2)).catch(e=>status.textContent=String(e))</script>"
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif self.path.split("?", 1)[0] == "/healthz":
            body = json.dumps(self.server.mcp_server.health_payload(), separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        else:
            body = b'{"error":"not_found"}'
            content_type = "application/json"
        status = 200 if self.path.split("?", 1)[0] in {"/", "/index.html", "/healthz"} else 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _write_http_body_safely(self, body)

    def do_POST(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if request_path == "/notify-tools-changed":
            generation = self.server.mcp_server.notify_tools_changed()
            body = json.dumps(
                {"status": "ok", "tool_list_generation": generation}, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(200)
        elif request_path != "/prune":
            body = b'{"error":"not_found"}'
            self.send_response(404)
        else:
            self.server.mcp_server.sessions.prune()
            body = json.dumps(self.server.mcp_server.health_payload(), separators=(",", ":")).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _write_http_body_safely(self, body)


class RuntimeHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[MCPHandler],
        control_runtime: Any,
        runtime_factory: Any,
        *,
        tool_list_state: dict[str, Any] | None = None,
        enable_health: bool = True,
    ) -> None:
        super().__init__(address, handler)
        self.control_runtime = control_runtime
        self.sessions = HTTPSessionManager(runtime_factory)
        self.control_runtime.execution_registry.http_session_stats_provider = self.sessions.stats
        self.rate_limiter = SlidingWindowRateLimiter()
        self.started_at = time.time()
        self._tool_list_state = tool_list_state or {"condition": threading.Condition(), "generation": 0}
        self.health_server: http.server.ThreadingHTTPServer | None = None
        self.health_thread: threading.Thread | None = None
        if not enable_health:
            return
        raw_health_port = (os.environ.get(f"{ENV_PREFIX}_HEALTH_PORT") or "8766").strip()
        try:
            health_port = int(raw_health_port)
        except ValueError:
            health_port = 8766
        if health_port > 0:
            try:
                self.health_server = http.server.ThreadingHTTPServer(("127.0.0.1", health_port), MCPHealthHandler)
                self.health_server.mcp_server = self  # type: ignore[attr-defined]
                self.health_server.daemon_threads = True
                self.health_thread = threading.Thread(
                    target=self.health_server.serve_forever,
                    name="coding-tools-mcp-health",
                    daemon=True,
                )
                self.health_thread.start()
            except OSError as exc:
                print(f"WARNING: local health endpoint disabled: {exc}", file=sys.stderr)
                self.health_server = None

    @property
    def tool_list_generation(self) -> int:
        return int(self._tool_list_state["generation"])

    def notify_tools_changed(self) -> int:
        condition = self._tool_list_state["condition"]
        with condition:
            self._tool_list_state["generation"] = int(self._tool_list_state["generation"]) + 1
            condition.notify_all()
            return int(self._tool_list_state["generation"])

    def wait_for_tool_list_change(self, generation: int, *, timeout: float) -> int:
        condition = self._tool_list_state["condition"]
        with condition:
            if int(self._tool_list_state["generation"]) == generation:
                condition.wait(timeout=max(0.1, timeout))
            return int(self._tool_list_state["generation"])

    def health_payload(self) -> dict[str, Any]:
        with self.control_runtime.sessions_lock:
            running_exec = len(self.control_runtime.sessions)
            retained_output = len(self.control_runtime.output_sessions)
        return {
            "status": "ok",
            "version": __version__,
            "display_version": runtime_version(),
            "build_identity": runtime_build_identity(),
            "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
            "permission_mode": self.control_runtime.permission_mode,
            "dangerously_skip_all_permissions": self.control_runtime.dangerously_skip_all_permissions,
            "permission_approval_transport": "local_windows_broker",
            "workspace": str(self.control_runtime.workspace.root),
            "workspace_allowlist": [
                {"name": entry.name, "path": str(entry.path)}
                for entry in workspace_catalog_from_env()
            ],
            "mcp": {"endpoint": MCP_ENDPOINT_PATH, "bind": self.server_address, "port": self.server_address[1]},
            "http_sessions": self.sessions.stats(),
            "execution": {
                "running": running_exec,
                "retained_output": retained_output,
                "max_running": MAX_ACTIVE_EXEC_SESSIONS,
            },
            "rate_limit": {"rejected": self.rate_limiter.rejected},
            "oauth": {
                "enabled": self.control_runtime.oauth_enabled(),
                "state_path": (
                    str(self.control_runtime.oauth_config.state_store.path)
                    if self.control_runtime.oauth_config is not None
                    and self.control_runtime.oauth_config.state_store is not None
                    else None
                ),
                "access_token_ttl": (
                    self.control_runtime.oauth_config.token_ttl
                    if self.control_runtime.oauth_config is not None
                    else None
                ),
                "refresh_token_ttl": (
                    self.control_runtime.oauth_config.refresh_token_ttl
                    if self.control_runtime.oauth_config is not None
                    else None
                ),
            },
        }

    def server_close(self) -> None:
        if self.health_server is not None:
            self.health_server.shutdown()
            self.health_server.server_close()
            self.health_server = None
        self.sessions.close()
        self.control_runtime.close()
        super().server_close()


__all__ = ["MCPHandler", "MCPHealthHandler", "RuntimeHTTPServer", "server_card_payload"]
