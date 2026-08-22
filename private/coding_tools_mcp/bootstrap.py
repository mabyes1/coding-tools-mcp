from __future__ import annotations

import argparse
import os
import secrets
import signal
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

from .envutils import ENV_PREFIX, truthy_env
from .http_server import MCPHandler, RuntimeHTTPServer
from .oauth import OAUTH_TOKEN_TTL_SECONDS, OAuthClientRegistry, OAuthConfig, OAuthStateStore
from .project_context import ProjectContext
from .runtime_config import (
    PERMISSION_MODE_CHOICES,
    SHELL_ENV_INHERIT_CHOICES,
    RuntimePolicy,
    env_int,
    runtime_policy_from_args,
)
from .runtime_meta import SERVER_NAME
from .session_store import ExecutionRegistry
from .transport_http import http_base_for_bind_host, is_loopback_bind_host
from .transport_stdio import serve_stdio
from .workspace import validate_workspace_selection


AUTH_MODE_CHOICES = ("bearer", "noauth", "oauth")


def build_runtime(
    args: argparse.Namespace,
    runtime_policy: RuntimePolicy,
    *,
    auth_token: str | None = None,
    oauth_config: OAuthConfig | None = None,
    emit_warning: bool = True,
    project_context: ProjectContext | None = None,
    transport: str = "stdio",
    execution_registry: ExecutionRegistry | None = None,
) -> Any:
    from .server import Runtime

    workspace = Path(args.workspace or os.environ.get(f"{ENV_PREFIX}_WORKSPACE") or os.getcwd())
    workspace_allowlist = validate_workspace_selection(workspace)
    runtime = Runtime(
        workspace,
        enable_view_image=args.enable_view_image,
        permission_mode=runtime_policy.permission_mode,
        shell_env_policy=runtime_policy.shell_env_policy,
        allow_network=runtime_policy.allow_network,
        auth_token=auth_token,
        oauth_config=oauth_config,
        project_context=project_context,
        fake_readonly_annotations=runtime_policy.fake_readonly_annotations,
        transport=transport,
        execution_registry=execution_registry,
    )
    runtime.workspace_allowlist = workspace_allowlist  # type: ignore[attr-defined]
    if emit_warning and runtime.capabilities.skip_all_permissions:
        print(
            "WARNING: permission_mode=dangerous disables MCP safety gates. Use only inside an isolated container or VM.",
            file=sys.stderr,
        )
    if emit_warning and runtime.fake_readonly_annotations:
        print(
            "WARNING: tools/list reports every tool as read-only and non-destructive. "
            "apply_patch and exec_command still mutate the workspace and still run commands. "
            "server_info and the server card keep reporting the real annotations.",
            file=sys.stderr,
        )
    return runtime


