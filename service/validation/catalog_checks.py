from __future__ import annotations

import hashlib
import json
from typing import Any


EXPECTED_PUBLIC_CONTRACT_SHA256 = "a52c9cce2eead7bf63175461b346220b5e26a255093a9fae8027961397c94f21"


def run_catalog_checks(server: Any, elevated_actions: Any, activity_module: Any) -> dict[str, tuple[str, ...]]:
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
    if elevated_actions.ELEVATED_ACTIONS.intersection(elevated_actions.MCP_PERMISSION_NAMES):
        raise RuntimeError("ordinary MCP permission names overlap true elevated action names")
    try:
        elevated_actions.request_elevated_action("validator-unregistered-elevated-action", timeout_seconds=1)
    except server.ToolFailure as exc:
        if exc.code != "ELEVATED_ACTION_NOT_ALLOWED" or exc.category != "security":
            raise RuntimeError("unregistered elevated action no longer fails at the manifest boundary") from exc
    else:
        raise RuntimeError("unregistered elevated action bypassed the manifest allowlist")
    try:
        elevated_actions.request_permission_approval(
            tool_name="request_elevated_action",
            permission="network",
            reason="validator boundary check",
            arguments={},
            scope="once",
            ttl_seconds=60,
        )
    except server.ToolFailure as exc:
        if exc.code != "INVALID_ARGUMENT":
            raise RuntimeError("ordinary permission approval accepted an elevated-action tool") from exc
    else:
        raise RuntimeError("ordinary permission approval crossed into the elevated-action API")

    public_contract = [server.tool_definition(name) for name in server.PUBLIC_TOOL_NAMES]
    public_contract_bytes = json.dumps(
        public_contract,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    public_contract_sha256 = hashlib.sha256(public_contract_bytes).hexdigest()
    if public_contract_sha256 != EXPECTED_PUBLIC_CONTRACT_SHA256:
        raise RuntimeError(
            "public tool definition/schema contract changed; "
            f"expected sha256={EXPECTED_PUBLIC_CONTRACT_SHA256}, actual={public_contract_sha256}. "
            "If this is an intentional product/API change, review the complete tools/list diff before updating this baseline."
        )

    schemas = server.input_schemas()
    cwd_workspace_schema = schemas["set_default_cwd"].get("properties", {}).get("workspace", {})
    if cwd_workspace_schema.get("type") != "string" or cwd_workspace_schema.get("maxLength") != 64:
        raise RuntimeError("set_default_cwd lost its named-workspace selector")
    exec_schema = schemas["exec_command"]
    execution_context = exec_schema.get("properties", {}).get("execution_context", {})
    if execution_context.get("enum") != ["service", "active_user"]:
        raise RuntimeError("exec_command execution_context schema drifted from service/active_user")
    for intent_tool in ("exec_command", "apply_patch"):
        intent_schema = schemas[intent_tool].get("properties", {}).get("intent", {})
        if (
            intent_schema.get("type") != "string"
            or intent_schema.get("minLength") != 1
            or intent_schema.get("maxLength") != 160
        ):
            raise RuntimeError(f"{intent_tool} lost its short user-facing activity intent contract")
        if "intent" not in schemas[intent_tool].get("required", []):
            raise RuntimeError(f"{intent_tool} must require a user-facing activity intent")
        try:
            server.validate_arguments(intent_tool, {"cmd" if intent_tool == "exec_command" else "patch": "x", "intent": "   "})
        except server.JsonRpcError as exc:
            if exc.code != -32602 or "user-facing description" not in exc.message:
                raise RuntimeError(f"{intent_tool} blank intent validation returned the wrong error") from exc
        else:
            raise RuntimeError(f"{intent_tool} accepted a blank user-facing activity intent")

    activity_intent = "review intent visibility"
    exec_start = activity_module._activity_start_lines(
        "exec_command",
        {"cmd": "echo test", "intent": activity_intent, "execution_context": "service"},
    )
    exec_done = activity_module._activity_log_lines(
        "exec_command",
        {"cmd": "echo test", "intent": activity_intent, "execution_context": "service"},
        {"ok": True, "exit_code": 0, "stdout": "test"},
        12,
    )
    patch_start = activity_module._activity_start_lines(
        "apply_patch",
        {"patch": "*** Begin Patch", "intent": activity_intent},
    )
    patch_done = activity_module._activity_log_lines(
        "apply_patch",
        {"patch": "*** Begin Patch", "intent": activity_intent},
        {"ok": True, "additions": 1, "removals": 0},
        12,
    )
    for label, lines in (
        ("exec start", exec_start),
        ("exec done", exec_done),
        ("patch start", patch_start),
        ("patch done", patch_done),
    ):
        if not any(activity_intent in line for line in lines):
            raise RuntimeError(f"Web Console activity intent disappeared from {label} rendering")

    human_request = "Start the existing Tunnel-Coding task"
    human_args = {"reason": "faster_by_human", "request": human_request}
    human_start = activity_module._activity_start_lines("human_help_me", human_args)
    human_done = activity_module._activity_log_lines(
        "human_help_me",
        human_args,
        {"ok": True, "status": "human_action_required"},
        12,
    )
    for label, lines in (("human help start", human_start), ("human help done", human_done)):
        if not any(human_request in line for line in lines):
            raise RuntimeError(f"Web Console HUMAN HELP request summary disappeared from {label} rendering")

    sensitive_human_request = "Use api_key=sk-1234567890abcdef for this diagnostic"
    sensitive_human_lines = activity_module._activity_start_lines(
        "human_help_me",
        {"reason": "need_information", "request": sensitive_human_request},
    )
    sensitive_rendered = "\n".join(sensitive_human_lines)
    if "sk-1234567890abcdef" in sensitive_rendered or "[REDACTED]" not in sensitive_rendered:
        raise RuntimeError("Web Console HUMAN HELP request summary leaked a sensitive value")
    permission_schema = schemas["request_permissions"]["properties"]["permission"]
    if "interactive_session" not in permission_schema.get("enum", []):
        raise RuntimeError("interactive_session permission is missing from request_permissions schema")
    human_schema = schemas["human_help_me"]
    if set(human_schema.get("required", [])) != {"reason", "request"}:
        raise RuntimeError("human_help_me must require exactly reason and request")
    human_reasons = human_schema.get("properties", {}).get("reason", {}).get("enum", [])
    if "faster_by_human" not in human_reasons or "permission_blocked" not in human_reasons:
        raise RuntimeError("human_help_me reason schema is missing core escalation reasons")
    human_description = server.TOOL_REGISTRY["human_help_me"].description
    for phrase in ("efficiency tradeoff", "time, tokens, tool calls", "complete actionable steps"):
        if phrase not in human_description:
            raise RuntimeError(f"human_help_me description lost efficiency-routing guidance: {phrase}")
    human_delivery = human_schema.get("properties", {}).get("delivery", {}).get("enum", [])
    if not {"auto", "desktop_only", "chat_only"}.issubset(human_delivery):
        raise RuntimeError("human_help_me delivery schema is missing automatic, desktop-only, or chat-only routing")

    computer_actions = schemas["computer_use"]["properties"]["action"].get("enum", [])
    if "inspect" not in computer_actions or "click" not in computer_actions or "type_text" not in computer_actions:
        raise RuntimeError("computer_use schema is missing core UI actions")
    browser_actions = schemas["browser_use"]["properties"]["action"].get("enum", [])
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

    return action_contract
