from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .envutils import ENV_PREFIX
from .runtime_support import SENSITIVE_ENV_RE, SENSITIVE_VALUE_RE

ACTIVITY_INLINE_SECRET_RE = re.compile(
    r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key|authorization|cookie|key\s+content)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)"
)

ACTIVITY_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")

ACTIVITY_LONG_VALUE_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{96,}={0,2}(?![A-Za-z0-9+/])")

ACTIVITY_REQUEST_BASE64_RE = re.compile(r"(?i)(--request-base64\s+)\S+")

ACTIVITY_LOG_RETENTION_DAYS = 7

ACTIVITY_LOG_LOCK = threading.Lock()


def _activity_identity_suffix(session_id: Any = None, request_id: Any = None) -> str:
    def short(value: Any, prefix: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_-]", "", str(value or ""))
        return f"{prefix}-{text[:6].upper()}" if text else ""

    fields = [item for item in (short(session_id, "S"), short(request_id, "R")) if item]
    return " {" + ";".join(fields) + "}" if fields else ""

ACTIVITY_LOG_PATH = (
    Path(
        os.environ.get(
            f"{ENV_PREFIX}_ACTIVITY_LOG",
            r"C:\ProgramData\WebGPTCodingToolsMCPService\logs\ai-activity.log",
        )
    )
    if os.name == "nt"
    else None
)

def redact_for_trace(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_ENV_RE.search(str(key)) else redact_for_trace(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_trace(item) for item in value[:50]]
    if isinstance(value, tuple):
        return [redact_for_trace(item) for item in value[:50]]
    if isinstance(value, str):
        if SENSITIVE_VALUE_RE.search(value):
            return "[REDACTED]"
        if len(value) > 240:
            return value[:240] + "...[truncated]"
        return value
    return value

def sanitize_activity_text(value: Any, *, max_chars: int = 1200) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = SENSITIVE_VALUE_RE.sub("[REDACTED]", text)
    text = ACTIVITY_INLINE_SECRET_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = ACTIVITY_BEARER_RE.sub("Bearer [REDACTED]", text)
    text = ACTIVITY_REQUEST_BASE64_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = ACTIVITY_LONG_VALUE_RE.sub("[LONG_VALUE_REDACTED]", text)
    if len(text) > max_chars:
        return text[: max(0, max_chars - 14)] + "...[truncated]"
    return text

def _activity_tail(value: Any, *, max_lines: int = 10, max_chars: int = 1800) -> list[str]:
    text = sanitize_activity_text(value, max_chars=max_chars * 2)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) > max_lines:
        lines = ["... output truncated ...", *lines[-max_lines:]]
    result: list[str] = []
    used = 0
    for line in lines:
        clipped = line if len(line) <= 260 else line[:257] + "..."
        if used + len(clipped) > max_chars:
            result.append("... output truncated ...")
            break
        result.append(clipped)
        used += len(clipped)
    return result

def _activity_log_lines(
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any],
    duration_ms: int,
) -> list[str]:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    ok = bool(payload.get("ok", False))
    marker = "✓" if ok else "✗"
    lines: list[str] = []

    if name == "exec_command":
        context = str(args.get("execution_context") or "service")
        exit_code = payload.get("exit_code")
        status = f"exit {exit_code}" if exit_code is not None else str(payload.get("status") or "done")
        lines.append(f"[{stamp}] {marker} exec_command · {context} · {status} · {duration_ms} ms")
        stdout = payload.get("stdout")
        stderr = payload.get("stderr")
        if stdout:
            lines.extend("  " + item for item in _activity_tail(stdout))
        if stderr:
            lines.append("  [stderr]")
            lines.extend("  " + item for item in _activity_tail(stderr))
        if not stdout and not stderr and payload.get("preview"):
            lines.extend("  " + item for item in _activity_tail(payload.get("preview")))
        return lines

    if name == "apply_patch":
        lines.append(
            f"[{stamp}] {marker} apply_patch · {payload.get('additions', 0)} additions · {payload.get('removals', 0)} removals · {duration_ms} ms"
        )
        affected = payload.get("affected_files")
        if isinstance(affected, list):
            for item in affected[:12]:
                if not isinstance(item, dict):
                    continue
                operation = sanitize_activity_text(item.get("operation", "update"), max_chars=32)
                path = sanitize_activity_text(item.get("path", ""), max_chars=300)
                lines.append(f"  {operation}: {path}")
        return lines

    if name in {"browser_use", "computer_use"}:
        action = sanitize_activity_text(args.get("action", "inspect"), max_chars=80)
        surface = "Browser Use" if name == "browser_use" else "Computer Use"
        lines.append(f"[{stamp}] {marker} {surface} · {action} · {duration_ms} ms")
        window = payload.get("window")
        if isinstance(window, dict):
            title = sanitize_activity_text(window.get("title", ""), max_chars=300)
            if title:
                lines.append("  " + title)
        return lines

    if name == "read_file":
        path = sanitize_activity_text(args.get("path", ""), max_chars=360)
        lines.append(
            f"[{stamp}] {marker} read_file · {path} · lines {payload.get('start_line', '?')}-{payload.get('end_line', '?')} · {duration_ms} ms"
        )
        return lines

    if name == "search_text":
        query = sanitize_activity_text(args.get("query", ""), max_chars=240)
        path = sanitize_activity_text(args.get("path", "."), max_chars=280)
        lines.append(f"[{stamp}] {marker} search_text · {path} · {payload.get('total_matches', 0)} matches · {duration_ms} ms")
        if not ok:
            lines.append(f"  query: {query}")
        return lines

    if name in {"list_files", "list_dir"}:
        path = sanitize_activity_text(args.get("path", "."), max_chars=320)
        count = len(payload.get("files") or payload.get("entries") or [])
        lines.append(f"[{stamp}] {marker} {name} · {path} · {count} items · {duration_ms} ms")
        return lines

    if name.startswith("git_"):
        path = sanitize_activity_text(args.get("path", "."), max_chars=320)
        lines.append(f"[{stamp}] {marker} {name} · {path} · {duration_ms} ms")
        return lines

    if name == "human_help_me":
        reason = sanitize_activity_text(args.get("reason", ""), max_chars=120)
        lines.append(
            f"[{stamp}] {marker} HUMAN HELP · {reason} · {payload.get('status') or payload.get('outcome') or 'done'} · {duration_ms} ms"
        )
        return lines

    status = payload.get("status") or ("ok" if ok else "failed")
    lines.append(f"[{stamp}] {marker} {name} · {sanitize_activity_text(status, max_chars=120)} · {duration_ms} ms")
    return lines

