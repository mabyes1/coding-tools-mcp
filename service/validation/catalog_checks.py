from __future__ import annotations

import hashlib
import json
from typing import Any


EXPECTED_PUBLIC_CONTRACT_SHA256 = "3df3c465bba681718a5094320299e585518351104d0b0fd2c9057ed326742a73"


def run_catalog_checks(server: Any, elevated_actions: Any) -> dict[str, tuple[str, ...]]:
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
    exec_schema = schemas["exec_command"]
    execution_context = exec_schema.get("properties", {}).get("execution_context", {})
    if execution_context.get("enum") != ["service", "active_user"]:
        raise RuntimeError("exec_command execution_context schema drifted from service/active_user")
    for intent_tool in ("exec_command", "apply_patch"):
        intent_schema = schemas[intent_tool].get("properties", {}).get("intent", {})
        if intent_schema.get("type") != "string" or intent_schema.get("maxLength") != 160:
            raise RuntimeError(f"{intent_tool} lost its short user-facing activity intent contract")
    permission_schema = schemas["request_permissions"]["properties"]["permission"]
    if "interactive_session" not in permission_schema.get("enum", []):
        raise RuntimeError("interactive_session permission is missing from request_permissions schema")
    human_schema = schemas["human_help_me"]
    if set(human_schema.get("required", [])) != {"reason", "request"}:
        raise RuntimeError("human_help_me must require exactly reason and request")
    human_reasons = human_schema.get("properties", {}).get("reason", {}).get("enum", [])
    if "faster_by_human" not in human_reasons or "permission_blocked" not in human_reasons:
        raise RuntimeError("human_help_me reason schema is missing core escalation reasons")

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
