"""Bridge one-shot commands into the signed-in Windows user's desktop session.

The MCP server itself runs as LocalService in Session 0.  A separate scheduled
task runs this broker protocol as the signed-in user with RunLevel Limited, so
commands that need the interactive desktop do not silently become elevated.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from .errors import ToolFailure


INTERACTIVE_QUEUE_ENV = "CODING_TOOLS_MCP_INTERACTIVE_QUEUE"
DEFAULT_INTERACTIVE_QUEUE = Path(r"C:\ProgramData\WebGPTCodingToolsMCPService\interactive-requests")
INTERACTIVE_PROTOCOL_VERSION = 1
INTERACTIVE_REQUEST_TTL_SECONDS = 900
BROKER_HEARTBEAT_TTL_SECONDS = 30.0


def interactive_queue_path() -> Path:
    raw = (os.environ.get(INTERACTIVE_QUEUE_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_INTERACTIVE_QUEUE


def _write_json_atomically(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
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
        # Access denied still proves the PID exists.
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
            loaded = json.loads(status_path.read_text(encoding="utf-8"))
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


def request_interactive_exec(
    *,
    cmd: str,
    cwd: str,
    env_overrides: dict[str, str],
    env_policy: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one command through the non-elevated interactive-user broker."""
    if os.name != "nt":
        raise ToolFailure(
            "INTERACTIVE_CONTEXT_UNSUPPORTED",
            "active_user execution is currently supported only on Windows.",
            category="runtime",
        )
    command = str(cmd or "")
    if not command:
        raise ToolFailure("INVALID_ARGUMENT", "cmd is required.", category="validation")
    try:
        timeout = max(1.0, min(float(timeout_seconds), 600.0))
    except (TypeError, ValueError):
        timeout = 30.0

    queue = interactive_queue_path()
    status = interactive_broker_status()
    if not queue.is_dir() or not status.get("available"):
        raise ToolFailure(
            "INTERACTIVE_BROKER_UNAVAILABLE",
            "The non-elevated interactive-user broker is not running in the signed-in desktop session.",
            category="runtime",
            retryable=True,
            details=status,
        )

    request_id = secrets.token_urlsafe(18)
    request_path = queue / f"{request_id}.request"
    response_path = queue / f"{request_id}.response"
    payload = {
        "protocol": INTERACTIVE_PROTOCOL_VERSION,
        "request_id": request_id,
        "kind": "exec",
        "created_at": time.time(),
        "requested_by": os.getpid(),
        "cmd": command,
        "cwd": str(cwd),
        "env_overrides": {str(key): str(value) for key, value in env_overrides.items()},
        "env_policy": env_policy,
        "timeout_ms": int(timeout * 1000),
    }
    try:
        _write_json_atomically(request_path, payload)
    except (OSError, TypeError, ValueError) as exc:
        raise ToolFailure(
            "INTERACTIVE_QUEUE_UNAVAILABLE",
            "The interactive-user execution request could not be queued.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue), "reason": str(exc)},
        ) from exc

    # Give the broker a small envelope after its own process timeout so it can
    # terminate the child, collect redirected output, and write the response.
    deadline = time.monotonic() + timeout + 10.0
    try:
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ToolFailure(
                        "INTERACTIVE_RESPONSE_INVALID",
                        "The interactive-user broker returned an invalid response.",
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
                        "INTERACTIVE_RESPONSE_INVALID",
                        "The interactive-user broker response did not match the request.",
                        category="security",
                        details={"request_id": request_id},
                    )
                if not bool(response.get("ok")):
                    raise ToolFailure(
                        str(response.get("error") or "INTERACTIVE_EXEC_FAILED"),
                        str(response.get("message") or "Interactive-user execution failed."),
                        category="runtime",
                        retryable=bool(response.get("retryable", False)),
                        details={
                            "request_id": request_id,
                            "execution_context": "active_user",
                            "broker": status,
                        },
                    )
                return response
            time.sleep(0.1)
    finally:
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass

    raise ToolFailure(
        "INTERACTIVE_EXEC_TIMEOUT",
        "Timed out waiting for the interactive-user broker to return the command result.",
        category="runtime",
        retryable=True,
        details={
            "request_id": request_id,
            "execution_context": "active_user",
            "timeout_seconds": timeout,
            "broker": status,
        },
    )