def _activity_start_lines(name: str, args: dict[str, Any]) -> list[str]:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    if name == "exec_command":
        context = sanitize_activity_text(args.get("execution_context") or "service", max_chars=40)
        return [
            f"[{stamp}] ▶ exec_command · {context}",
            "> " + sanitize_activity_text(args.get("cmd", ""), max_chars=700),
        ]
    if name == "apply_patch":
        return [f"[{stamp}] ▶ apply_patch"]
    if name in {"browser_use", "computer_use"}:
        surface = "Browser Use" if name == "browser_use" else "Computer Use"
        action = sanitize_activity_text(args.get("action", "inspect"), max_chars=80)
        return [f"[{stamp}] ▶ {surface} · {action}"]
    if name == "read_file":
        return [f"[{stamp}] ▶ read_file · {sanitize_activity_text(args.get('path', ''), max_chars=360)}"]
    if name == "search_text":
        return [
            f"[{stamp}] ▶ search_text · {sanitize_activity_text(args.get('path', '.'), max_chars=280)}",
            "> " + sanitize_activity_text(args.get("query", ""), max_chars=240),
        ]
    if name in {"list_files", "list_dir"} or name.startswith("git_"):
        return [f"[{stamp}] ▶ {name} · {sanitize_activity_text(args.get('path', '.'), max_chars=320)}"]
    if name == "human_help_me":
        return [f"[{stamp}] ▶ HUMAN HELP · {sanitize_activity_text(args.get('reason', ''), max_chars=120)}"]
    return [f"[{stamp}] ▶ {name}"]

def _prepare_activity_log_for_write() -> None:
    if ACTIVITY_LOG_PATH is None:
        return
    ACTIVITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    if ACTIVITY_LOG_PATH.exists():
        try:
            last_write = datetime.fromtimestamp(ACTIVITY_LOG_PATH.stat().st_mtime).astimezone()
            if last_write.date() != now.date():
                archive = ACTIVITY_LOG_PATH.with_name(
                    f"{ACTIVITY_LOG_PATH.stem}-{last_write.date().isoformat()}{ACTIVITY_LOG_PATH.suffix}"
                )
                if archive.exists():
                    archive = ACTIVITY_LOG_PATH.with_name(
                        f"{ACTIVITY_LOG_PATH.stem}-{last_write.strftime('%Y-%m-%d-%H%M%S')}{ACTIVITY_LOG_PATH.suffix}"
                    )
                os.replace(ACTIVITY_LOG_PATH, archive)
        except OSError:
            pass

    cutoff = time.time() - ACTIVITY_LOG_RETENTION_DAYS * 24 * 60 * 60
    try:
        pattern = f"{ACTIVITY_LOG_PATH.stem}-*{ACTIVITY_LOG_PATH.suffix}"
        for archived in ACTIVITY_LOG_PATH.parent.glob(pattern):
            try:
                if archived.stat().st_mtime < cutoff:
                    archived.unlink()
            except OSError:
                pass
    except OSError:
        pass

def append_activity_start(
    name: str,
    args: dict[str, Any],
    *,
    session_id: Any = None,
    request_id: Any = None,
) -> None:
    if ACTIVITY_LOG_PATH is None:
        return
    try:
        lines = _activity_start_lines(name, args)
        if lines:
            lines[0] += _activity_identity_suffix(session_id, request_id)
        block = "\n".join(lines) + "\n"
        with ACTIVITY_LOG_LOCK:
            _prepare_activity_log_for_write()
            with ACTIVITY_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(block)
    except Exception:
        return

def append_activity_log(
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any],
    duration_ms: int,
    *,
    session_id: Any = None,
    request_id: Any = None,
) -> None:
    if ACTIVITY_LOG_PATH is None:
        return
    try:
        lines = _activity_log_lines(name, args, payload, duration_ms)
        if lines:
            lines[0] += _activity_identity_suffix(session_id, request_id)
        block = "\n".join(lines) + "\n\n"
        with ACTIVITY_LOG_LOCK:
            _prepare_activity_log_for_write()
            with ACTIVITY_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(block)
    except Exception:
        # Activity logging must never break a real MCP operation.
        return