def run_http(args: argparse.Namespace) -> int:
    auth_mode = (os.environ.get(f"{ENV_PREFIX}_AUTH_MODE") or "").strip().lower()
    if auth_mode and auth_mode not in AUTH_MODE_CHOICES:
        supported = ", ".join(AUTH_MODE_CHOICES)
        print(f"ERROR: {ENV_PREFIX}_AUTH_MODE must be one of: {supported}.", file=sys.stderr)
        return 2
    auth_token = args.auth_token or os.environ.get(f"{ENV_PREFIX}_AUTH_TOKEN") or None
    try:
        runtime_policy = runtime_policy_from_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    oauth_config: OAuthConfig | None = None
    oauth_mode = (
        getattr(args, "oauth_mode", False)
        or truthy_env(os.environ.get(f"{ENV_PREFIX}_OAUTH_MODE"))
        or auth_mode == "oauth"
    )
    if oauth_mode:
        client_id = os.environ.get(f"{ENV_PREFIX}_OAUTH_CLIENT_ID") or None
        client_secret = os.environ.get(f"{ENV_PREFIX}_OAUTH_CLIENT_SECRET") or None
        env_password = os.environ.get(f"{ENV_PREFIX}_OAUTH_PASSWORD")
        password = env_password or secrets.token_urlsafe(32)
        server_url = (os.environ.get(f"{ENV_PREFIX}_SERVER_URL") or "").rstrip("/") or None
        if not env_password:
            print(f"OAuth authorize password: {password}", file=sys.stderr)
        raw_secret = os.environ.get(f"{ENV_PREFIX}_OAUTH_TOKEN_SECRET") or ""
        if raw_secret:
            try:
                token_secret = bytes.fromhex(raw_secret)
            except ValueError:
                print(
                    f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_SECRET must be hex-encoded bytes.",
                    file=sys.stderr,
                )
                return 2
            if len(token_secret) < 32:
                print(
                    f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_SECRET must contain at least 32 bytes.",
                    file=sys.stderr,
                )
                return 2
        else:
            token_secret = secrets.token_bytes(32)
        try:
            token_ttl = int(os.environ.get(f"{ENV_PREFIX}_OAUTH_TOKEN_TTL") or OAUTH_TOKEN_TTL_SECONDS)
        except ValueError:
            print(f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_TTL must be an integer.", file=sys.stderr)
            return 2
        if not 60 <= token_ttl <= 315_360_000:
            print(f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_TTL must be between 60 and 315360000 seconds.", file=sys.stderr)
            return 2
        try:
            refresh_token_ttl = int(
                os.environ.get(f"{ENV_PREFIX}_OAUTH_REFRESH_TOKEN_TTL") or 365 * 24 * 60 * 60
            )
        except ValueError:
            print(f"ERROR: {ENV_PREFIX}_OAUTH_REFRESH_TOKEN_TTL must be an integer.", file=sys.stderr)
            return 2
        if not 60 <= refresh_token_ttl <= 10 * 365 * 24 * 60 * 60:
            print(
                f"ERROR: {ENV_PREFIX}_OAUTH_REFRESH_TOKEN_TTL must be between 60 and {10 * 365 * 24 * 60 * 60} seconds.",
                file=sys.stderr,
            )
            return 2
        state_store: OAuthStateStore | None = None
        state_path = (os.environ.get(f"{ENV_PREFIX}_OAUTH_STATE_PATH") or "").strip()
        if state_path:
            try:
                state_store = OAuthStateStore(state_path)
            except (OSError, sqlite3.Error) as exc:
                print(f"ERROR: could not open OAuth state store: {exc}", file=sys.stderr)
                return 2
        oauth_config = OAuthConfig(
            password=password,
            server_url=server_url,
            token_secret=token_secret,
            token_ttl=token_ttl,
            refresh_token_ttl=refresh_token_ttl,
            state_store=state_store,
            registry=OAuthClientRegistry(state_store),
        )
        if client_id:
            raw_redirects = os.environ.get(f"{ENV_PREFIX}_OAUTH_REDIRECT_URIS") or "http://127.0.0.1/callback"
            redirect_uris = tuple(item.strip() for item in raw_redirects.split(",") if item.strip())
            try:
                oauth_config.registry.add_preregistered(
                    client_id,
                    redirect_uris,
                    client_secret=client_secret,
                )
            except ValueError as exc:
                print(f"ERROR: invalid OAuth redirect URI configuration: {exc}", file=sys.stderr)
                return 2
        if auth_token:
            print(
                "Auth: dual credentials enabled — both static bearer token and OAuth 2.1 access tokens will be accepted.",
                file=sys.stderr,
            )

    if (
        not auth_token
        and not oauth_config
        and not is_loopback_bind_host(str(args.host))
        and auth_mode != "noauth"
        and truthy_env(os.environ.get(f"{ENV_PREFIX}_GENERATE_AUTH_TOKEN"))
    ):
        auth_token = secrets.token_urlsafe(32)
        print(f"Generated {ENV_PREFIX}_AUTH_TOKEN for non-loopback binding.", file=sys.stderr)
        print(f"Bearer token: {auth_token}", file=sys.stderr)

    if not auth_token and not oauth_config and not is_loopback_bind_host(str(args.host)):
        print(
            "ERROR: non-loopback HTTP binding requires --auth-token, CODING_TOOLS_MCP_AUTH_TOKEN, or --oauth-mode.",
            file=sys.stderr,
        )
        return 2

    if runtime_policy.fake_readonly_annotations and not auth_token and not oauth_config:
        print(
            "ERROR: --dangerously-fake-readonly-annotations over HTTP requires --auth-token, "
            f"{ENV_PREFIX}_AUTH_TOKEN, or --oauth-mode. "
            "Use stdio for an unauthenticated local sandbox.",
            file=sys.stderr,
        )
        return 2

    runtime = build_runtime(args, runtime_policy, auth_token=auth_token, oauth_config=oauth_config, transport="http")

    def runtime_factory() -> Any:
        return build_runtime(
            args,
            runtime_policy,
            auth_token=auth_token,
            oauth_config=oauth_config,
            emit_warning=False,
            project_context=runtime.project_context,
            transport="http",
            execution_registry=runtime.execution_registry,
        )

    tool_list_state: dict[str, Any] = {"condition": threading.Condition(), "generation": 0}
    server = RuntimeHTTPServer(
        (args.host, args.port),
        MCPHandler,
        runtime,
        runtime_factory,
        tool_list_state=tool_list_state,
    )

    tunnel_server: RuntimeHTTPServer | None = None
    tunnel_thread: threading.Thread | None = None
    raw_tunnel_port = (os.environ.get(f"{ENV_PREFIX}_TUNNEL_PORT") or "").strip()
    if raw_tunnel_port:
        try:
            tunnel_port = int(raw_tunnel_port)
        except ValueError:
            print(f"ERROR: {ENV_PREFIX}_TUNNEL_PORT must be an integer.", file=sys.stderr)
            server.server_close()
            if oauth_config is not None:
                oauth_config.close()
            return 2
        if not 1 <= tunnel_port <= 65535 or tunnel_port == int(args.port):
            print(
                f"ERROR: {ENV_PREFIX}_TUNNEL_PORT must be a different TCP port between 1 and 65535.",
                file=sys.stderr,
            )
            server.server_close()
            if oauth_config is not None:
                oauth_config.close()
            return 2
        if runtime_policy.fake_readonly_annotations:
            print(
                f"ERROR: {ENV_PREFIX}_TUNNEL_PORT cannot be used with --dangerously-fake-readonly-annotations.",
                file=sys.stderr,
            )
            server.server_close()
            if oauth_config is not None:
                oauth_config.close()
            return 2

        tunnel_runtime = build_runtime(
            args,
            runtime_policy,
            auth_token=None,
            oauth_config=None,
            emit_warning=False,
            project_context=runtime.project_context,
            transport="http",
            execution_registry=runtime.execution_registry,
        )

        def tunnel_runtime_factory() -> Any:
            return build_runtime(
                args,
                runtime_policy,
                auth_token=None,
                oauth_config=None,
                emit_warning=False,
                project_context=runtime.project_context,
                transport="http",
                execution_registry=runtime.execution_registry,
            )

        try:
            tunnel_server = RuntimeHTTPServer(
                ("127.0.0.1", tunnel_port),
                MCPHandler,
                tunnel_runtime,
                tunnel_runtime_factory,
                tool_list_state=tool_list_state,
                enable_health=False,
            )
        except OSError as exc:
            tunnel_runtime.close()
            print(
                f"ERROR: could not bind local Secure MCP Tunnel listener on 127.0.0.1:{tunnel_port}: {exc}",
                file=sys.stderr,
            )
            server.server_close()
            if oauth_config is not None:
                oauth_config.close()
            return 2

        def combined_http_session_stats() -> dict[str, int | float]:
            primary = server.sessions.stats()
            tunnel = tunnel_server.sessions.stats() if tunnel_server is not None else {}
            additive = {
                "active", "in_flight", "creating", "max", "expired",
                "stale_in_flight_evicted", "capacity_evicted", "rejected",
            }
            maxima = {"oldest_age_seconds", "oldest_in_flight_seconds"}
            merged: dict[str, int | float] = dict(primary)
            for key in additive:
                merged[key] = int(primary.get(key, 0)) + int(tunnel.get(key, 0))
            for key in maxima:
                merged[key] = max(float(primary.get(key, 0.0)), float(tunnel.get(key, 0.0)))
            return merged

        runtime.execution_registry.http_session_stats_provider = combined_http_session_stats
        tunnel_thread = threading.Thread(
            target=tunnel_server.serve_forever,
            name="coding-tools-mcp-secure-tunnel",
            daemon=True,
        )
        tunnel_thread.start()
        print(
            f"{SERVER_NAME} Secure MCP Tunnel listener on http://127.0.0.1:{tunnel_port}/mcp (loopback only, auth handled by tunnel)",
            file=sys.stderr,
        )
    if oauth_config:
        url_label = oauth_config.server_url or "dynamic request URL"
        suffix = " + bearer" if runtime.auth_token else ""
        auth_label = f"oauth2{suffix} enabled (server_url={url_label})"
    elif runtime.auth_token:
        auth_label = "bearer auth enabled"
    else:
        auth_label = "no auth configured"
    base_url = http_base_for_bind_host(str(args.host), args.port)
    print(f"{SERVER_NAME} listening on {base_url}/mcp ({auth_label})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        if tunnel_server is not None:
            tunnel_server.shutdown()
            tunnel_server.server_close()
        server.server_close()
        if oauth_config is not None:
            oauth_config.close()
    return 0


def run_stdio(args: argparse.Namespace) -> int:
    try:
        runtime_policy = runtime_policy_from_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    runtime = build_runtime(args, runtime_policy)
    return serve_stdio(runtime)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve workspace-confined coding tools over MCP.")
    parser.add_argument("--workspace", help="workspace root; defaults to CODING_TOOLS_MCP_WORKSPACE or cwd")
    parser.add_argument(
        "--host",
        default=os.environ.get(f"{ENV_PREFIX}_HOST") or "127.0.0.1",
        help=f"bind host; defaults to {ENV_PREFIX}_HOST or 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_int(f"{ENV_PREFIX}_PORT", 8000),
        help=f"bind port; defaults to {ENV_PREFIX}_PORT or 8000",
    )
    parser.add_argument("--stdio", action="store_true", help="serve newline-delimited JSON-RPC over stdio")
    parser.add_argument(
        "--auth-token",
        default=None,
        help=f"require Authorization: Bearer <token> on /mcp; defaults to {ENV_PREFIX}_AUTH_TOKEN",
    )
    parser.add_argument(
        "--oauth-mode",
        action="store_true",
        default=False,
        help=(
            "enable OAuth 2.1 Authorization Code + PKCE; "
            f"{ENV_PREFIX}_SERVER_URL is optional; when unset OAuth metadata uses the request host; "
            "authorize password is generated when unset; RFC 7591 dynamic registration is enabled"
        ),
    )
    parser.add_argument(
        "--shell-env-inherit",
        choices=SHELL_ENV_INHERIT_CHOICES,
        default=None,
        help=(
            "baseline environment inheritance for exec_command subprocesses; "
            f"defaults to {ENV_PREFIX}_SHELL_ENV_INHERIT or core"
        ),
    )
    parser.add_argument(
        "--permission-mode",
        choices=PERMISSION_MODE_CHOICES,
        default=None,
        help=(
            "exec_command permission mode: safe denies network/shell-expansion/inline-script gates; "
            "trusted allows local development network, shell expansion, and inline scripts; "
            "dangerous disables permission gates"
        ),
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "compatibility alias: allow network-looking exec_command calls without changing other gates; "
            f"can also be enabled with {ENV_PREFIX}_ALLOW_NETWORK=1"
        ),
    )
    parser.add_argument(
        "--enable-view-image",
        action="store_true",
        default=os.environ.get("CODING_TOOLS_MCP_ENABLE_VIEW_IMAGE", "1") != "0",
        help="enable the P1 view_image tool",
    )
    parser.add_argument(
        "--dangerously-skip-all-permissions",
        action="store_true",
        help=(
            "compatibility alias for --permission-mode dangerous; workspace path boundaries for direct file tools still apply"
        ),
    )
    parser.add_argument(
        "--dangerously-fake-readonly-annotations",
        action="store_true",
        help=(
            "report every tool in tools/list as read-only and non-destructive for clients that gate on "
            "annotations; mutation and execution still happen; requires --permission-mode dangerous, and "
            "requires auth over HTTP; server_info and the server card keep reporting the real annotations; "
            f"can also be enabled with {ENV_PREFIX}_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS=1"
        ),
    )
    return parser


def install_sigterm_handler() -> None:
    """Exit cleanly on SIGTERM (128 + 15), matching the KeyboardInterrupt path."""
    if threading.current_thread() is not threading.main_thread():
        return

    def _terminate(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    install_sigterm_handler()
    return run_stdio(args) if args.stdio else run_http(args)


__all__ = [
    "AUTH_MODE_CHOICES",
    "build_parser",
    "build_runtime",
    "install_sigterm_handler",
    "main",
    "run_http",
    "run_stdio",
]
