from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

SERVER_NAME = "human-help-mcp"
SERVER_TITLE = "Human Help MCP"
SERVER_VERSION = "0.1.0"

PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = (
    PROTOCOL_VERSION,
    "2025-06-18",
    "2026-07-28",
)

QUEUE_ENV = "HUMAN_HELP_MCP_INTERACTIVE_QUEUE"
LEGACY_QUEUE_ENV = "CODING_TOOLS_MCP_INTERACTIVE_QUEUE"
DEFAULT_INTERACTIVE_QUEUE = Path(
    r"C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests"
)
BROKER_HEARTBEAT_TTL_SECONDS = 30.0

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
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
        "request": {"type": "string", "minLength": 1, "maxLength": 4000},
        "expected_result": {"type": "string", "maxLength": 4000},
        "return_to_agent": {"type": "string", "maxLength": 4000},
        "mode": {
            "type": "string",
            "enum": ["prefer_human", "blocking"],
            "default": "prefer_human",
        },
        "fallback": {
            "type": "string",
            "enum": ["continue_best_effort", "wait_for_human"],
            "default": "continue_best_effort",
        },
        "delivery": {
            "type": "string",
            "enum": ["auto", "desktop_only", "chat_only"],
            "default": "auto",
        },
        "timeout_seconds": {
            "type": "integer",
            "minimum": 5,
            "maximum": 300,
            "default": 60,
        },
    },
    "required": ["reason", "request"],
    "additionalProperties": False,
}

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
    },
    "required": ["ok"],
    "additionalProperties": True,
}


class HumanHelpError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


