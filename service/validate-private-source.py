from __future__ import annotations

import argparse
import os
import sys
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
    from coding_tools_mcp.project_context import load_project_context
    from coding_tools_mcp.transport_http import HTTP_IN_FLIGHT_TTL_SECONDS, HTTPSessionManager

    # Importing server already executes the public-catalog invariants. Keep a
    # few explicit assertions here so failures explain what contract broke.
    if len(server.PUBLIC_TOOL_NAMES) > 20:
        raise RuntimeError("public tool catalog exceeds 20-tool connector budget")
    required_public_tools = {"get_default_cwd", "request_permissions"}
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

    if os.name == "nt":
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

    print(
        "PRIVATE_MCP_SOURCE_CHECK_OK "
        f"tools={len(server.PUBLIC_TOOL_NAMES)} "
        f"context_files={len(context.nested_files)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
