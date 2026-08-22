from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
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
    from coding_tools_mcp.patching import find_subsequence_all
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
    required_public_tools = {
        "get_default_cwd",
        "set_default_cwd",
        "request_permissions",
        "human_help_me",
        "computer_use",
        "browser_use",
    }
    missing_public_tools = required_public_tools.difference(server.PUBLIC_TOOL_NAMES)
    if missing_public_tools:
        raise RuntimeError(
            "required V10 public tools are missing: " + ", ".join(sorted(missing_public_tools))
        )
    stale_public_tools = {"list_sessions", "request_elevated_action", "git_blame"}.intersection(server.PUBLIC_TOOL_NAMES)
    if stale_public_tools:
        raise RuntimeError(
            "stale pre-V9 public tools remain exposed: " + ", ".join(sorted(stale_public_tools))
        )
    public_contract = [server.tool_definition(name) for name in server.PUBLIC_TOOL_NAMES]
    public_contract_bytes = json.dumps(
        public_contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    public_contract_sha256 = hashlib.sha256(public_contract_bytes).hexdigest()
    expected_public_contract_sha256 = "10a6219c4dd9a739f3ad6d05572f449d0800f8ad9bce16184851d10413b65392"
    if public_contract_sha256 != expected_public_contract_sha256:
        raise RuntimeError(
            "public tool definition/schema contract changed; "
            f"expected sha256={expected_public_contract_sha256}, actual={public_contract_sha256}. "
            "If this is an intentional product/API change, review the complete tools/list diff before updating this baseline."
        )

    exec_schema = server.input_schemas()["exec_command"]
    execution_context = exec_schema.get("properties", {}).get("execution_context", {})
    if execution_context.get("enum") != ["service", "active_user"]:
        raise RuntimeError("exec_command execution_context schema drifted from service/active_user")
    permission_schema = server.input_schemas()["request_permissions"]["properties"]["permission"]
    if "interactive_session" not in permission_schema.get("enum", []):
        raise RuntimeError("interactive_session permission is missing from request_permissions schema")
    human_schema = server.input_schemas()["human_help_me"]
    if set(human_schema.get("required", [])) != {"reason", "request"}:
        raise RuntimeError("human_help_me must require exactly reason and request")
    human_reasons = human_schema.get("properties", {}).get("reason", {}).get("enum", [])
    if "faster_by_human" not in human_reasons or "permission_blocked" not in human_reasons:
        raise RuntimeError("human_help_me reason schema is missing core escalation reasons")

    computer_actions = server.input_schemas()["computer_use"]["properties"]["action"].get("enum", [])
    if "inspect" not in computer_actions or "click" not in computer_actions or "type_text" not in computer_actions:
        raise RuntimeError("computer_use schema is missing core UI actions")
    browser_actions = server.input_schemas()["browser_use"]["properties"]["action"].get("enum", [])
    if "navigate" not in browser_actions or "inspect" not in browser_actions:
        raise RuntimeError("browser_use schema is missing core browser actions")
    action_contract = server.computer_use_action_contract()
    if tuple(computer_actions) != action_contract["computer_use"]:
        raise RuntimeError("computer_use schema drifted from computer-use-actions.json")
    if tuple(browser_actions) != action_contract["browser_use"]:
        raise RuntimeError("browser_use schema drifted from computer-use-actions.json")
    grouped_message, grouped_leaves = server.summarize_exception(
        ExceptionGroup("task group failed", [ValueError("regression leaf")])
    )
    if "regression leaf" not in grouped_message or grouped_leaves != ["ValueError: regression leaf"]:
        raise RuntimeError("ExceptionGroup diagnostics regressed to an opaque TaskGroup-style error")

    if os.name == "nt":
        windows_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
        csc = windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
        automation_candidates = list(
            (windows_root / "Microsoft.Net" / "assembly" / "GAC_MSIL" / "System.Management.Automation").glob(
                "*/System.Management.Automation.dll"
            )
        )
        automation_ref = automation_candidates[0] if automation_candidates else Path("__missing_System.Management.Automation.dll")
        launcher_source = Path(__file__).resolve().with_name("ElevatedBrokerLauncher.cs")
        interactive_launcher_source = Path(__file__).resolve().with_name("InteractiveBrokerLauncher.cs")
        helper_source = Path(__file__).resolve().with_name("ComputerUseHelper.cs")
        overlay_source = Path(__file__).resolve().with_name("ComputerUseOverlay.cs")
        activity_viewer_source = Path(__file__).resolve().with_name("ActivityLogViewer.cs")
        action_contract_source = package_parent / "coding_tools_mcp" / "computer-use-actions.json"
        helper_refs = [
            windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "System.Web.Extensions.dll",
            windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "WPF" / "UIAutomationClient.dll",
            windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "WPF" / "UIAutomationTypes.dll",
            windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "WPF" / "WindowsBase.dll",
        ]
        missing_helper_inputs = [
            path
            for path in [
                csc,
                automation_ref,
                launcher_source,
                interactive_launcher_source,
                helper_source,
                overlay_source,
                activity_viewer_source,
                action_contract_source,
                *helper_refs,
            ]
            if not path.is_file()
        ]
        if missing_helper_inputs:
            raise RuntimeError(
                "Computer Use helper build inputs are missing: "
                + ", ".join(str(path) for path in missing_helper_inputs)
            )

        # Regression contracts come from bugs we actually hit in production.
        helper_text = helper_source.read_text(encoding="utf-8")
        interactive_broker_text = Path(__file__).resolve().with_name("interactive-broker.ps1").read_text(encoding="utf-8")
        if "System.Management.Automation.Language.Parser]::ParseInput" not in interactive_broker_text:
            raise RuntimeError("active_user exec must reject PowerShell syntax errors before launching the child shell")
        for action in sorted(set(action_contract["computer_use"]) | set(action_contract["browser_use"])):
            if f'action == "{action}"' not in helper_text:
                raise RuntimeError(f"Computer Use backend has no implementation branch for advertised action: {action}")
        if 'right_click is not supported' in helper_text:
            raise RuntimeError("right_click regressed to a schema-only action")
        if 'if (action == "inspect") ActivateWindow' in helper_text:
            raise RuntimeError("inspect must not foreground the target window")
        capture_start = helper_text.find("private static Tuple<byte[], int, int> Capture")
        capture_end = helper_text.find("private static", capture_start + 20)
        if capture_start < 0 or "ActivateWindow(" in helper_text[capture_start:capture_end]:
            raise RuntimeError("screenshot capture must not foreground the target window")
        if "try { element.SetFocus(); return; }" in helper_text:
            raise RuntimeError("click must not report success when it only focused the element")
        if "computer-use-overlay-leases" not in helper_text:
            raise RuntimeError("Computer Use overlay must use per-operation leases")
        with tempfile.TemporaryDirectory(prefix="coding-tools-computer-use-build-") as helper_temp:
            (Path(helper_temp) / "computer-use-actions.json").write_bytes(action_contract_source.read_bytes())
            launcher_output = Path(helper_temp) / "elevated-broker-launcher.exe"
            launcher_compile = subprocess.run(
                [
                    str(csc),
                    "/nologo",
                    "/target:winexe",
                    "/optimize+",
                    f"/out:{launcher_output}",
                    f"/reference:{automation_ref}",
                    str(launcher_source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if launcher_compile.returncode != 0 or not launcher_output.is_file():
                raise RuntimeError(
                    "Elevated broker launcher failed to compile:\n" + launcher_compile.stdout[-8000:]
                )
            launcher_self_test = subprocess.run(
                [str(launcher_output), "--self-test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if launcher_self_test.returncode != 0:
                raise RuntimeError(
                    "Elevated broker launcher runtime self-test failed:\n" + launcher_self_test.stdout[-8000:]
                )
            interactive_launcher_output = Path(helper_temp) / "interactive-broker-launcher.exe"
            interactive_launcher_compile = subprocess.run(
                [
                    str(csc),
                    "/nologo",
                    "/target:winexe",
                    "/optimize+",
                    f"/out:{interactive_launcher_output}",
                    f"/reference:{automation_ref}",
                    str(interactive_launcher_source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if interactive_launcher_compile.returncode != 0 or not interactive_launcher_output.is_file():
                raise RuntimeError(
                    "Interactive broker launcher failed to compile:\n"
                    + interactive_launcher_compile.stdout[-8000:]
                )
            interactive_launcher_self_test = subprocess.run(
                [str(interactive_launcher_output), "--self-test"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if interactive_launcher_self_test.returncode != 0:
                raise RuntimeError(
                    "Interactive broker launcher runtime self-test failed:\n"
                    + interactive_launcher_self_test.stdout[-8000:]
                )
            helper_output = Path(helper_temp) / "computer-use-helper.exe"
            compile_result = subprocess.run(
                [
                    str(csc),
                    "/nologo",
                    "/target:exe",
                    "/optimize+",
                    f"/out:{helper_output}",
                    *(f"/reference:{path}" for path in helper_refs),
                    "/reference:System.Drawing.dll",
                    "/reference:System.Windows.Forms.dll",
                    str(helper_source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if compile_result.returncode != 0 or not helper_output.is_file():
                raise RuntimeError(
                    "Computer Use helper failed to compile:\n" + compile_result.stdout[-8000:]
                )
            request_json = json.dumps(
                {
                    "action": "list_windows",
                    "browser_only": False,
                    "include_screenshot": False,
                    "include_text": False,
                },
                separators=(",", ":"),
            ).encode("utf-8")
            helper_smoke = subprocess.run(
                [str(helper_output), "--request-base64", base64.b64encode(request_json).decode("ascii")],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=15,
            )
            if helper_smoke.returncode != 0:
                raise RuntimeError("Computer Use list_windows smoke test failed:\n" + helper_smoke.stdout[-8000:])
            try:
                smoke_payload = json.loads(helper_smoke.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError("Computer Use list_windows smoke test returned invalid JSON") from exc
            if not smoke_payload.get("ok") or smoke_payload.get("action") != "list_windows":
                raise RuntimeError("Computer Use list_windows smoke test returned an invalid payload")
            activity_viewer_output = Path(helper_temp) / "activity-log-viewer.exe"
            viewer_compile = subprocess.run(
                [
                    str(csc),
                    "/nologo",
                    "/target:winexe",
                    "/optimize+",
                    f"/out:{activity_viewer_output}",
                    "/reference:System.Drawing.dll",
                    "/reference:System.Windows.Forms.dll",
                    str(activity_viewer_source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if viewer_compile.returncode != 0 or not activity_viewer_output.is_file():
                raise RuntimeError(
                    "Activity Log viewer failed to compile:\n" + viewer_compile.stdout[-8000:]
                )

    human_runtime = server.Runtime(workspace, enable_view_image=False)
    try:
        human_runtime.initialized = True
        unknown_tool_response = server.dispatch_rpc(
            human_runtime,
            {
                "jsonrpc": "2.0",
                "id": "unknown-tool-regression",
                "method": "tools/call",
                "params": {"name": "definitely_removed_tool", "arguments": {}},
            },
        )
        unknown_error = (unknown_tool_response or {}).get("error", {})
        if unknown_error.get("code") != -32602 or "Unknown tool" not in str(unknown_error.get("message") or ""):
            raise RuntimeError("removed tools no longer fail cleanly with an Unknown tool JSON-RPC error")

        handoff_result = human_runtime.call_tool(
            "human_help_me",
            {
                "reason": "faster_by_human",
                "request": "Run one diagnostic command.",
                "expected_result": "Command output is visible.",
                "return_to_agent": "Paste the output.",
                "delivery": "chat_only",
            },
        )
        handoff = handoff_result.get("structuredContent", {})
        if handoff.get("status") != "human_action_required" or handoff.get("visibility") != "must_surface_to_user":
            raise RuntimeError("human_help_me did not produce a blocking handoff status")
        if handoff.get("request") != "Run one diagnostic command.":
            raise RuntimeError("human_help_me did not preserve the requested human action")
        rendered = "\n".join(
            str(item.get("text") or "")
            for item in handoff_result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        )
        if "HUMAN HELP NEEDED" not in rendered or "surface it in your next visible reply" not in rendered:
            raise RuntimeError("human_help_me model-facing handoff text is incomplete")
    finally:
        human_runtime.close()

    # Freeze the lightweight desktop/browser facade separately from the C#
    # helper implementation. This protects argument mapping and surface/skill
    # metadata while letting the facade move out of Runtime safely.
    with tempfile.TemporaryDirectory(prefix="coding-tools-desktop-contract-") as temporary:
        desktop_runtime = server.Runtime(Path(temporary), enable_view_image=False)
        original_computer_use = server.request_computer_use
        desktop_calls: list[dict[str, object]] = []

        def fake_computer_use(**kwargs: object) -> dict[str, object]:
            desktop_calls.append(dict(kwargs))
            return {"ok": True, "action": kwargs.get("action")}

        server.request_computer_use = fake_computer_use
        try:
            windows_result = desktop_runtime.computer_use(
                {
                    "action": "click",
                    "window_id": 42,
                    "x": 10,
                    "y": 20,
                    "include_screenshot": False,
                    "include_text": False,
                    "timeout_seconds": 12,
                }
            )
            browser_result = desktop_runtime.browser_use(
                {
                    "action": "navigate",
                    "url": "https://example.invalid/path",
                    "process_name": "chrome",
                }
            )
            if windows_result.get("surface") != "windows" or not str(windows_result.get("skill") or "").endswith("computer-use/SKILL.md"):
                raise RuntimeError("computer_use facade surface/skill metadata drifted")
            if browser_result.get("surface") != "browser" or not str(browser_result.get("skill") or "").endswith("control-chrome/SKILL.md"):
                raise RuntimeError("browser_use facade surface/skill metadata drifted")
            if len(desktop_calls) != 2:
                raise RuntimeError("desktop facade did not delegate exactly one broker call per tool call")
            windows_call, browser_call = desktop_calls
            if windows_call.get("browser_only") is not False or windows_call.get("window_id") != 42:
                raise RuntimeError("computer_use broker argument mapping drifted")
            if windows_call.get("x") != 10 or windows_call.get("y") != 20 or windows_call.get("timeout_seconds") != 12.0:
                raise RuntimeError("computer_use coordinate/timeout mapping drifted")
            if windows_call.get("include_screenshot") is not False or windows_call.get("include_text") is not False:
                raise RuntimeError("computer_use include flags drifted")
            if browser_call.get("browser_only") is not True or browser_call.get("action") != "navigate":
                raise RuntimeError("browser_use broker mode/action mapping drifted")
            if browser_call.get("text") != "https://example.invalid/path" or browser_call.get("process_name") != "chrome":
                raise RuntimeError("browser_use navigate URL mapping drifted")
        finally:
            server.request_computer_use = original_computer_use
            desktop_runtime.close()

    # Refactor characterization contracts. These intentionally exercise private
    # seams that are about to move out of server.py. They are not new product
    # behavior; they freeze ownership/cleanup rules so extraction cannot change
    # them accidentally.
    with tempfile.TemporaryDirectory(prefix="coding-tools-runtime-contract-") as temporary:
        runtime_workspace = Path(temporary)
        owner = server.Runtime(runtime_workspace, enable_view_image=False)
        owner._ensure_runtime_dirs()
        shared = server.Runtime(
            runtime_workspace,
            enable_view_image=False,
            project_context=owner.project_context,
            execution_registry=owner.execution_registry,
        )
        try:
            registry = owner.execution_registry
            if set(owner._tool_handlers) != set(server.TOOL_REGISTRY):
                raise RuntimeError("Runtime tool-handler catalog drifted from TOOL_REGISTRY")
            missing_handlers = [
                name for name, handler in owner._tool_handlers.items() if not callable(handler)
            ]
            if missing_handlers:
                raise RuntimeError(
                    "Runtime has non-callable registered tool handlers: "
                    + ", ".join(sorted(missing_handlers))
                )
            if not set(owner.exposed_tool_names()).issubset(owner._tool_handlers):
                raise RuntimeError("public tool surface contains a tool with no Runtime handler")

            runtime_dir = owner.runtime_dir
            if not runtime_dir.is_dir():
                raise RuntimeError("runtime characterization could not create its isolated runtime directory")
            shared.close()
            if registry.closed:
                raise RuntimeError("closing a shared Runtime incorrectly closed its ExecutionRegistry")
            if not runtime_dir.is_dir():
                raise RuntimeError("closing a shared Runtime incorrectly deleted the shared runtime directory")

            original_server_info_handler = owner._tool_handlers["server_info"]

            def failing_handler(_args: dict[str, object]) -> dict[str, object]:
                owner.request_sessions["cleanup-contract"] = "synthetic-session"
                owner.request_context.claimed_permission_grants = {"network"}
                raise server.ToolFailure(
                    "CHARACTERIZATION_FAILURE",
                    "synthetic handler failure",
                    category="internal",
                )

            owner._tool_handlers["server_info"] = failing_handler
            failed_result = owner.call_tool("server_info", {}, request_id="cleanup-contract")
            owner._tool_handlers["server_info"] = original_server_info_handler
            if not failed_result.get("isError"):
                raise RuntimeError("synthetic Runtime handler failure did not remain a tool error")
            if "cleanup-contract" in owner.request_sessions:
                raise RuntimeError("tool request/session mapping leaked after a handler failure")
            if getattr(owner.request_context, "request_id", None) is not None:
                raise RuntimeError("request_id leaked from Runtime request context after a handler failure")
            if getattr(owner.request_context, "tool_name", None) is not None:
                raise RuntimeError("tool_name leaked from Runtime request context after a handler failure")
            if getattr(owner.request_context, "arguments", None) is not None:
                raise RuntimeError("arguments leaked from Runtime request context after a handler failure")
            if getattr(owner.request_context, "claimed_permission_grants", None) != set():
                raise RuntimeError("permission claims leaked from Runtime request context after a handler failure")

            info = owner.server_info_payload()
            required_info_keys = {
                "server",
                "version",
                "build_identity",
                "protocol_version",
                "workspace",
                "default_cwd",
                "auth_enabled",
                "oauth",
                "exec_policy",
                "execution",
                "http_sessions",
                "tools",
                "tool_count",
            }
            missing_info_keys = sorted(required_info_keys.difference(info))
            if missing_info_keys:
                raise RuntimeError(
                    "server_info lost refactor-critical fields: " + ", ".join(missing_info_keys)
                )
            execution_info = info.get("execution", {})
            required_execution_keys = {"running", "starting", "retained_output", "max_running", "available_slots"}
            if not isinstance(execution_info, dict) or not required_execution_keys.issubset(execution_info):
                raise RuntimeError("server_info execution-pressure contract drifted")

            exec_environment = owner._exec_environment_summary()
            required_environment_keys = {"workspace", "permission_mode", "network_allowed", "runtime_dir", "home", "tmpdir", "cache_dir"}
            if not required_environment_keys.issubset(exec_environment):
                raise RuntimeError("exec-environment summary contract drifted")
            if execution_info.get("running") != 0 or execution_info.get("starting") != 0:
                raise RuntimeError("fresh Runtime execution-pressure baseline drifted")

            skill_dir = runtime_workspace / "skills" / "diagnostic-contract"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: diagnostic-contract\ndescription: Validator diagnostic skill.\n---\n",
                encoding="utf-8",
            )
            skills = owner._skill_catalog()
            matching_skills = [item for item in skills if item.get("name") == "diagnostic-contract"]
            if matching_skills != [
                {
                    "name": "diagnostic-contract",
                    "description": "Validator diagnostic skill.",
                    "path": "skills/diagnostic-contract/SKILL.md",
                }
            ]:
                raise RuntimeError("skill catalog metadata/path contract drifted")

            impossible_tool = "coding-tools-validator-definitely-missing-executable"
            discovery = owner._discover_tools([impossible_tool])
            if discovery != [{"name": impossible_tool, "available": False, "path": None}]:
                raise RuntimeError("tool discovery missing-executable contract drifted")
        finally:
            shared.close()
            owner.close()
        if not registry.closed:
            raise RuntimeError("the owning Runtime did not close its ExecutionRegistry")
        if runtime_dir.exists():
            raise RuntimeError("the owning Runtime did not clean up its isolated runtime directory")

    # ExecutionRegistry.close is the last-resort owner shutdown path. Freeze
    # its live-child behavior before relocating the registry out of server.py:
    # active sessions must be cleared, the process group must be hard-killed,
    # reader cleanup must run, and a repeated close must remain harmless.
    close_registry = server.ExecutionRegistry()
    close_process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **server.process_group_popen_kwargs(),
    )
    close_session = server.ExecSession("registry-close-contract", close_process)
    close_registry.sessions[close_session.session_id] = close_session
    try:
        close_registry.close()
        if not close_registry.closed:
            raise RuntimeError("ExecutionRegistry.close did not mark the registry closed")
        if close_registry.sessions or close_registry.output_sessions:
            raise RuntimeError("ExecutionRegistry.close did not clear session maps")
        if close_process.poll() is None:
            raise RuntimeError("ExecutionRegistry.close did not terminate a live child process")
        close_registry.close()
    finally:
        if close_process.poll() is None:
            close_process.kill()
            close_process.wait(timeout=5)

    # Freeze the retention/store behavior separately from output formatting and
    # stdin/kill orchestration. These are the first methods that will move onto
    # ExecutionRegistry after the registry class itself has been relocated.
    class _ExitedProcess:
        pid = 0

        @staticmethod
        def poll() -> int:
            return 0

    with tempfile.TemporaryDirectory(prefix="coding-tools-retention-check-") as temporary:
        retention_workspace = Path(temporary)
        retention_runtime = server.Runtime(retention_workspace, enable_view_image=False)
        try:
            promoted = server.ExecSession("retention-promoted", _ExitedProcess())
            retention_runtime.sessions[promoted.session_id] = promoted
            retention_runtime._complete_session(promoted)
            if promoted.session_id in retention_runtime.sessions:
                raise RuntimeError("completed session remained in the active registry")
            if retention_runtime.output_sessions.get(promoted.session_id) is not promoted:
                raise RuntimeError("completed session was not promoted to retained output")

            retention_runtime.output_sessions.clear()
            oldest_scratch = retention_workspace / "scratch-oldest"
            oldest_scratch.mkdir()
            total_to_remember = server.MAX_RETAINED_OUTPUT_SESSIONS + 1
            for index in range(total_to_remember):
                scratch = str(oldest_scratch) if index == 0 else ""
                session = server.ExecSession(
                    f"retention-{index:03d}",
                    _ExitedProcess(),
                    scratch_dir=scratch,
                )
                session.stdout.extend(f"output-{index}".encode("utf-8"))
                retention_runtime._remember_output_session(session)
            if len(retention_runtime.output_sessions) != server.MAX_RETAINED_OUTPUT_SESSIONS:
                raise RuntimeError("retained-session count eviction contract drifted")
            if "retention-000" in retention_runtime.output_sessions:
                raise RuntimeError("retained-session eviction stopped removing the oldest session")
            if oldest_scratch.exists():
                raise RuntimeError("retained-session eviction stopped cleaning the oldest scratch directory")

            retention_runtime.output_sessions.clear()
            expired_scratch = retention_workspace / "scratch-expired"
            expired_scratch.mkdir()
            expired = server.ExecSession(
                "retention-expired",
                _ExitedProcess(),
                scratch_dir=str(expired_scratch),
            )
            expired.closed = True
            expired.exit_code = 0
            expired.completed_at = time.time() - server.COMPLETED_SESSION_TTL_SECONDS - 1
            retention_runtime.output_sessions[expired.session_id] = expired
            retention_runtime._prune_sessions()
            if expired.session_id in retention_runtime.output_sessions:
                raise RuntimeError("completed-session TTL prune contract drifted")
            if expired_scratch.exists():
                raise RuntimeError("completed-session TTL prune stopped cleaning scratch directories")

            for lookup, expected_category in (
                (retention_runtime._get_output_session, "runtime"),
                (retention_runtime._get_session, "not_found"),
            ):
                try:
                    lookup("retention-missing")
                except server.ToolFailure as exc:
                    if exc.code != "SESSION_NOT_FOUND" or exc.category != expected_category:
                        raise RuntimeError("session lookup missing-error contract drifted") from exc
                else:
                    raise RuntimeError("missing session lookup stopped raising SESSION_NOT_FOUND")
        finally:
            retention_runtime.close()

    # Freeze output snapshot/paging separately from stdin and kill behavior.
    # Reconnect-safe byte cursors and output_ref continuation are public tool
    # contracts even though their implementation currently lives on Runtime.
    with tempfile.TemporaryDirectory(prefix="coding-tools-output-check-") as temporary:
        output_workspace = Path(temporary)
        output_runtime = server.Runtime(output_workspace, enable_view_image=False)
        try:
            delta_session = server.ExecSession("output-delta-contract", _ExitedProcess())
            delta_session.append_stdout(b"abcdef")
            delta_session.append_stderr(b"XYZ")
            delta = output_runtime._snapshot_session(
                delta_session,
                {
                    "output_mode": "delta",
                    "after_cursor": {"stdout": 2, "stderr": 1},
                },
                65536,
            )
            if delta.get("stdout") != "cdef" or delta.get("stderr") != "YZ":
                raise RuntimeError("explicit-cursor session delta contract drifted")
            if delta.get("cursor") != {"stdout": 6, "stderr": 3}:
                raise RuntimeError("session snapshot cursor contract drifted")

            try:
                output_runtime._snapshot_session(
                    delta_session,
                    {"output_mode": "definitely-invalid"},
                    65536,
                )
            except server.ToolFailure as exc:
                if exc.code != "INVALID_ARGUMENT":
                    raise RuntimeError("invalid output-mode error contract drifted") from exc
            else:
                raise RuntimeError("invalid output mode stopped being rejected")

            paged_session = server.ExecSession("output-page-contract", _ExitedProcess())
            paged_session.append_stdout(b"abcdef")
            truncated_payload = output_runtime._snapshot_session(
                paged_session,
                {"output_mode": "full"},
                3,
            )
            formatted = output_runtime._format_session_output(paged_session, truncated_payload, {})
            expected_ref = "session:output-page-contract:stdout"
            if not formatted.get("output_truncated") or formatted.get("output_ref") != expected_ref:
                raise RuntimeError("truncated output_ref formatting contract drifted")
            next_action = formatted.get("next_action")
            if not isinstance(next_action, dict) or next_action.get("tool") != "read_output":
                raise RuntimeError("truncated terminal output lost read_output next_action")
            if next_action.get("arguments", {}).get("output_ref") != expected_ref:
                raise RuntimeError("truncated terminal output next_action ref drifted")

            page = output_runtime.read_output(
                {
                    "output_ref": expected_ref,
                    "offset": 1,
                    "limit": 2,
                }
            )
            if page.get("content") != "bc" or page.get("next_offset") != 3:
                raise RuntimeError("read_output byte paging contract drifted")
            page_next = page.get("next_action")
            if not isinstance(page_next, dict) or page_next.get("arguments", {}).get("offset") != 3:
                raise RuntimeError("read_output next-page action contract drifted")

            try:
                output_runtime.read_output(
                    {
                        "output_ref": expected_ref,
                        "stream": "stderr",
                    }
                )
            except server.ToolFailure as exc:
                if exc.code != "INVALID_ARGUMENT":
                    raise RuntimeError("read_output stream-mismatch error contract drifted") from exc
            else:
                raise RuntimeError("read_output stopped rejecting stream/output_ref mismatch")
        finally:
            output_runtime.close()

    # Freeze session-id process control before moving it off Runtime. Request-id
    # cancellation remains Runtime glue and is intentionally not part of this
    # relocation contract.
    with tempfile.TemporaryDirectory(prefix="coding-tools-control-check-") as temporary:
        control_workspace = Path(temporary)
        session_runtime = server.Runtime(control_workspace, enable_view_image=False)
        kill_process: subprocess.Popen[bytes] | None = None
        cancel_process: subprocess.Popen[bytes] | None = None
        try:
            completed = server.ExecSession("control-completed", _ExitedProcess())
            completed.append_stdout(b"done")
            session_runtime.output_sessions[completed.session_id] = completed
            polled = session_runtime.poll_session(
                {
                    "session_id": completed.session_id,
                    "output_mode": "delta",
                    "after_cursor": {"stdout": 0, "stderr": 0},
                    "yield_time_ms": 0,
                }
            )
            if polled.get("status") != "exited" or polled.get("stdout") != "done":
                raise RuntimeError("completed-session poll contract drifted")
            try:
                session_runtime.write_stdin(
                    {
                        "session_id": completed.session_id,
                        "chars": "x",
                    }
                )
            except server.ToolFailure as exc:
                if exc.code != "SESSION_CLOSED" or exc.category != "runtime":
                    raise RuntimeError("closed-session stdin error contract drifted") from exc
            else:
                raise RuntimeError("write_stdin stopped rejecting input to a completed session")

            output_probe = server.ExecSession("control-output-probe", _ExitedProcess())
            output_probe.append_stdout(b"abcd")
            if not session_runtime._session_has_new_output(
                output_probe,
                {"after_cursor": {"stdout": 0, "stderr": 0}},
            ):
                raise RuntimeError("session new-output detection missed bytes after explicit cursor")
            if session_runtime._session_has_new_output(
                output_probe,
                {"after_cursor": {"stdout": 4, "stderr": 0}},
            ):
                raise RuntimeError("session new-output detection ignored an up-to-date explicit cursor")

            kill_process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **server.process_group_popen_kwargs(),
            )
            kill_session = server.ExecSession("control-kill", kill_process)
            session_runtime.sessions[kill_session.session_id] = kill_session
            killed = session_runtime.kill_session(
                {
                    "session_id": kill_session.session_id,
                    "signal": "KILL",
                    "wait_ms": 1000,
                    "kill_wait_ms": 1000,
                    "output_mode": "none",
                }
            )
            if killed.get("status") != "killed" or killed.get("signal_sent") != "SIGKILL":
                raise RuntimeError("forced kill-session result contract drifted")
            if killed.get("evicted") is not True or kill_process.poll() is None:
                raise RuntimeError("forced kill-session did not terminate and evict the live child")
            if kill_session.session_id in session_runtime.sessions or kill_session.session_id in session_runtime.output_sessions:
                raise RuntimeError("forced kill-session left the session in a registry map")

            cancel_process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **server.process_group_popen_kwargs(),
            )
            cancelled = server.ExecSession("control-cancel", cancel_process)
            session_runtime.sessions[cancelled.session_id] = cancelled
            session_runtime.cancel_session(cancelled.session_id)
            if cancelled.session_id in session_runtime.sessions:
                raise RuntimeError("cancel_session stopped removing the active session mapping")
        finally:
            session_runtime.close()
            for process in (kill_process, cancel_process):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    # Hidden session-inspection handlers remain in TOOL_REGISTRY even though
    # they are not on the public 20-tool surface. Freeze their behavior before
    # moving implementation into the registry/session service.
    with tempfile.TemporaryDirectory(prefix="coding-tools-session-inspect-") as temporary:
        inspect_runtime = server.Runtime(Path(temporary), enable_view_image=False)
        try:
            inspected = server.ExecSession(
                "inspect-completed",
                _ExitedProcess(),
                command_preview="echo inspect",
                cwd=str(temporary),
            )
            inspected.append_stdout(b"first line\nneedle line\nlast line\n")
            inspected.refresh_status()
            inspect_runtime.output_sessions[inspected.session_id] = inspected

            listed = inspect_runtime.list_sessions({"include_completed": True})
            if listed.get("completed") != 1 or listed.get("active") != 0:
                raise RuntimeError("hidden list_sessions count contract drifted")
            listed_rows = listed.get("sessions", [])
            if not listed_rows or listed_rows[0].get("session_id") != inspected.session_id:
                raise RuntimeError("hidden list_sessions metadata contract drifted")

            tailed = inspect_runtime.tail_output(
                {"session_id": inspected.session_id, "stream": "stdout", "lines": 1}
            )
            if "last line" not in str(tailed.get("content") or ""):
                raise RuntimeError("hidden tail_output contract drifted")

            found = inspect_runtime.find_output(
                {"session_id": inspected.session_id, "query": "needle", "stream": "stdout"}
            )
            matches = found.get("matches", [])
            if len(matches) != 1 or matches[0].get("line") != 2 or matches[0].get("column") != 1:
                raise RuntimeError("hidden find_output contract drifted")

            tree = inspect_runtime.process_tree({"session_id": inspected.session_id})
            if tree.get("session_id") != inspected.session_id or tree.get("process_tree") != []:
                raise RuntimeError("hidden process_tree contract drifted")
        finally:
            inspect_runtime.close()

    with tempfile.TemporaryDirectory(prefix="coding-tools-http-lifecycle-") as temporary:
        http_workspace = Path(temporary)
        control_runtime = server.Runtime(http_workspace, enable_view_image=False)
        registry = control_runtime.execution_registry

        def http_runtime_factory() -> server.Runtime:
            return server.Runtime(
                http_workspace,
                enable_view_image=False,
                project_context=control_runtime.project_context,
                execution_registry=registry,
                transport="http",
            )

        http_server = server.RuntimeHTTPServer(
            ("127.0.0.1", 0),
            server.MCPHandler,
            control_runtime,
            http_runtime_factory,
            enable_health=False,
        )
        reconnect_binding = http_server.sessions.create("http-lifecycle-owner")
        reconnect_runtime = reconnect_binding.runtime
        if reconnect_runtime.execution_registry is not registry:
            raise RuntimeError("HTTP reconnect Runtime did not share the control ExecutionRegistry")
        if reconnect_runtime._owns_execution_registry:
            raise RuntimeError("HTTP reconnect Runtime incorrectly owns the shared ExecutionRegistry")
        if registry.closed:
            raise RuntimeError("HTTP lifecycle setup unexpectedly closed the ExecutionRegistry")
        http_server.server_close()
        if not registry.closed:
            raise RuntimeError("RuntimeHTTPServer.server_close did not close the control ExecutionRegistry")
        if not control_runtime._closed:
            raise RuntimeError("RuntimeHTTPServer.server_close did not close its control Runtime")
        if not reconnect_runtime._closed:
            raise RuntimeError("RuntimeHTTPServer.server_close did not close reconnect session Runtimes")

    # Freeze the pure command-policy parsing layer before moving it out of
    # server.py. These checks intentionally stop before Runtime allow/deny
    # decisions; they capture shell structure and path/script classification.
    heredoc_command = "cat <<EOF > /etc/cron.d/evil\n</modelVersion>\nEOF\necho done\n"
    heredoc_live = server.strip_heredoc_payloads(heredoc_command)
    if "</modelVersion>" in heredoc_live:
        raise RuntimeError("heredoc payload stripping stopped removing stdin body data")
    if "> /etc/cron.d/evil" not in heredoc_live or "echo done" not in heredoc_live:
        raise RuntimeError("heredoc payload stripping hid live redirection/commands")
    quoted_heredoc = "printf '%s\\n' '<<EOF'\necho live\n"
    if server.strip_heredoc_payloads(quoted_heredoc) != quoted_heredoc:
        raise RuntimeError("quoted heredoc marker started being treated as a live heredoc")

    parsed_tokens = server.shlex_split("FOO=1 echo hi | cat ./file.txt")
    if server.command_executables(parsed_tokens) != ["echo", "cat"]:
        raise RuntimeError("shell executable discovery contract drifted")
    path_candidates = set(
        server.explicit_command_path_candidates(
            server.shlex_split("env -C ./sub FOO=1 python ./script.py > ./out.txt")
        )
    )
    if path_candidates != {"./sub", "./script.py", "./out.txt"}:
        raise RuntimeError("env-wrapped command path discovery contract drifted")
    env_candidates, env_command, env_args = server.env_wrapped_command(
        ["-C", "./sub", "FOO=1", "python", "./script.py"]
    )
    if env_candidates != ["./sub"] or env_command != "python" or env_args != ["./script.py"]:
        raise RuntimeError("env wrapped-command parsing contract drifted")
    if server.inline_script_command("env FOO=1 python -c 'print(1)'") != {
        "command": "python",
        "option": "-c",
    }:
        raise RuntimeError("inline-script detection contract drifted")

    inspectable_cases = {
        "file.txt": True,
        "./file": True,
        "../file": True,
        "https://example.invalid/file.txt": False,
        "bareword": False,
    }
    for candidate, expected in inspectable_cases.items():
        if server.is_inspectable_path_argument(candidate) is not expected:
            raise RuntimeError(f"inspectable path classification drifted for {candidate!r}")
    if not server.is_literal_network_reference_command("echo https://example.invalid/path"):
        raise RuntimeError("literal-network echo command stopped being classified as data-only")
    if server.is_literal_network_reference_command("curl https://example.invalid/path"):
        raise RuntimeError("network-capable curl command was misclassified as literal-only")

    # Freeze Runtime command-policy decisions separately from the pure parser
    # layer. Each case checks the first policy gate and permission detail that
    # callers currently receive; extraction must not reorder these decisions.
    with tempfile.TemporaryDirectory(prefix="coding-tools-command-policy-") as temporary:
        policy_workspace = Path(temporary)
        policy_runtime = server.Runtime(policy_workspace, enable_view_image=False, permission_mode="safe")
        try:
            policy_cases = [
                (
                    "active_user",
                    "echo hi",
                    {"execution_context": "active_user"},
                    "interactive_session",
                ),
                (
                    "sensitive_env",
                    "echo hi",
                    {"env": {"LD_PRELOAD": "./hook.so"}},
                    "sensitive_env",
                ),
                (
                    "inline_script",
                    "python -c 'print(1)'",
                    {},
                    server.INLINE_SCRIPT_PERMISSION,
                ),
                (
                    "shell_expansion",
                    "echo $(whoami)",
                    {},
                    "shell_expansion",
                ),
                (
                    "destructive_command",
                    "git reset --hard HEAD",
                    {},
                    "destructive_command",
                ),
                (
                    "network",
                    "curl https://example.invalid/path",
                    {},
                    "network",
                ),
                (
                    "filesystem_escape",
                    "cat ../outside.txt",
                    {},
                    "filesystem_escape",
                ),
            ]
            for label, command, arguments, permission in policy_cases:
                try:
                    policy_runtime._check_command_policy(command, arguments)
                except server.ToolFailure as exc:
                    if exc.code != "PERMISSION_REQUIRED" or exc.category != "permission":
                        raise RuntimeError(f"{label} command-policy error contract drifted") from exc
                    if exc.details.get("permission") != permission:
                        raise RuntimeError(f"{label} command-policy permission detail drifted") from exc
                else:
                    raise RuntimeError(f"{label} command-policy gate stopped rejecting the operation")

            # Literal URLs used only as data must remain allowed even when the
            # network capability itself is disabled.
            policy_runtime._check_command_policy("echo https://example.invalid/path", {})
        finally:
            policy_runtime.close()

    # Freeze execution orchestration separately from command policy. Exercise
    # one real managed child and active-user payload formatting via a broker
    # stub so relocation does not depend on a desktop being present in CI.
    with tempfile.TemporaryDirectory(prefix="coding-tools-exec-contract-") as temporary:
        exec_workspace = Path(temporary)
        exec_runtime = server.Runtime(exec_workspace, enable_view_image=False, permission_mode="dangerous")
        original_interactive_exec = server.request_interactive_exec
        try:
            quick_command = "Write-Output EXEC_CONTRACT" if os.name == "nt" else "printf 'EXEC_CONTRACT\\n'"
            quick = exec_runtime.exec_command(
                {
                    "cmd": quick_command,
                    "yield_time_ms": 30000,
                    "output_mode": "full",
                    "max_output_bytes": 65536,
                }
            )
            if quick.get("status") != "exited" or quick.get("exit_code") != 0:
                raise RuntimeError("managed exec terminal status contract drifted")
            if "EXEC_CONTRACT" not in str(quick.get("stdout") or ""):
                raise RuntimeError("managed exec stdout contract drifted")

            slow_command = (
                "Start-Sleep -Milliseconds 400; Write-Output EXEC_LATE"
                if os.name == "nt"
                else "sleep 0.4; printf 'EXEC_LATE\\n'"
            )
            running = exec_runtime.exec_command(
                {
                    "cmd": slow_command,
                    "yield_time_ms": 0,
                    "output_mode": "delta",
                    "max_output_bytes": 65536,
                }
            )
            if running.get("status") != "running" or not running.get("session_id"):
                raise RuntimeError("managed exec running-session contract drifted")
            next_action = running.get("next_action")
            if not isinstance(next_action, dict) or next_action.get("tool") != "poll_session":
                raise RuntimeError("managed exec running session lost poll continuation")
            exec_runtime.kill_session(
                {
                    "session_id": str(running["session_id"]),
                    "signal": "KILL",
                    "wait_ms": 1000,
                    "kill_wait_ms": 1000,
                    "output_mode": "summary",
                }
            )

            def fake_interactive_exec(**kwargs: object) -> dict[str, object]:
                if kwargs.get("cwd") != str(exec_workspace):
                    raise RuntimeError("active-user broker cwd mapping drifted")
                return {
                    "status": "exited",
                    "exit_code": 0,
                    "timed_out": False,
                    "elapsed_ms": 12,
                    "stdout": "ACTIVE_EXEC\\n",
                    "stderr": "",
                    "execution_identity": {"username": "validator"},
                    "process_id": 4242,
                }

            server.request_interactive_exec = fake_interactive_exec
            active = exec_runtime._exec_command_active_user(
                cmd="echo active",
                workdir=exec_workspace,
                args={"verbosity": "summary"},
                timeout_ms=1000,
                max_output_bytes=65536,
            )
            if active.get("execution_context") != "active_user" or active.get("managed_session") is not False:
                raise RuntimeError("active-user execution context contract drifted")
            if active.get("polling_supported") is not False or active.get("process_id") != 4242:
                raise RuntimeError("active-user one-shot metadata contract drifted")
            if "stdout" in active or "stderr" in active or "active_user" not in str(active.get("summary") or ""):
                raise RuntimeError("active-user summary formatting contract drifted")

            missing_tool_payload: dict[str, object] = {
                "status": "exited",
                "exit_code": 127,
                "stdout": "",
                "stderr": "definitely-missing-command: command not found",
            }
            exec_runtime._add_exec_diagnostics(missing_tool_payload)
            if missing_tool_payload.get("error_kind") != "tool_not_found":
                raise RuntimeError("exec diagnostic tool-not-found classification drifted")
            process_error = missing_tool_payload.get("process_error")
            if not isinstance(process_error, dict) or process_error.get("exit_code") != 127:
                raise RuntimeError("exec diagnostic process-error metadata drifted")

            timeout_payload: dict[str, object] = {
                "status": "timeout",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "timed_out": True,
            }
            exec_runtime._add_exec_diagnostics(timeout_payload)
            if timeout_payload.get("error_kind") != "timeout":
                raise RuntimeError("exec diagnostic timeout classification drifted")
        finally:
            server.request_interactive_exec = original_interactive_exec
            exec_runtime.close()

    context = load_project_context(workspace)
    scan_warnings = [warning for warning in context.warnings if "scan stopped" in warning.casefold()]
    if scan_warnings:
        raise RuntimeError("project-context discovery hit a scan limit: " + "; ".join(scan_warnings))

    if find_subsequence_all(["x"] * 12_000, ["x"] * 6_000) != list(range(6_001)):
        raise RuntimeError("linear patch hunk matcher did not find overlapping matches correctly")

    # Freeze the image-tool boundary before moving its handler/helpers out of
    # server.py. A minimal PNG header is enough for the current non-decoding
    # identification path and avoids making Pillow a validator requirement.
    with tempfile.TemporaryDirectory(prefix="coding-tools-image-contract-") as temporary:
        image_workspace = Path(temporary)
        image_path = image_workspace / "pixel.png"
        image_path.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + b"\x00" * 8
            + (1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
        )
        binary_path = image_workspace / "not-image.bin"
        binary_path.write_bytes(b"definitely-not-an-image")
        image_runtime = server.Runtime(image_workspace)
        try:
            image_payload = image_runtime.view_image({"path": "pixel.png"})
            if image_payload.get("path") != "pixel.png":
                raise RuntimeError("view_image path display contract drifted")
            if image_payload.get("mime_type") != "image/png":
                raise RuntimeError("view_image PNG mime detection drifted")
            if image_payload.get("width") != 1 or image_payload.get("height") != 1:
                raise RuntimeError("view_image PNG dimensions drifted")
            if image_payload.get("resized") is not False or image_payload.get("warnings") != []:
                raise RuntimeError("view_image no-resize result contract drifted")
            if not image_payload.get("_mcp_image_data"):
                raise RuntimeError("view_image stopped producing MCP image content data")
            try:
                image_runtime.view_image({"path": "not-image.bin"})
            except server.ToolFailure as exc:
                if exc.code != "BINARY_FILE":
                    raise
            else:
                raise RuntimeError("view_image accepted an unsupported binary file")
            try:
                image_runtime.view_image({"path": "pixel.png", "max_bytes": 8, "auto_resize": False})
            except server.ToolFailure as exc:
                if exc.code != "OUTPUT_TOO_LARGE":
                    raise
            else:
                raise RuntimeError("view_image max_bytes guard drifted")
        finally:
            image_runtime.close()

    # Freeze the workspace/path boundary before moving it out of server.py.
    original_workspace_allowlist = os.environ.get(server.WORKSPACE_ALLOWLIST_ENV)
    try:
        with tempfile.TemporaryDirectory(prefix="coding-tools-workspace-contract-") as temporary:
            contract_root = Path(temporary)
            alpha = contract_root / "alpha"
            beta = contract_root / "beta"
            outside = contract_root / "outside"
            nested = alpha / "nested"
            alpha.mkdir()
            beta.mkdir()
            outside.mkdir()
            nested.mkdir()
            marker = nested / "marker.txt"
            marker.write_text("workspace-contract\n", encoding="utf-8")
            os.environ[server.WORKSPACE_ALLOWLIST_ENV] = (
                f"Alpha={alpha}{os.pathsep}Beta={beta}"
            )

            catalog = server.workspace_catalog_from_env()
            if [entry.name for entry in catalog] != ["Alpha", "Beta"]:
                raise RuntimeError("workspace allowlist selector order/names drifted")
            if [entry.path for entry in catalog] != [alpha.resolve(), beta.resolve()]:
                raise RuntimeError("workspace allowlist path normalization drifted")
            if server.workspace_entry_for_selector("alpha").path != alpha.resolve():
                raise RuntimeError("workspace selector matching stopped being case-insensitive")
            if server.workspace_entry_for_selector(str(beta)).name != "Beta":
                raise RuntimeError("workspace selector stopped accepting an exact allowlisted path")
            allowed = server.validate_workspace_selection(alpha)
            if allowed != (alpha.resolve(), beta.resolve()):
                raise RuntimeError("workspace selection validation no longer returns the configured roots")
            try:
                server.validate_workspace_selection(outside)
            except server.ToolFailure as exc:
                if exc.code != "WORKSPACE_NOT_ALLOWED":
                    raise
            else:
                raise RuntimeError("workspace selection accepted a root outside the private allowlist")

            workspace_contract = server.Workspace(alpha)
            inside_absolute = workspace_contract.resolve_existing(str(marker.resolve()))
            if inside_absolute.path != marker.resolve() or inside_absolute.display != "nested/marker.txt":
                raise RuntimeError("absolute path inside the workspace no longer normalizes to a relative display")
            if workspace_contract.resolve_existing("nested/marker.txt").path != marker.resolve():
                raise RuntimeError("relative workspace path resolution drifted")
            try:
                workspace_contract.resolve_existing("../outside")
            except server.ToolFailure as exc:
                if exc.code != "PATH_OUTSIDE_WORKSPACE":
                    raise
            else:
                raise RuntimeError("workspace traversal guard accepted '..'")
            try:
                workspace_contract.resolve_existing(str(outside.resolve()))
            except server.ToolFailure as exc:
                if exc.code != "ABSOLUTE_PATH_DENIED":
                    raise
            else:
                raise RuntimeError("workspace accepted an absolute path outside its root")
            pending = workspace_contract.resolve_for_write("nested/new-file.txt")
            if pending.existed or pending.path != (nested / "new-file.txt").resolve(strict=False):
                raise RuntimeError("workspace write-target resolution drifted for a new file")
            if server.normalize_rel_display(alpha, alpha) != ".":
                raise RuntimeError("workspace relative display for root drifted")

            docs = alpha / "docs"
            docs_nested = docs / "nested"
            docs_nested.mkdir(parents=True)
            (docs / "a.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            (docs_nested / "c.txt").write_text("gamma beta\n", encoding="utf-8")
            (docs / "binary.bin").write_bytes(b"abc\x00def")
            filesystem_runtime = server.Runtime(alpha, enable_view_image=False)
            try:
                read_result = filesystem_runtime.read_file(
                    {"path": "docs/a.txt", "start_line": 2, "max_lines": 1}
                )
                read_content = str(read_result.get("content") or "").replace("\r\n", "\n")
                if read_content != "beta\n" or read_result.get("end_line") != 2:
                    raise RuntimeError("read_file line-selection contract drifted")
                try:
                    filesystem_runtime.read_file({"path": "docs/binary.bin"})
                except server.ToolFailure as exc:
                    if exc.code != "BINARY_FILE":
                        raise
                else:
                    raise RuntimeError("read_file binary guard drifted")

                listed = filesystem_runtime.list_dir(
                    {"path": "docs", "recursive": True, "max_depth": 3, "sort": "name"}
                )
                listed_paths = {str(item.get("path")) for item in listed.get("entries", [])}
                if not {"docs/a.txt", "docs/nested", "docs/nested/c.txt"}.issubset(listed_paths):
                    raise RuntimeError("list_dir recursive path contract drifted")

                files = filesystem_runtime.list_files(
                    {"path": "docs", "patterns": ["*.txt"], "sort": "path"}
                )
                file_paths = {str(item.get("path")) for item in files.get("files", [])}
                if not {"docs/a.txt", "docs/nested/c.txt"}.issubset(file_paths):
                    raise RuntimeError("list_files glob contract drifted")

                searched = filesystem_runtime.search_text(
                    {"query": "beta", "path": "docs", "case_sensitive": True}
                )
                match_paths = {str(item.get("path")) for item in searched.get("matches", [])}
                if match_paths != {"docs/a.txt", "docs/nested/c.txt"}:
                    raise RuntimeError("search_text literal-match contract drifted")
                if int(searched.get("total_matches", -1)) != 2:
                    raise RuntimeError("search_text total-match contract drifted")
            finally:
                filesystem_runtime.close()
    finally:
        if original_workspace_allowlist is None:
            os.environ.pop(server.WORKSPACE_ALLOWLIST_ENV, None)
        else:
            os.environ[server.WORKSPACE_ALLOWLIST_ENV] = original_workspace_allowlist

    # Freeze apply_patch orchestration before extracting it from Runtime. The
    # lower-level parser/committer already owns patch mechanics; these checks
    # capture Runtime-facing cwd, dry-run, move/delete, baseline, and staged
    # all-or-nothing behavior that is easiest to regress during relocation.
    with tempfile.TemporaryDirectory(prefix="coding-tools-patch-check-") as temporary:
        patch_workspace = Path(temporary)
        patch_project = patch_workspace / "project"
        patch_project.mkdir()
        patch_runtime = server.Runtime(patch_workspace, enable_view_image=False)
        try:
            patch_runtime.state_owner = "patch-check-owner"
            selected = patch_runtime.set_default_cwd({"path": "project"})
            if selected.get("default_cwd") != "project":
                raise RuntimeError("apply_patch characterization could not select project cwd")

            added = patch_runtime.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Add File: new.txt",
                            "+alpha",
                            "+beta",
                            "*** End Patch",
                        ]
                    )
                }
            )
            new_file = patch_project / "new.txt"
            if new_file.read_text(encoding="utf-8") != "alpha\nbeta\n":
                raise RuntimeError("apply_patch add-file content contract drifted")
            if added.get("base") != "project" or added.get("additions") != 2:
                raise RuntimeError("apply_patch add-file result contract drifted")
            baseline_key = "project/new.txt"
            if baseline_key not in patch_runtime.patch_baselines or patch_runtime.patch_baselines[baseline_key] is not None:
                raise RuntimeError("apply_patch add-file baseline contract drifted")

            baselines_before_dry_run = dict(patch_runtime.patch_baselines)
            dry_run = patch_runtime.apply_patch(
                {
                    "dry_run": True,
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: new.txt",
                            "@@",
                            "-alpha",
                            "+dry-run-change",
                            " beta",
                            "*** End Patch",
                        ]
                    ),
                }
            )
            if not dry_run.get("dry_run") or new_file.read_text(encoding="utf-8") != "alpha\nbeta\n":
                raise RuntimeError("apply_patch dry-run mutation contract drifted")
            if patch_runtime.patch_baselines != baselines_before_dry_run:
                raise RuntimeError("apply_patch dry-run baseline contract drifted")

            moved = patch_runtime.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: new.txt",
                            "*** Move to: moved.txt",
                            "@@",
                            "-alpha",
                            "+gamma",
                            " beta",
                            "*** End Patch",
                        ]
                    )
                }
            )
            moved_file = patch_project / "moved.txt"
            if new_file.exists() or moved_file.read_text(encoding="utf-8") != "gamma\nbeta\n":
                raise RuntimeError("apply_patch move/update filesystem contract drifted")
            moved_entries = moved.get("affected_files", [])
            if not any(
                item.get("path") == "project/moved.txt"
                and item.get("old_path") == "project/new.txt"
                and item.get("operation") == "move"
                for item in moved_entries
            ):
                raise RuntimeError("apply_patch move result metadata contract drifted")
            if "project/moved.txt" not in patch_runtime.patch_baselines:
                raise RuntimeError("apply_patch move destination baseline contract drifted")

            existing = patch_project / "existing.txt"
            existing.write_text("keep\n", encoding="utf-8")
            partial = patch_project / "should-not-exist.txt"
            try:
                patch_runtime.apply_patch(
                    {
                        "patch": "\n".join(
                            [
                                "*** Begin Patch",
                                "*** Add File: should-not-exist.txt",
                                "+temporary",
                                "*** Add File: existing.txt",
                                "+replacement",
                                "*** End Patch",
                            ]
                        )
                    }
                )
            except server.ToolFailure as exc:
                if exc.code != "PATCH_FAILED":
                    raise RuntimeError("apply_patch staged-failure error contract drifted") from exc
            else:
                raise RuntimeError("apply_patch staged-failure contract stopped rejecting existing add target")
            if partial.exists() or existing.read_text(encoding="utf-8") != "keep\n":
                raise RuntimeError("apply_patch staged validation failure partially committed files")

            deleted = patch_runtime.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Delete File: moved.txt",
                            "*** End Patch",
                        ]
                    )
                }
            )
            if moved_file.exists() or deleted.get("removals") != 2:
                raise RuntimeError("apply_patch delete-file contract drifted")
        finally:
            patch_runtime.close()

    # A directory-only shell change is a common model/user expectation. Verify
    # it becomes the shared owner cwd instead of disappearing with a one-shot
    # child shell, and verify another owner cannot inherit it.
    with tempfile.TemporaryDirectory(prefix="coding-tools-cwd-check-") as temporary:
        cwd_workspace = Path(temporary)
        project = cwd_workspace / "project"
        project.mkdir()
        nested_only = cwd_workspace / "nested" / "deep-project"
        nested_only.mkdir(parents=True)
        primary = server.Runtime(cwd_workspace, enable_view_image=False)
        try:
            primary.state_owner = "selfcheck-owner"
            web_project = primary.set_default_cwd({"project_name": "PROJECT"})
            if web_project.get("default_cwd") != "project":
                raise RuntimeError("Web Project name did not resolve case-insensitively to a first-level directory")
            missing_web_project = primary.set_default_cwd({"project_name": "deep-project"})
            if missing_web_project.get("default_cwd") != ".":
                raise RuntimeError("Web Project resolution searched recursively instead of falling back to workspace root")
            changed = primary.exec_command({"cmd": "cd project"})
            if not changed.get("cwd_persisted") or changed.get("default_cwd") != "project":
                raise RuntimeError("directory-only exec did not persist the new default cwd")
            parent = primary.exec_command({"cmd": "cd .."})
            if parent.get("default_cwd") != ".":
                raise RuntimeError("a safe parent-directory change did not return to the workspace root")
            windows_style = primary.exec_command({"cmd": 'cd /d "project"'})
            if windows_style.get("default_cwd") != "project":
                raise RuntimeError("CMD-style cd /d did not persist the new default cwd")
            patch_target = project / "patch-target.txt"
            patch_target.write_text("before\n", encoding="utf-8")
            subprocess.run(
                [server.require_git(), "init", "-q", str(project)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            patch_result = primary.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: patch-target.txt",
                            "@@",
                            "-before",
                            "+after",
                            "*** End Patch",
                        ]
                    )
                }
            )
            if patch_target.read_text(encoding="utf-8") != "after\n" or patch_result.get("base") != "project":
                raise RuntimeError("apply_patch did not resolve a relative path from the default cwd")
            repo, filters = primary._git_repo_scope({"path": "."})
            if repo != project or filters:
                raise RuntimeError("git_diff scope did not resolve '.' from the default cwd")

            git = server.require_git()
            for config_key, config_value in (
                ("user.name", "Coding Tools Validator"),
                ("user.email", "validator@example.invalid"),
            ):
                subprocess.run(
                    [git, "-C", str(project), "config", config_key, config_value],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            subprocess.run(
                [git, "-C", str(project), "add", "patch-target.txt"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [git, "-C", str(project), "commit", "-q", "-m", "validator baseline"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            patch_target.write_text("after\nchanged\n", encoding="utf-8")

            git_status_result = primary.git_status({"path": "."})
            status_entry = next(
                (
                    item
                    for item in git_status_result.get("entries", [])
                    if item.get("path") == "patch-target.txt"
                ),
                None,
            )
            if not git_status_result.get("is_repo") or status_entry is None:
                raise RuntimeError("git_status repository/change contract drifted")
            if status_entry.get("worktree_status") != "M":
                raise RuntimeError("git_status worktree-status contract drifted")

            git_diff_result = primary.git_diff({"path": "."})
            if "changed" not in str(git_diff_result.get("diff") or ""):
                raise RuntimeError("git_diff content contract drifted")
            if not any(
                item.get("path") == "patch-target.txt" and item.get("status") == "modified"
                for item in git_diff_result.get("files", [])
            ):
                raise RuntimeError("git_diff file metadata contract drifted")

            git_log_result = primary.git_log({"path": ".", "max_count": 1})
            commits = git_log_result.get("commits", [])
            if not commits or commits[0].get("subject") != "validator baseline":
                raise RuntimeError("git_log commit metadata contract drifted")

            git_show_result = primary.git_show(
                {"path": ".", "rev": "HEAD", "include_diff": False}
            )
            if not git_show_result.get("is_repo") or "validator baseline" not in str(
                git_show_result.get("content") or ""
            ):
                raise RuntimeError("git_show metadata-only contract drifted")

            git_blame_result = primary.git_blame(
                {"path": "patch-target.txt", "rev": "HEAD", "start_line": 1, "max_lines": 10}
            )
            blame_lines = git_blame_result.get("lines", [])
            if not git_blame_result.get("is_repo") or not blame_lines:
                raise RuntimeError("git_blame repository/line contract drifted")
            if str(blame_lines[0].get("content") or "").strip() != "after":
                raise RuntimeError("git_blame line-text contract drifted")
            if not str(blame_lines[0].get("commit") or ""):
                raise RuntimeError("git_blame commit attribution contract drifted")

            try:
                server.validate_git_ref("-unsafe")
            except server.ToolFailure as exc:
                if exc.code != "INVALID_ARGUMENT":
                    raise
            else:
                raise RuntimeError("git revision validation accepted an option-like ref")

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
                    privileged = primary.request_permissions(
                        {
                            "tool_name": "exec_command",
                            "permission": "privileged_executable",
                            "reason": "privilege-boundary self-check",
                            "arguments": {"cmd": "approved-tool"},
                        }
                    )
                    privileged_constraints = privileged.get("constraints", {})
                    if "never grants Administrator" not in str(privileged_constraints.get("privileged_executable_effect", "")):
                        raise RuntimeError("privileged_executable grant does not disclose its OS privilege boundary")
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
            (queue / "broker.heartbeat").write_text(str(time.time()), encoding="ascii")
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

            _, interactive_policy = runtime._interactive_command_env({})
            interactive_core = {str(name).upper() for name in interactive_policy.get("core_names", [])}
            required_interactive_windows_env = {"SYSTEMDRIVE", "PROGRAMDATA", "ALLUSERSPROFILE"}
            missing_interactive_env = sorted(required_interactive_windows_env.difference(interactive_core))
            if missing_interactive_env:
                raise RuntimeError(
                    "Interactive-user core environment is missing Windows known-folder variables: "
                    + ", ".join(missing_interactive_env)
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