def interactive_queue_path() -> Path:
    raw = (os.environ.get(QUEUE_ENV) or os.environ.get(LEGACY_QUEUE_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_INTERACTIVE_QUEUE


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _broker_pid(queue: Path) -> int | None:
    try:
        pid = int((queue / "broker.pid").read_text(encoding="ascii").strip())
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return pid if pid > 0 else None


def _process_is_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except PermissionError:
            return True
        except (ProcessLookupError, OSError):
            return False

    import ctypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5
    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _heartbeat_is_fresh(queue: Path) -> bool:
    try:
        age = time.time() - (queue / "broker.heartbeat").stat().st_mtime
    except OSError:
        return False
    return -30.0 <= age <= BROKER_HEARTBEAT_TTL_SECONDS


def interactive_broker_status() -> dict[str, Any]:
    queue = interactive_queue_path()
    pid = _broker_pid(queue) if queue.is_dir() else None
    alive = bool(pid and _process_is_alive(pid) and _heartbeat_is_fresh(queue))
    status_payload: dict[str, Any] = {}
    status_path = queue / "broker.status.json"
    if status_path.is_file():
        try:
            loaded = json.loads(status_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                status_payload = loaded
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    return {
        "available": alive,
        "queue": str(queue),
        "broker_pid": pid,
        "username": status_payload.get("username"),
        "session_id": status_payload.get("session_id"),
        "elevated": status_payload.get("elevated"),
        "run_level": status_payload.get("run_level"),
    }


def request_human_help(
    *,
    reason: str,
    request: str,
    expected_result: str,
    return_to_agent: str,
    mode: str,
    fallback: str,
    delivery: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    if os.name != "nt":
        raise HumanHelpError(
            "INTERACTIVE_CONTEXT_UNSUPPORTED",
            "Human-help desktop prompts are currently supported only on Windows.",
        )

    try:
        timeout = max(5.0, min(float(timeout_seconds), 300.0))
    except (TypeError, ValueError):
        timeout = 60.0

    queue = interactive_queue_path()
    status = interactive_broker_status()
    if not queue.is_dir() or not status.get("available"):
        raise HumanHelpError(
            "INTERACTIVE_BROKER_UNAVAILABLE",
            "The signed-in desktop broker is unavailable for a human-help prompt.",
            retryable=True,
            details=status,
        )

    request_id = secrets.token_urlsafe(18)
    request_path = queue / f"{request_id}.request"
    response_path = queue / f"{request_id}.response"
    activity_path = queue / f"{request_id}.web-human-help.activity"
    payload = {
        "protocol": 1,
        "request_id": request_id,
        "kind": "human_help",
        "created_at": time.time(),
        "requested_by": os.getpid(),
        "reason": str(reason),
        "request": str(request),
        "expected_result": str(expected_result),
        "return_to_agent": str(return_to_agent),
        "mode": str(mode),
        "fallback": str(fallback),
        "delivery": str(delivery),
        "timeout_seconds": int(timeout),
    }

    try:
        _write_json_atomically(request_path, payload)
    except (OSError, TypeError, ValueError) as exc:
        raise HumanHelpError(
            "INTERACTIVE_QUEUE_UNAVAILABLE",
            "The human-help request could not be queued.",
            retryable=True,
            details={"queue": str(queue), "reason": str(exc)},
        ) from exc

    deadline = time.monotonic() + timeout + 10.0
    last_activity_mtime_ns = 0
    try:
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HumanHelpError(
                        "INTERACTIVE_RESPONSE_INVALID",
                        "The desktop broker returned an invalid human-help response.",
                        retryable=True,
                        details={"request_id": request_id},
                    ) from exc

                try:
                    response_path.unlink(missing_ok=True)
                except OSError:
                    pass

                if not isinstance(response, dict) or response.get("request_id") != request_id:
                    raise HumanHelpError(
                        "INTERACTIVE_RESPONSE_INVALID",
                        "The human-help response did not match the request.",
                        details={"request_id": request_id},
                    )
                if not bool(response.get("ok")):
                    raise HumanHelpError(
                        str(response.get("error") or "HUMAN_HELP_FAILED"),
                        str(response.get("message") or "Human-help prompt failed."),
                        retryable=bool(response.get("retryable", False)),
                        details={"request_id": request_id, "broker": status},
                    )
                return response

            try:
                activity_mtime_ns = activity_path.stat().st_mtime_ns
            except OSError:
                activity_mtime_ns = 0
            if activity_mtime_ns > last_activity_mtime_ns:
                last_activity_mtime_ns = activity_mtime_ns
                deadline = time.monotonic() + timeout + 10.0
            time.sleep(0.1)
    finally:
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            activity_path.unlink(missing_ok=True)
        except OSError:
            pass

    raise HumanHelpError(
        "HUMAN_HELP_TIMEOUT",
        "Timed out waiting for the desktop broker to return the human-help result.",
        retryable=True,
        details={
            "request_id": request_id,
            "timeout_seconds": timeout,
            "broker": status,
        },
    )


def validate_arguments(args: dict[str, Any]) -> None:
    allowed = set(TOOL_SCHEMA["properties"])
    unknown = set(args) - allowed
    if unknown:
        raise HumanHelpError(
            "INVALID_ARGUMENT",
            f"Unknown arguments: {', '.join(sorted(unknown))}",
        )

    reason = args.get("reason")
    if reason not in TOOL_SCHEMA["properties"]["reason"]["enum"]:
        raise HumanHelpError("INVALID_ARGUMENT", "reason is required and invalid.")

    request = args.get("request")
    if not isinstance(request, str) or not request.strip() or len(request) > 4000:
        raise HumanHelpError(
            "INVALID_ARGUMENT",
            "request must be a non-empty string up to 4000 characters.",
        )

    for key in ("expected_result", "return_to_agent"):
        value = args.get(key, "")
        if not isinstance(value, str) or len(value) > 4000:
            raise HumanHelpError(
                "INVALID_ARGUMENT",
                f"{key} must be a string up to 4000 characters.",
            )

    mode = args.get("mode", "prefer_human")
    if mode not in {"prefer_human", "blocking"}:
        raise HumanHelpError("INVALID_ARGUMENT", "mode is invalid.")

    fallback = args.get("fallback", "continue_best_effort")
    if fallback not in {"continue_best_effort", "wait_for_human"}:
        raise HumanHelpError("INVALID_ARGUMENT", "fallback is invalid.")

    delivery = args.get("delivery", "auto")
    if delivery not in {"auto", "desktop_only", "chat_only"}:
        raise HumanHelpError("INVALID_ARGUMENT", "delivery is invalid.")

    timeout = args.get("timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 5 <= timeout <= 300:
        raise HumanHelpError(
            "INVALID_ARGUMENT",
            "timeout_seconds must be an integer from 5 to 300.",
        )


def human_help_tool(args: dict[str, Any]) -> dict[str, Any]:
    validate_arguments(args)

    request = str(args.get("request") or "").strip()
    expected_result = str(args.get("expected_result") or "").strip()
    return_to_agent = str(args.get("return_to_agent") or "").strip()
    reason = str(args.get("reason") or "other")
    mode = str(args.get("mode") or "prefer_human")
    fallback = str(args.get("fallback") or "continue_best_effort")
    delivery = str(args.get("delivery") or "auto")
    timeout_seconds = int(args.get("timeout_seconds") or 60)

    if delivery != "chat_only":
        try:
            response = request_human_help(
                reason=reason,
                request=request,
                expected_result=expected_result,
                return_to_agent=return_to_agent,
                mode=mode,
                fallback=fallback,
                delivery=delivery,
                timeout_seconds=timeout_seconds,
            )
            outcome = str(response.get("outcome") or "unknown")
            actual_delivery = (
                "web_qa"
                if str(response.get("execution_context") or "") == "web_console"
                else "desktop_qa"
            )
            if outcome in {"submitted", "done"}:
                return {
                    "ok": True,
                    "status": "human_completed",
                    "delivery": actual_delivery,
                    "reason": reason,
                    "request": request,
                    "answer": str(response.get("answer") or ""),
                    "outcome": outcome,
                    "agent_action": "resume_from_human_result",
                }
            return {
                "ok": True,
                "status": (
                    "human_declined"
                    if outcome == "skip"
                    else "human_unavailable"
                ),
                "delivery": actual_delivery,
                "reason": reason,
                "request": request,
                "answer": str(response.get("answer") or ""),
                "outcome": outcome,
                "agent_action": (
                    "continue_best_effort"
                    if fallback == "continue_best_effort"
                    else "wait_for_human"
                ),
                "agent_guidance": (
                    "The human skipped or did not answer. Continue with the best safe "
                    "agent path; do not repeat the same human request immediately."
                    if fallback == "continue_best_effort"
                    else "The human did not complete this blocking step. Stop this branch "
                    "until they explicitly return to it."
                ),
            }
        except HumanHelpError as exc:
            desktop_error = {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "details": exc.details,
            }
    else:
        desktop_error = None

    return {
        "ok": True,
        "status": "human_action_required",
        "delivery": "chat",
        "visibility": "must_surface_to_user",
        "reason": reason,
        "mode": mode,
        "fallback": fallback,
        "request": request,
        "expected_result": expected_result,
        "return_to_agent": return_to_agent,
        "desktop_error": desktop_error,
        "agent_action": "ask_user_visibly",
        "agent_guidance": (
            "Immediately show this exact small request in the assistant's visible reply; "
            "never assume MCP tool calls/results are visible to the human. Tell them they "
            "may skip it and ask you to continue if fallback=continue_best_effort."
        ),
    }


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "structuredContent": payload,
        "isError": False,
    }


def tool_definition() -> dict[str, Any]:
    return {
        "name": "human_help_me",
        "title": "Human help me",
        "description": (
            "Escalate one small step to the human. Use desktop_only in local agents; "
            "auto keeps Web Console first with desktop fallback. Surface chat fallbacks "
            "visibly. Never offload ordinary work."
        ),
        "inputSchema": TOOL_SCHEMA,
        "outputSchema": OUTPUT_SCHEMA,
        "annotations": {
            "title": "Human help me",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    }


def jsonrpc_error(
    request_id: str | int | None,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def dispatch(request: dict[str, Any], initialized: bool) -> tuple[dict[str, Any] | None, bool]:
    request_id = request.get("id")
    method = request.get("method")
    if request.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return jsonrpc_error(request_id, -32600, "Invalid Request"), initialized

    params = request.get("params") or {}
    if not isinstance(params, dict):
        return jsonrpc_error(request_id, -32602, "params must be an object"), initialized

    if method == "initialize":
        requested = params.get("protocolVersion", PROTOCOL_VERSION)
        if requested not in SUPPORTED_PROTOCOL_VERSIONS:
            return (
                jsonrpc_error(
                    request_id,
                    -32602,
                    "Unsupported MCP protocol version",
                    {"supported": list(SUPPORTED_PROTOCOL_VERSIONS)},
                ),
                initialized,
            )
        result = {
            "protocolVersion": requested,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": SERVER_TITLE,
                "version": SERVER_VERSION,
            },
            "instructions": (
                "Use human_help_me only for one small step that genuinely needs a human. "
                "Do not offload ordinary agent work."
            ),
        }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}, True

    if method == "notifications/initialized":
        return None, initialized

    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}, initialized

    if not initialized:
        return jsonrpc_error(request_id, -32002, "Server not initialized"), initialized

    if method == "tools/list":
        return (
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": [tool_definition()]},
            },
            initialized,
        )

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name != "human_help_me":
            return jsonrpc_error(request_id, -32601, f"Unknown tool: {name}"), initialized
        if not isinstance(arguments, dict):
            return jsonrpc_error(request_id, -32602, "arguments must be an object"), initialized
        try:
            payload = human_help_tool(arguments)
        except HumanHelpError as exc:
            payload = {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "details": exc.details,
                },
            }
            result = tool_result(payload)
            result["isError"] = True
        else:
            result = tool_result(payload)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}, initialized

    return jsonrpc_error(request_id, -32601, f"Unknown method: {method}"), initialized


def main() -> int:
    initialized = False
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            response, initialized = dispatch(request, initialized)
        except (json.JSONDecodeError, ValueError) as exc:
            response = jsonrpc_error(None, -32700, "Parse error", {"reason": str(exc)})

        if response is not None:
            sys.stdout.write(
                json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
