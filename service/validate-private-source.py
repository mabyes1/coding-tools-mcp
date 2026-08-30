from __future__ import annotations

import argparse
import json
import sys
import time
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
    parser.add_argument(
        "--skip-desktop-surfaces",
        action="store_true",
        help="Do not invoke Computer Use/Browser Use or desktop HUMAN HELP smoke paths.",
    )
    parser.add_argument(
        "--report-json",
        default=None,
        help="Write one JSON record per source check for the regression runner.",
    )
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

    outcomes: list[dict[str, object]] = []
    failures: list[str] = []

    def emit(label: str, status: str, started: float, detail: str = "") -> None:
        name = f"source.{label}"
        item = {
            "name": name,
            "status": status,
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "detail": detail,
        }
        outcomes.append(item)
        suffix = f" - {detail}" if detail else ""
        print(f"[{status}] {name}{suffix}", flush=True)
        if status in {"FAIL", "ERROR"}:
            failures.append(f"{name}: {detail}")

    def run_one(label: str, callback) -> object | None:
        started = time.perf_counter()
        try:
            value = callback()
        except Exception as exc:  # noqa: BLE001 - keep checking independent contracts
            emit(label, "FAIL", started, f"{exc.__class__.__name__}: {exc}")
            return None
        emit(label, "PASS", started)
        return value

    architecture = run_one("architecture", lambda: run_architecture_checks(package_parent, server))
    if isinstance(architecture, list):
        for architecture_warning in architecture:
            print(f"ARCH_WARNING: {architecture_warning}", file=sys.stderr)

    action_contract: dict[str, tuple[str, ...]] | None = None

    def catalog() -> object:
        nonlocal action_contract
        action_contract = run_catalog_checks(server, elevated_actions, activity_module)
        return action_contract

    run_one("catalog", catalog)

    if action_contract is None:
        emit(
            "windows_deployment",
            "ERROR",
            time.perf_counter(),
            "skipped because source.catalog did not produce the action contract",
        )
    else:
        run_one(
            "windows_deployment",
            lambda: run_windows_deployment_checks(
                package_parent,
                action_contract,
                include_desktop_surfaces=not args.skip_desktop_surfaces,
            ),
        )

    if args.skip_desktop_surfaces:
        emit(
            "desktop_handoff",
            "PAUSED",
            time.perf_counter(),
            "Computer Use/Browser Use and desktop HUMAN HELP smoke paths were intentionally not run",
        )
    else:
        run_one("desktop_handoff", lambda: run_desktop_handoff_checks(server, runtime_module, workspace))

    run_one("runtime_lifecycle", lambda: run_runtime_lifecycle_checks(server))
    run_one("session_registry", lambda: run_session_registry_checks(server))
    run_one("session_output", lambda: run_session_output_checks(server))
    run_one("process_control", lambda: run_process_control_checks(server))
    run_one("session_inspection", lambda: run_session_inspection_checks(server))
    run_one("http_lifecycle", lambda: run_http_lifecycle_checks(server))
    run_one("command_policy", lambda: run_command_policy_checks(server))
    run_one("execution", lambda: run_execution_checks(server, runtime_module))

    def project_context_check() -> object:
        context = load_project_context(workspace)
        scan_warnings = [warning for warning in context.warnings if "scan stopped" in warning.casefold()]
        if scan_warnings:
            raise RuntimeError("project-context discovery hit a scan limit: " + "; ".join(scan_warnings))
        return context

    context = run_one("project_context", project_context_check)
    run_one("image", lambda: run_image_checks(server))
    run_one("workspace_filesystem", lambda: run_workspace_filesystem_checks(server))
    run_one("patch", lambda: run_patch_checks(server, find_subsequence_all))
    run_one("cwd_git_permission", lambda: run_cwd_git_permission_checks(server, runtime_module))
    if context is None:
        emit(
            "windows_runtime",
            "ERROR",
            time.perf_counter(),
            "skipped because source.project_context did not produce a context",
        )
    else:
        run_one("windows_runtime", lambda: run_windows_runtime_checks(server, elevated_actions, workspace, context))
    run_one(
        "http_transport",
        lambda: run_http_transport_checks(
            server,
            HTTPSessionManager,
            HTTP_IN_FLIGHT_TTL_SECONDS,
            HTTP_SESSION_TTL_SECONDS,
        ),
    )

    report = {"ok": not failures, "checks": outcomes}
    if args.report_json:
        report_path = Path(args.report_json).resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if failures:
        raise RuntimeError("source checks failed: " + "; ".join(failures))

    context_files = len(getattr(context, "nested_files", [])) if context is not None else 0
    print(
        "PRIVATE_MCP_SOURCE_CHECK_OK "
        f"tools={len(server.PUBLIC_TOOL_NAMES)} "
        f"context_files={context_files}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
