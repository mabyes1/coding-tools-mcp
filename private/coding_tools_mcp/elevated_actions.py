"""Fixed-action bridge from the LocalService MCP process to a user broker.

The MCP process must never accept an arbitrary executable or argument list for
elevation.  It writes a short-lived request containing only a registered action
name; an interactive user-session broker validates that name and presents the
UAC consent prompt before running the fixed script.
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
    try:
        if os.name == "nt":
            # A zero-signal probe is supported on Windows and may raise
            # PermissionError for a live process owned by another identity.
            os.kill(pid, 0)
        else:
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
