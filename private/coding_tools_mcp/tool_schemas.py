from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from .elevated_actions import ELEVATED_ACTIONS
from .errors import JsonRpcError, ToolFailure


INLINE_SCRIPT_PERMISSION = "inline_script"
IMAGE_RESIZE_MAX_DIMENSION = 2000


def object_schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def tool_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "error": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "category": {"type": "string"},
                    "retryable": {"type": "boolean"},
                    "details": {"type": "object", "additionalProperties": True},
                },
                "required": ["code", "message", "category", "retryable", "details"],
                "additionalProperties": True,
            },
        },
        "required": ["ok"],
        "additionalProperties": True,
    }


def validate_arguments(tool_name: str, args: dict[str, Any]) -> None:
    schema = input_schemas()[tool_name]
    try:
        validate_schema_value(args, schema, path="arguments")
    except ToolFailure as exc:
        raise JsonRpcError(-32602, exc.message, {"reason": "invalid_arguments", "code": exc.code}) from exc


def validate_schema_value(value: Any, schema: dict[str, Any], *, path: str) -> None:
    expected_type = schema.get("type")
    if expected_type is not None and not schema_type_matches(value, expected_type):
        raise ToolFailure("INVALID_ARGUMENT", f"{path} must be {schema_type_name(expected_type)}.", category="validation")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} is shorter than {min_length}.", category="validation")
        if "enum" in schema and value not in schema["enum"]:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be one of {schema['enum']!r}.", category="validation")

    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be >= {minimum}.", category="validation")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ToolFailure("INVALID_ARGUMENT", f"{path} must be <= {maximum}.", category="validation")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        for index, item in enumerate(value):
            validate_schema_value(item, item_schema, path=f"{path}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ToolFailure("INVALID_ARGUMENT", f"{path}.{key} is required.", category="validation")
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in properties:
                validate_schema_value(item, properties[key], path=child_path)
            elif additional is False:
                raise ToolFailure("INVALID_ARGUMENT", f"{child_path} is not a recognized argument.", category="validation")
            elif isinstance(additional, dict):
                validate_schema_value(item, additional, path=child_path)


def schema_type_matches(value: Any, expected_type: str | list[str]) -> bool:
    if isinstance(expected_type, list):
        return any(schema_type_matches(value, item) for item in expected_type)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "null":
        return value is None
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "string":
        return isinstance(value, str)
    return False


def schema_type_name(expected_type: str | list[str]) -> str:
    if isinstance(expected_type, list):
        return " or ".join(expected_type)
    return expected_type


@functools.cache
def computer_use_action_contract() -> dict[str, tuple[str, ...]]:
    path = Path(__file__).with_name("computer-use-actions.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("Computer Use action contract must be a JSON object")
    contract: dict[str, tuple[str, ...]] = {}
    for surface in ("computer_use", "browser_use"):
        values = raw.get(surface)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) and value for value in values):
            raise RuntimeError(f"Computer Use action contract is invalid for {surface}")
        if len(values) != len(set(values)):
            raise RuntimeError(f"Computer Use action contract contains duplicates for {surface}")
        contract[surface] = tuple(values)
    return contract


