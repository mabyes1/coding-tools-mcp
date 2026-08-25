from __future__ import annotations

import argparse
import sys
from pathlib import Path

from validation.architecture_checks import run_architecture_checks
from validation.catalog_checks import run_catalog_checks
from validation.command_policy_checks import run_command_policy_checks
from validation.cwd_git_permission_checks import run_cwd_git_permission_checks
from validation.desktop_handoff_checks import run_desktop_handoff_checks
from validation.execution_checks import run_execution_checks
from validation.http_lifecycle_checks import run_http_lifecycle_checks
from validation.http_transport_checks import run_http_transport_checks
from validation.image_checks import run_image_checks
from validation.patch_checks import run_patch_checks
from validation.process_control_checks import run_process_control_checks
from validation.runtime_lifecycle_checks import run_runtime_lifecycle_checks
from validation.session_registry_checks import run_session_registry_checks
from validation.session_inspection_checks import run_session_inspection_checks
from validation.session_output_checks import run_session_output_checks
from validation.windows_deployment_checks import run_windows_deployment_checks
from validation.windows_runtime_checks import run_windows_runtime_checks
from validation.workspace_filesystem_checks import run_workspace_filesystem_checks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-parent", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    package_parent = Path(args.package_parent).resolve()
    workspace = Path(args.workspace).resolve()
    sys.path.insert(0, str(package_parent))

    from coding_tools_mcp import server
    from coding_tools_mcp import activity as activity_module
    from coding_tools_mcp import elevated_actions
    from coding_tools_mcp import runtime as runtime_module
    from coding_tools_mcp.patching import find_subsequence_all
    from coding_tools_mcp.project_context import load_project_context
    from coding_tools_mcp.transport_http import (
        HTTP_IN_FLIGHT_TTL_SECONDS,
        HTTP_SESSION_TTL_SECONDS,
        HTTPSessionManager,
    )

    for architecture_warning in run_architecture_checks(package_parent, server):
        print(f"ARCH_WARNING: {architecture_warning}", file=sys.stderr)

    action_contract = run_catalog_checks(server, elevated_actions, activity_module)

    run_windows_deployment_checks(package_parent, action_contract)

    run_desktop_handoff_checks(server, runtime_module, workspace)

    run_runtime_lifecycle_checks(server)

    run_session_registry_checks(server)

    run_session_output_checks(server)

    run_process_control_checks(server)

    run_session_inspection_checks(server)

    run_http_lifecycle_checks(server)

    run_command_policy_checks(server)

    run_execution_checks(server, runtime_module)

    context = load_project_context(workspace)
    scan_warnings = [warning for warning in context.warnings if "scan stopped" in warning.casefold()]
    if scan_warnings:
        raise RuntimeError("project-context discovery hit a scan limit: " + "; ".join(scan_warnings))

    run_image_checks(server)

    run_workspace_filesystem_checks(server)

    run_patch_checks(server, find_subsequence_all)

    run_cwd_git_permission_checks(server, runtime_module)

    run_windows_runtime_checks(server, elevated_actions, workspace, context)

    run_http_transport_checks(
        server,
        HTTPSessionManager,
        HTTP_IN_FLIGHT_TTL_SECONDS,
        HTTP_SESSION_TTL_SECONDS,
    )

    print(
        "PRIVATE_MCP_SOURCE_CHECK_OK "
        f"tools={len(server.PUBLIC_TOOL_NAMES)} "
        f"context_files={len(context.nested_files)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
