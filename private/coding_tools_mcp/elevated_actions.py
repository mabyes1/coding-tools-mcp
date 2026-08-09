"""Interactive approval bridge from the LocalService MCP process to a user broker.

Permission requests display a signed-in-user dialog and only create a scoped,
short-lived in-memory grant. Elevated actions remain restricted to registered
action names; the broker validates the fixed script path and hash before launch.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from .errors import ToolFailure


ELEVATED_QUEUE_ENV = "CODING_TOOLS_MCP_ELEVATED_QUEUE"
DEFAULT_ELEVATED_QUEUE = Path(r"C:\ProgramData\WebGPTCodingToolsMCPService\elevated-requests")
ELEVATED_PROTOCOL_VERSION = 1
ELEVATED_ACTIONS = frozenset({
    "install-vibedeck-update",
    "repair-vibedeck-autostart",
    "sync-installed-webroot",
    "update-private-mcp",
})
ELEVATED_REQUEST_TTL_SECONDS = 900
MCP_PERMISSION_NAMES = frozenset({
    "network",
    "destructive_command",
    "long_timeout",
    "sensitive_env",
    "shell_expansion",
    "inline_script",
    "privileged_executable",
    "filesystem_escape",
    "write_generated_or_ignored",
})


def elevated_queue_path() -> Path:
    raw = (os.environ.get(ELEVATED_QUEUE_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_ELEVATED_QUEUE


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)


def _broker_is_alive(queue: Path) -> tuple[bool, int | None]:
    """Use the broker heartbeat to distinguish unavailable from approval timeout."""
    pid_path = queue / "broker.pid"
    try:
        pid = int(pid_path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeDecodeError, ValueError):
        return False, None
    if pid <= 0:
        return False, pid
    if os.name == "nt":
        # os.kill(pid, 0) is not a portable existence probe on Windows and
        # raises WinError 87 on current CPython builds. A synchronization
        # handle can be queried without terminating or otherwise signaling the
        # elevated broker. Access denied still proves that the PID exists.
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
            return ctypes.get_last_error() == 5, pid
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout, pid
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True, pid
    except (ProcessLookupError, OSError):
        return False, pid
    return True, pid


def request_elevated_action(action: str, *, timeout_seconds: float = 300.0) -> dict[str, Any]:
    """Request one fixed elevated action and wait for the interactive broker."""
    action = str(action or "").strip()
    if action not in ELEVATED_ACTIONS:
        raise ToolFailure(
            "ELEVATED_ACTION_NOT_ALLOWED",
            "This elevated action is not registered for the private MCP.",
            category="security",
            details={"action": action, "allowed": sorted(ELEVATED_ACTIONS)},
        )
    try:
        timeout = max(1.0, min(float(timeout_seconds), 600.0))
    except (TypeError, ValueError):
        timeout = 300.0
    queue = elevated_queue_path()
    if not queue.is_dir():
        raise ToolFailure(
            "ELEVATION_BROKER_UNAVAILABLE",
            "The interactive elevated-action broker is not installed or running.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue)},
        )
    broker_alive, broker_pid = _broker_is_alive(queue)
    if not broker_alive:
        raise ToolFailure(
            "ELEVATION_BROKER_UNAVAILABLE",
            "The interactive elevated-action broker is installed but not running in the signed-in user session.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue), "broker_pid": broker_pid},
        )

    request_id = secrets.token_urlsafe(18)
    request_path = queue / f"{request_id}.request"
    response_path = queue / f"{request_id}.response"
    payload = {
        "protocol": ELEVATED_PROTOCOL_VERSION,
        "request_id": request_id,
        "action": action,
        "created_at": time.time(),
        "requested_by": os.getpid(),
    }
    try:
        _write_json_atomically(request_path, payload)
    except OSError as exc:
        raise ToolFailure(
            "ELEVATION_QUEUE_UNAVAILABLE",
            "The elevated-action request queue could not be written.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue), "reason": str(exc)},
        ) from exc

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ToolFailure(
                        "ELEVATION_RESPONSE_INVALID",
                        "The elevated-action broker returned an invalid response.",
                        category="runtime",
                        retryable=True,
                        details={"request_id": request_id},
                    ) from exc
                if not isinstance(response, dict) or response.get("request_id") != request_id:
                    raise ToolFailure(
                        "ELEVATION_RESPONSE_INVALID",
                        "The elevated-action broker response did not match the request.",
                        category="security",
                        details={"request_id": request_id},
                    )
                try:
                    response_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if not bool(response.get("ok")):
                    raise ToolFailure(
                        str(response.get("error") or "ELEVATED_ACTION_FAILED"),
                        str(response.get("message") or "The elevated action was not completed."),
                        category="runtime",
                        retryable=bool(response.get("retryable", False)),
                        details={"request_id": request_id, "action": action},
                    )
                return response
            time.sleep(0.25)
    finally:
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass

    raise ToolFailure(
        "ELEVATION_TIMEOUT",
        "Timed out waiting for interactive user approval. Start the elevated broker and try again.",
        category="runtime",
        retryable=True,
        details={
            "request_id": request_id,
            "action": action,
            "timeout_seconds": timeout,
            "broker_pid": broker_pid,
        },
    )


def request_permission_approval(
    *,
    tool_name: str,
    permission: str,
    reason: str,
    arguments: dict[str, Any],
    scope: str,
    ttl_seconds: int,
    timeout_seconds: float = 75.0,
) -> dict[str, Any]:
    """Ask the signed-in user to approve one MCP permission in a desktop dialog."""
    if tool_name not in {"exec_command", "apply_patch"}:
        raise ToolFailure("INVALID_ARGUMENT", "Unsupported permission tool.", category="validation")
    if permission not in MCP_PERMISSION_NAMES:
        raise ToolFailure("INVALID_ARGUMENT", "Unsupported permission name.", category="validation")
    if scope not in {"once", "session"}:
        raise ToolFailure("INVALID_ARGUMENT", "Unsupported permission scope.", category="validation")
    ttl = max(1, min(int(ttl_seconds), 3600))
    timeout = max(5.0, min(float(timeout_seconds), 85.0))
    queue = elevated_queue_path()
    if not queue.is_dir():
        raise ToolFailure(
            "PERMISSION_BROKER_UNAVAILABLE",
            "The interactive permission broker is not installed.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue)},
        )
    broker_alive, broker_pid = _broker_is_alive(queue)
    if not broker_alive:
        raise ToolFailure(
            "PERMISSION_BROKER_UNAVAILABLE",
            "The interactive permission broker is not running in the signed-in desktop session.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue), "broker_pid": broker_pid},
        )

    request_id = secrets.token_urlsafe(18)
    request_path = queue / f"{request_id}.request"
    response_path = queue / f"{request_id}.response"
    payload = {
        "protocol": ELEVATED_PROTOCOL_VERSION,
        "request_id": request_id,
        "kind": "mcp_permission",
        "created_at": time.time(),
        "requested_by": os.getpid(),
        "tool_name": tool_name,
        "permission": permission,
        "reason": str(reason)[:1000],
        "arguments": arguments,
        "scope": scope,
        "ttl_seconds": ttl,
    }
    try:
        _write_json_atomically(request_path, payload)
    except (OSError, TypeError, ValueError) as exc:
        raise ToolFailure(
            "PERMISSION_QUEUE_UNAVAILABLE",
            "The interactive permission request could not be queued.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue), "reason": str(exc)},
        ) from exc

    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ToolFailure(
                        "PERMISSION_RESPONSE_INVALID",
                        "The permission broker returned an invalid response.",
                        category="runtime",
                        retryable=True,
                        details={"request_id": request_id},
                    ) from exc
                try:
                    response_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if not isinstance(response, dict) or response.get("request_id") != request_id:
                    raise ToolFailure(
                        "PERMISSION_RESPONSE_INVALID",
                        "The permission response did not match the request.",
                        category="security",
                        details={"request_id": request_id},
                    )
                if not bool(response.get("ok")):
                    raise ToolFailure(
                        str(response.get("error") or "PERMISSION_BROKER_ERROR"),
                        str(response.get("message") or "Permission approval failed."),
                        category="runtime",
                        retryable=bool(response.get("retryable", False)),
                        details={"request_id": request_id},
                    )
                return response
            time.sleep(0.25)
    finally:
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass

    raise ToolFailure(
        "PERMISSION_APPROVAL_TIMEOUT",
        "Timed out waiting for the signed-in user to answer the permission dialog.",
        category="permission",
        retryable=True,
        details={"request_id": request_id, "timeout_seconds": timeout, "broker_pid": broker_pid},
    )
