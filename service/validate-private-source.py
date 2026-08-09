from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-parent", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    package_parent = Path(args.package_parent).resolve()
    workspace = Path(args.workspace).resolve()
    sys.path.insert(0, str(package_parent))

    from coding_tools_mcp import server
    from coding_tools_mcp import elevated_actions
    from coding_tools_mcp.project_context import load_project_context
    from coding_tools_mcp.transport_http import (
        HTTP_IN_FLIGHT_TTL_SECONDS,
        HTTP_SESSION_TTL_SECONDS,
        HTTPSessionManager,
    )

    # Importing server already executes the public-catalog invariants. Keep a
    # few explicit assertions here so failures explain what contract broke.
    if len(server.PUBLIC_TOOL_NAMES) > 20:
        raise RuntimeError("public tool catalog exceeds 20-tool connector budget")
    required_public_tools = {"get_default_cwd", "set_default_cwd", "request_permissions"}
    missing_public_tools = required_public_tools.difference(server.PUBLIC_TOOL_NAMES)
    if missing_public_tools:
        raise RuntimeError(
            "required V9 public tools are missing: " + ", ".join(sorted(missing_public_tools))
        )
    stale_public_tools = {"list_sessions", "request_elevated_action"}.intersection(server.PUBLIC_TOOL_NAMES)
    if stale_public_tools:
        raise RuntimeError(
            "stale pre-V9 public tools remain exposed: " + ", ".join(sorted(stale_public_tools))
        )

    context = load_project_context(workspace)
    scan_warnings = [warning for warning in context.warnings if "scan stopped" in warning.casefold()]
    if scan_warnings:
        raise RuntimeError("project-context discovery hit a scan limit: " + "; ".join(scan_warnings))

    # A directory-only shell change is a common model/user expectation. Verify
    # it becomes the shared owner cwd instead of disappearing with a one-shot
    # child shell, and verify another owner cannot inherit it.
    with tempfile.TemporaryDirectory(prefix="coding-tools-cwd-check-") as temporary:
        cwd_workspace = Path(temporary)
        project = cwd_workspace / "project"
        project.mkdir()
        primary = server.Runtime(cwd_workspace, enable_view_image=False)
        try:
            primary.state_owner = "selfcheck-owner"
            changed = primary.exec_command({"cmd": "cd project"})
            if not changed.get("cwd_persisted") or changed.get("default_cwd") != "project":
                raise RuntimeError("directory-only exec did not persist the new default cwd")
            parent = primary.exec_command({"cmd": "cd .."})
            if parent.get("default_cwd") != ".":
                raise RuntimeError("a safe parent-directory change did not return to the workspace root")
            windows_style = primary.exec_command({"cmd": 'cd /d "project"'})
            if windows_style.get("default_cwd") != "project":
                raise RuntimeError("CMD-style cd /d did not persist the new default cwd")
            reconnect = server.Runtime(
                cwd_workspace,
                enable_view_image=False,
                project_context=primary.project_context,
                execution_registry=primary.execution_registry,
            )
            isolated = server.Runtime(
                cwd_workspace,
                enable_view_image=False,
                project_context=primary.project_context,
                execution_registry=primary.execution_registry,
            )
            try:
                reconnect.state_owner = "selfcheck-owner"
                isolated.state_owner = "different-owner"
                if reconnect.default_cwd_display() != "project":
                    raise RuntimeError("default cwd did not survive an owner reconnect")
                if isolated.default_cwd_display() != ".":
                    raise RuntimeError("default cwd leaked across owners")

                original_approval = server.request_permission_approval
                server.request_permission_approval = lambda **_kwargs: {"ok": True, "granted": True}
                try:
                    blocked_arguments = {"cmd": "curl https://example.invalid"}
                    once = primary.request_permissions(
                        {
                            "tool_name": "exec_command",
                            "permission": "network",
                            "reason": "permission self-check",
                            "arguments": blocked_arguments,
                            "scope": "once",
                            "ttl_seconds": 60,
                        }
                    )
                    if once.get("status") != "granted":
                        raise RuntimeError("interactive approval did not create a permission grant")
                    primary.request_context.tool_name = "exec_command"
                    primary.request_context.arguments = blocked_arguments
                    primary.request_context.claimed_permission_grants = set()
                    primary._check_command_policy(blocked_arguments["cmd"], blocked_arguments)
                    primary._finish_permission_grants()
                    try:
                        primary._check_command_policy(blocked_arguments["cmd"], blocked_arguments)
                    except server.ToolFailure as exc:
                        if exc.code != "PERMISSION_REQUIRED":
                            raise
                    else:
                        raise RuntimeError("one-shot permission grant was not consumed")

                    session = primary.request_permissions(
                        {
                            "tool_name": "exec_command",
                            "permission": "network",
                            "reason": "session permission self-check",
                            "arguments": blocked_arguments,
                            "scope": "session",
                            "ttl_seconds": 60,
                        }
                    )
                    if session.get("status") != "granted":
                        raise RuntimeError("session approval did not create a permission grant")
                    changed_arguments = {"cmd": "curl https://different.invalid"}
                    reconnect.request_context.tool_name = "exec_command"
                    reconnect.request_context.arguments = changed_arguments
                    reconnect.request_context.claimed_permission_grants = set()
                    reconnect._check_command_policy(changed_arguments["cmd"], changed_arguments)
                finally:
                    server.request_permission_approval = original_approval

                dangerous = server.Runtime(
                    cwd_workspace,
                    enable_view_image=False,
                    permission_mode="dangerous",
                    project_context=primary.project_context,
                    execution_registry=primary.execution_registry,
                )
                try:
                    dangerous.state_owner = "dangerous-selfcheck-owner"
                    dangerous.request_context.tool_name = "exec_command"
                    dangerous.request_context.arguments = {"cmd": "curl https://yolo.invalid"}
                    dangerous.request_context.claimed_permission_grants = set()
                    dangerous._check_command_policy(
                        "curl https://yolo.invalid",
                        dangerous.request_context.arguments,
                    )
                    if not dangerous.dangerously_skip_all_permissions:
                        raise RuntimeError("dangerous mode did not enable the YOLO permission policy")
                finally:
                    dangerous.close()
            finally:
                isolated.close()
                reconnect.close()
        finally:
            primary.close()

    if os.name == "nt":
        with tempfile.TemporaryDirectory(prefix="coding-tools-broker-check-") as temporary:
            queue = Path(temporary)
            (queue / "broker.pid").write_text(str(os.getpid()), encoding="ascii")
            alive, reported_pid = elevated_actions._broker_is_alive(queue)
            if not alive or reported_pid != os.getpid():
                raise RuntimeError("Windows broker process liveness probe rejected a live PID")

        runtime = server.Runtime(workspace, enable_view_image=False, project_context=context)
        try:
            command_env = {key.upper(): value for key, value in runtime._command_env({}).items()}
            required_windows_env = {
                "PROGRAMFILES",
                "PROGRAMFILES(X86)",
                "PROGRAMW6432",
                "USERPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "HOMEDRIVE",
                "HOMEPATH",
                "DOTNET_CLI_HOME",
                "NUGET_PACKAGES",
            }
            missing_windows_env = sorted(required_windows_env.difference(command_env))
            if missing_windows_env:
                raise RuntimeError(
                    "Windows command environment is missing developer-tool profile variables: "
                    + ", ".join(missing_windows_env)
                )
            runtime_prefix = str(runtime.runtime_dir).rstrip("\\/").casefold() + "\\"
            for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "DOTNET_CLI_HOME", "NUGET_PACKAGES"):
                value = str(command_env[name]).casefold()
                if not value.startswith(runtime_prefix):
                    raise RuntimeError(f"{name} must remain isolated inside the MCP runtime directory")
        finally:
            runtime.close()

    class FakeRuntime:
        serial = 0

        def __init__(self) -> None:
            type(self).serial += 1
            self.http_session_id = f"selfcheck-{self.serial}"
            self.state_owner = None

        def close(self) -> None:
            return None

    sessions = HTTPSessionManager(
        FakeRuntime,
        max_sessions=2,
        session_ttl_seconds=30,
        in_flight_ttl_seconds=30,
        max_sessions_per_owner=1,
    )
    sessions.create("owner")
    sessions.create("owner")
    stats = sessions.stats()
    stuck = sessions.create("stuck", acquire=True)
    stuck_record = sessions._sessions[stuck.session_id]
    stuck_record.last_seen -= 31
    stuck_record.in_flight_since = (stuck_record.in_flight_since or time.time()) - 31
    stale_stats = sessions.stats()
    sessions.close()
    if stats.get("capacity_evicted") != 1 or "expired" not in stats:
        raise RuntimeError("HTTP session diagnostics do not distinguish expiration from capacity eviction")
    if stale_stats.get("stale_in_flight_evicted") != 1 or stale_stats.get("in_flight") != 0:
        raise RuntimeError("stale HTTP in-flight lease watchdog did not evict a stuck lease")
    if HTTP_IN_FLIGHT_TTL_SECONDS != 90:
        raise RuntimeError("HTTP in-flight lease TTL must not exceed the 90-second request lifetime")
    if HTTP_SESSION_TTL_SECONDS != 300:
        raise RuntimeError("idle HTTP sessions must survive normal five-minute tool gaps")

    class DisconnectingWriter:
        def write(self, _body: bytes) -> None:
            raise ConnectionAbortedError("selfcheck disconnect")

    class DisconnectingHandler:
        def __init__(self) -> None:
            self.wfile = DisconnectingWriter()
            self.close_connection = False

    disconnect = DisconnectingHandler()
    if server._write_http_body_safely(disconnect, b"test") or not disconnect.close_connection:
        raise RuntimeError("client disconnects are not handled as normal response termination")

    print(
        "PRIVATE_MCP_SOURCE_CHECK_OK "
        f"tools={len(server.PUBLIC_TOOL_NAMES)} "
        f"context_files={len(context.nested_files)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