def request_human_help(
    *,
    reason: str,
    request: str,
    expected_result: str,
    return_to_agent: str,
    mode: str,
    fallback: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Ask the signed-in human one focused question/action through the desktop broker."""
    if os.name != "nt":
        raise ToolFailure(
            "INTERACTIVE_CONTEXT_UNSUPPORTED",
            "Human-help desktop prompts are currently supported only on Windows.",
            category="runtime",
        )
    try:
        timeout = max(5.0, min(float(timeout_seconds), 300.0))
    except (TypeError, ValueError):
        timeout = 60.0

    queue = interactive_queue_path()
    status = interactive_broker_status()
    if not queue.is_dir() or not status.get("available"):
        raise ToolFailure(
            "INTERACTIVE_BROKER_UNAVAILABLE",
            "The signed-in desktop broker is unavailable for a human-help prompt.",
            category="runtime",
            retryable=True,
            details=status,
        )

    request_id = secrets.token_urlsafe(18)
    request_path = queue / f"{request_id}.request"
    response_path = queue / f"{request_id}.response"
    payload = {
        "protocol": INTERACTIVE_PROTOCOL_VERSION,
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
        "timeout_seconds": int(timeout),
    }
    try:
        _write_json_atomically(request_path, payload)
    except (OSError, TypeError, ValueError) as exc:
        raise ToolFailure(
            "INTERACTIVE_QUEUE_UNAVAILABLE",
            "The human-help request could not be queued.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue), "reason": str(exc)},
        ) from exc

    deadline = time.monotonic() + timeout + 10.0
    try:
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ToolFailure(
                        "INTERACTIVE_RESPONSE_INVALID",
                        "The desktop broker returned an invalid human-help response.",
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
                        "INTERACTIVE_RESPONSE_INVALID",
                        "The human-help response did not match the request.",
                        category="security",
                        details={"request_id": request_id},
                    )
                if not bool(response.get("ok")):
                    raise ToolFailure(
                        str(response.get("error") or "HUMAN_HELP_FAILED"),
                        str(response.get("message") or "Human-help prompt failed."),
                        category="runtime",
                        retryable=bool(response.get("retryable", False)),
                        details={"request_id": request_id, "broker": status},
                    )
                return response
            time.sleep(0.1)
    finally:
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass

    raise ToolFailure(
        "HUMAN_HELP_TIMEOUT",
        "Timed out waiting for the desktop broker to return the human-help result.",
        category="runtime",
        retryable=True,
        details={"request_id": request_id, "timeout_seconds": timeout, "broker": status},
    )


def request_computer_use(
    *,
    action: str,
    window_id: int | None = None,
    title: str = "",
    process_name: str = "",
    x: int | None = None,
    y: int | None = None,
    element_index: int | None = None,
    text: str = "",
    key: str = "",
    scroll_y: int = 0,
    include_screenshot: bool = True,
    include_text: bool = True,
    browser_only: bool = False,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Perform one bounded UI action through the signed-in desktop broker."""
    if os.name != "nt":
        raise ToolFailure(
            "COMPUTER_USE_UNSUPPORTED",
            "Computer Use is currently supported only on Windows.",
            category="runtime",
        )
    try:
        timeout = max(2.0, min(float(timeout_seconds), 60.0))
    except (TypeError, ValueError):
        timeout = 30.0

    queue = interactive_queue_path()
    status = interactive_broker_status()
    if not queue.is_dir() or not status.get("available"):
        raise ToolFailure(
            "INTERACTIVE_BROKER_UNAVAILABLE",
            "The signed-in desktop broker is unavailable for Computer Use.",
            category="runtime",
            retryable=True,
            details=status,
        )

    request_id = secrets.token_urlsafe(18)
    request_path = queue / f"{request_id}.request"
    response_path = queue / f"{request_id}.response"
    payload: dict[str, Any] = {
        "protocol": INTERACTIVE_PROTOCOL_VERSION,
        "request_id": request_id,
        "kind": "computer_use",
        "created_at": time.time(),
        "requested_by": os.getpid(),
        "action": str(action),
        "window_id": window_id,
        "title": str(title),
        "process_name": str(process_name),
        "x": x,
        "y": y,
        "element_index": element_index,
        "text": str(text),
        "key": str(key),
        "scroll_y": int(scroll_y),
        "include_screenshot": bool(include_screenshot),
        "include_text": bool(include_text),
        "browser_only": bool(browser_only),
        "timeout_seconds": int(timeout),
    }
    try:
        _write_json_atomically(request_path, payload)
    except (OSError, TypeError, ValueError) as exc:
        raise ToolFailure(
            "INTERACTIVE_QUEUE_UNAVAILABLE",
            "The Computer Use request could not be queued.",
            category="runtime",
            retryable=True,
            details={"queue": str(queue), "reason": str(exc)},
        ) from exc

    deadline = time.monotonic() + timeout + 5.0
    try:
        while time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8-sig"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ToolFailure(
                        "COMPUTER_USE_RESPONSE_INVALID",
                        "The desktop broker returned an invalid Computer Use response.",
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
                        "COMPUTER_USE_RESPONSE_INVALID",
                        "The Computer Use response did not match the request.",
                        category="security",
                        details={"request_id": request_id},
                    )
                if not bool(response.get("ok")):
                    raise ToolFailure(
                        str(response.get("error") or "COMPUTER_USE_FAILED"),
                        str(response.get("message") or "Computer Use failed."),
                        category="runtime",
                        retryable=bool(response.get("retryable", False)),
                        details={"request_id": request_id, "broker": status},
                    )
                return response
            time.sleep(0.05)
    finally:
        try:
            request_path.unlink(missing_ok=True)
        except OSError:
            pass

    raise ToolFailure(
        "COMPUTER_USE_TIMEOUT",
        "Timed out waiting for the desktop broker to return the Computer Use result.",
        category="runtime",
        retryable=True,
        details={"request_id": request_id, "timeout_seconds": timeout, "broker": status},
    )
