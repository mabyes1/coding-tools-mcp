from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


def run_runtime_lifecycle_checks(server: Any) -> None:
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