@functools.cache
def input_schemas() -> dict[str, dict[str, Any]]:
    # Cached: callers only read the returned tree, and rebuilding the full
    # ~190-line schema dict on every tools/call dispatch is measurable.
    string = {"type": "string"}
    integer = {"type": "integer"}
    boolean = {"type": "boolean"}
    string_array = {"type": "array", "items": {"type": "string"}}
    cursor_schema = {
        "type": "object",
        "properties": {
            "stdout": {**integer, "minimum": 0, "default": 0},
            "stderr": {**integer, "minimum": 0, "default": 0},
        },
        "additionalProperties": False,
    }
    return {
        "server_info": object_schema(),
        "human_help_me": object_schema(
            {
                "reason": {
                    **string,
                    "enum": [
                        "permission_blocked",
                        "gui_required",
                        "physical_action",
                        "faster_by_human",
                        "need_information",
                        "need_decision",
                        "other",
                    ],
                },
                "request": {**string, "minLength": 1, "maxLength": 4000},
                "expected_result": {**string, "maxLength": 4000},
                "return_to_agent": {**string, "maxLength": 4000},
                "mode": {**string, "enum": ["prefer_human", "blocking"], "default": "prefer_human"},
                "fallback": {**string, "enum": ["continue_best_effort", "wait_for_human"], "default": "continue_best_effort"},
                "delivery": {**string, "enum": ["auto", "chat_only"], "default": "auto"},
                "timeout_seconds": {**integer, "minimum": 5, "maximum": 300, "default": 60},
            },
            ["reason", "request"],
        ),
        "computer_use": object_schema(
            {
                "action": {
                    **string,
                    "enum": list(computer_use_action_contract()["computer_use"]),
                    "default": "inspect",
                },
                "window_id": integer,
                "title": string,
                "process_name": string,
                "x": integer,
                "y": integer,
                "element_index": {**integer, "minimum": 0},
                "text": string,
                "key": string,
                "scroll_y": integer,
                "include_screenshot": {**boolean, "default": True},
                "include_text": {**boolean, "default": True},
                "timeout_seconds": {**integer, "minimum": 2, "maximum": 60, "default": 30},
            }
        ),
        "browser_use": object_schema(
            {
                "action": {
                    **string,
                    "enum": list(computer_use_action_contract()["browser_use"]),
                    "default": "inspect",
                },
                "window_id": integer,
                "title": string,
                "process_name": {**string, "enum": ["chrome", "msedge"]},
                "x": integer,
                "y": integer,
                "element_index": {**integer, "minimum": 0},
                "text": string,
                "url": string,
                "key": string,
                "scroll_y": integer,
                "include_screenshot": {**boolean, "default": True},
                "include_text": {**boolean, "default": True},
                "timeout_seconds": {**integer, "minimum": 2, "maximum": 60, "default": 30},
            }
        ),
        "check_exec_environment": object_schema(
            {"tools": {"type": "array", "items": {**string, "minLength": 1}, "maxItems": 64}}
        ),
        "which_tools": object_schema(
            {"tools": {"type": "array", "items": {**string, "minLength": 1}, "maxItems": 64}},
        ),
        "get_default_cwd": object_schema(),
        "list_workspaces": object_schema(),
        "switch_workspace": object_schema(
            {"workspace": {**string, "minLength": 1}},
            ["workspace"],
        ),
        "set_default_cwd": object_schema(
            {
                "path": {**string, "default": "."},
                "project_name": {**string, "minLength": 1, "maxLength": 255},
            }
        ),
        "read_file": object_schema(
            {
                "path": {**string, "minLength": 1},
                "start_line": {**integer, "minimum": 1, "default": 1},
                "end_line": {**integer, "minimum": 1},
                "max_lines": {**integer, "minimum": 1},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 131072},
                "encoding": {**string, "enum": ["utf-8"], "default": "utf-8"},
            },
            ["path"],
        ),
        "list_dir": object_schema(
            {
                "path": {**string, "default": "."},
                "recursive": {**boolean, "default": False},
                "max_depth": {**integer, "minimum": 1, "maximum": 20, "default": 1},
                "max_entries": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
                "include_hidden": {**boolean, "default": False},
                "include_ignored": {**boolean, "default": False},
                "sort": {**string, "enum": ["name", "type", "modified"], "default": "name"},
            }
        ),
        "list_files": object_schema(
            {
                "path": {**string, "default": "."},
                "patterns": string_array,
                "glob": string,
                "exclude_patterns": string_array,
                "include_hidden": {**boolean, "default": False},
                "include_ignored": {**boolean, "default": False},
                "max_results": {**integer, "minimum": 1, "maximum": 50000, "default": 5000},
                "sort": {**string, "enum": ["path", "modified"], "default": "path"},
            }
        ),
        "search_text": object_schema(
            {
                "query": {**string, "minLength": 1},
                "path": {**string, "default": "."},
                "regex": {**boolean, "default": False},
                "case_sensitive": {**boolean, "default": False},
                "include_globs": string_array,
                "glob": string,
                "exclude_globs": string_array,
                "context_lines": {**integer, "minimum": 0, "maximum": 5, "default": 0},
                "max_results": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
                "max_preview_bytes": {**integer, "minimum": 80, "maximum": 4096, "default": 512},
            },
            ["query"],
        ),
        "apply_patch": object_schema(
            {
                "patch": {**string, "minLength": 1},
                "dry_run": {**boolean, "default": False},
                "intent": {
                    **string,
                    "maxLength": 160,
                    "description": "Short user-facing description of why this edit is being made. Used by the Web Console activity UI.",
                },
            },
            ["patch"],
        ),
        "exec_command": object_schema(
            {
                "cmd": {**string, "minLength": 1},
                "intent": {
                    **string,
                    "maxLength": 160,
                    "description": "Short user-facing description of why this command is being run. Used by the Web Console activity UI.",
                },
                "execution_context": {**string, "enum": ["service", "active_user"], "default": "service"},
                "workdir": {**string, "default": "."},
                "cwd": {**string},
                "timeout_ms": {**integer, "minimum": 1, "maximum": 600000, "default": 30000},
                "yield_time_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 10000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "output_mode": {**string, "enum": ["delta", "tail", "none", "summary", "full"], "default": "delta"},
                "tail_lines": {**integer, "minimum": 1, "maximum": 1000, "default": 20},
                "after_cursor": cursor_schema,
                "verbosity": {**string, "enum": ["summary", "preview", "full"]},
                "preview_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
                "stdin": {**string, "default": ""},
                "tty": {**boolean, "default": False},
                "env": {"type": "object", "additionalProperties": {"type": "string"}, "default": {}},
            },
            ["cmd"],
        ),
        "write_stdin": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "chars": {**string, "default": ""},
                "yield_time_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 10000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "output_mode": {**string, "enum": ["delta", "tail", "none", "summary", "full"], "default": "delta"},
                "tail_lines": {**integer, "minimum": 1, "maximum": 1000, "default": 20},
                "after_cursor": cursor_schema,
                "verbosity": {**string, "enum": ["summary", "preview", "full"]},
                "preview_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
            },
            ["session_id"],
        ),
        "poll_session": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "yield_time_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 0},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "output_mode": {**string, "enum": ["delta", "tail", "none", "summary", "full"], "default": "delta"},
                "tail_lines": {**integer, "minimum": 1, "maximum": 1000, "default": 20},
                "after_cursor": cursor_schema,
                "verbosity": {**string, "enum": ["summary", "preview", "full"]},
                "preview_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
            },
            ["session_id"],
        ),
        "kill_session": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "signal": {**string, "enum": ["TERM", "KILL", "INT"], "default": "TERM"},
                "wait_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 5000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "output_mode": {**string, "enum": ["delta", "tail", "none", "summary", "full"], "default": "delta"},
                "tail_lines": {**integer, "minimum": 1, "maximum": 1000, "default": 20},
                "after_cursor": cursor_schema,
                "verbosity": {**string, "enum": ["summary", "preview", "full"]},
                "preview_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
            },
            ["session_id"],
        ),
        "kill_tree": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "force": {**boolean, "default": True},
                "wait_ms": {**integer, "minimum": 0, "maximum": 30000, "default": 5000},
                "max_output_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 65536},
                "output_mode": {**string, "enum": ["delta", "tail", "none", "summary", "full"], "default": "summary"},
                "tail_lines": {**integer, "minimum": 1, "maximum": 1000, "default": 20},
                "after_cursor": cursor_schema,
            },
            ["session_id"],
        ),
        "list_sessions": object_schema(
            {
                "include_completed": {**boolean, "default": True},
                "include_process_tree": {**boolean, "default": False},
            }
        ),
        "process_tree": object_schema(
            {"session_id": {**string, "minLength": 1}},
            ["session_id"],
        ),
        "read_output": object_schema(
            {
                "output_ref": {**string, "minLength": 1},
                "stream": {**string, "enum": ["stdout", "stderr"]},
                "offset": {**integer, "minimum": 0, "default": 0},
                "limit": {**integer, "minimum": 1, "maximum": 1048576, "default": 4096},
            },
            ["output_ref"],
        ),
        "tail_output": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "stream": {**string, "enum": ["stdout", "stderr"], "default": "stdout"},
                "lines": {**integer, "minimum": 1, "maximum": 1000, "default": 20},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 16384},
            },
            ["session_id"],
        ),
        "find_output": object_schema(
            {
                "session_id": {**string, "minLength": 1},
                "query": {**string, "minLength": 1},
                "stream": {**string, "enum": ["stdout", "stderr", "both"], "default": "both"},
                "regex": {**boolean, "default": False},
                "case_sensitive": {**boolean, "default": False},
                "max_results": {**integer, "minimum": 1, "maximum": 1000, "default": 100},
            },
            ["session_id", "query"],
        ),
        "git_status": object_schema(
            {
                "path": {**string, "default": "."},
                "include_untracked": {**boolean, "default": True},
                "max_entries": {**integer, "minimum": 1, "maximum": 10000, "default": 1000},
            }
        ),
        "git_diff": object_schema(
            {
                "path": string,
                "paths": string_array,
                "staged": {**boolean, "default": False},
                "unstaged": {**boolean, "default": True},
                "context_lines": {**integer, "minimum": 0, "maximum": 20, "default": 3},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 262144},
            }
        ),
        "git_log": object_schema(
            {
                "path": {**string, "default": "."},
                "ref": {**string, "default": "HEAD"},
                "max_count": {**integer, "minimum": 1, "maximum": 100, "default": 20},
                "skip": {**integer, "minimum": 0, "maximum": 10000, "default": 0},
            }
        ),
        "git_show": object_schema(
            {
                "rev": {**string, "default": "HEAD"},
                "path": string,
                "paths": string_array,
                "include_diff": {**boolean, "default": True},
                "context_lines": {**integer, "minimum": 0, "maximum": 20, "default": 3},
                "max_bytes": {**integer, "minimum": 1, "maximum": 1048576, "default": 262144},
            }
        ),
        "git_blame": object_schema(
            {
                "path": {**string, "minLength": 1},
                "rev": string,
                "start_line": {**integer, "minimum": 1, "default": 1},
                "end_line": {**integer, "minimum": 1},
                "max_lines": {**integer, "minimum": 1, "maximum": 1000, "default": 200},
            },
            ["path"],
        ),
        "request_permissions": object_schema(
            {
                "tool_name": {**string, "enum": ["exec_command", "apply_patch"]},
                "permission": {
                    **string,
                    "enum": [
                        "network",
                        "destructive_command",
                        "interactive_session",
                        "long_timeout",
                        "sensitive_env",
                        "shell_expansion",
                        INLINE_SCRIPT_PERMISSION,
                        "privileged_executable",
                        "filesystem_escape",
                        "write_generated_or_ignored",
                    ],
                },
                "reason": {**string, "minLength": 1},
                "arguments": {"type": "object", "additionalProperties": True},
                "scope": {**string, "enum": ["once", "session"], "default": "once"},
                "ttl_seconds": {**integer, "minimum": 1, "maximum": 3600, "default": 300},
                "approval_timeout_seconds": {**integer, "minimum": 5, "maximum": 85, "default": 75},
            },
            ["tool_name", "permission", "reason", "arguments"],
        ),
        "request_elevated_action": object_schema(
            {
                "action": {**string, "enum": sorted(ELEVATED_ACTIONS)},
                "timeout_ms": {**integer, "minimum": 1000, "maximum": 600000, "default": 300000},
            },
            ["action"],
        ),
        "view_image": object_schema(
            {
                "path": {**string, "minLength": 1},
                "max_bytes": {**integer, "minimum": 1024, "maximum": 10485760, "default": 5242880},
                "max_width": {**integer, "minimum": 1, "maximum": 10000, "default": IMAGE_RESIZE_MAX_DIMENSION},
                "max_height": {**integer, "minimum": 1, "maximum": 10000, "default": IMAGE_RESIZE_MAX_DIMENSION},
                "auto_resize": {**boolean, "default": True},
            },
            ["path"],
        ),
    }


__all__ = [
    "IMAGE_RESIZE_MAX_DIMENSION",
    "INLINE_SCRIPT_PERMISSION",
    "computer_use_action_contract",
    "input_schemas",
    "object_schema",
    "schema_type_matches",
    "schema_type_name",
    "tool_output_schema",
    "validate_arguments",
    "validate_schema_value",
]
