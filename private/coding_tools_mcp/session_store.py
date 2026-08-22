from __future__ import annotations

import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ToolFailure
from .processes import (
    HARD_KILL_SIGNAL,
    SESSION_BUFFER_BYTES,
    ExecSession,
    process_tree_for_pid,
    terminate_process_group,
    truncate_output_bytes_tail,
)


EXEC_PREVIEW_BYTES = 4096
MAX_RETAINED_OUTPUT_SESSIONS = 32
COMPLETED_SESSION_TTL_SECONDS = 300
MAX_RUNTIME_OUTPUT_BYTES = 16 * 1024 * 1024


def truncate_bytes(data: bytes, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        limit = 1
    truncated = len(data) > limit
    if truncated:
        marker = b"\n... output truncated ...\n"
        if limit > len(marker) + 2:
            remaining = limit - len(marker)
            head = max(1, remaining // 2)
            tail = max(1, remaining - head)
            data = data[:head] + marker + data[-tail:]
        else:
            data = data[:limit]
    return data.decode("utf-8", errors="replace"), truncated


def read_output_action(output_ref: str, *, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    return {
        "tool": "read_output",
        "arguments": {
            "output_ref": output_ref,
            "offset": offset,
            "limit": EXEC_PREVIEW_BYTES if limit is None else limit,
        },
    }


@dataclass(frozen=True)
class PermissionGrant:
    grant_id: str
    owner: str
    workspace: str
    tool_name: str
    permission: str
    arguments_digest: str
    scope: str
    expires_at: float


class ExecutionRegistry:
    """Process/output registry shared by reconnecting HTTP runtimes."""

    def __init__(self) -> None:
        self.sessions: dict[str, ExecSession] = {}
        self.output_sessions: dict[str, ExecSession] = {}
        self.sessions_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.owner_default_cwds: dict[tuple[str, str], Path] = {}
        self.permission_grants: dict[str, PermissionGrant] = {}
        self.starting_sessions = 0
        self.closed = False
        self.runtime_dir: Path | None = None
        self.fallback_runtime_dir: Path | None = None
        self.http_session_stats_provider: Callable[[], dict[str, int | float]] | None = None

    def close(self) -> None:
        with self.sessions_lock:
            if self.closed:
                return
            self.closed = True
            sessions = list(self.sessions.values())
            self.sessions.clear()
            self.output_sessions.clear()
        for session in sessions:
            session.refresh_status()
            if session.process.poll() is None:
                # Service/runtime shutdown must not leave Chrome, CDP, or other
                # descendants behind.  Windows uses taskkill /T /F here;
                # POSIX still receives the process-group hard kill.
                terminate_process_group(session.process, HARD_KILL_SIGNAL, force=True)
            session.drain_readers()

    def _remember_output_session(self, session: ExecSession) -> None:
        session.refresh_status()
        with self.sessions_lock:
            self.output_sessions.pop(session.session_id, None)
            self.output_sessions[session.session_id] = session
            self._evict_retained_locked()

    def _cleanup_session_scratch(self, session: ExecSession) -> None:
        if session.scratch_dir:
            shutil.rmtree(session.scratch_dir, ignore_errors=True)

    def _retained_output_bytes_locked(self) -> int:
        return sum(session.retained_bytes for session in self.sessions.values()) + sum(
            session.retained_bytes for session in self.output_sessions.values()
        )

    def _evict_retained_locked(self) -> None:
        retained = self._retained_output_bytes_locked()
        while self.output_sessions and (
            len(self.output_sessions) > MAX_RETAINED_OUTPUT_SESSIONS
            or retained > MAX_RUNTIME_OUTPUT_BYTES
        ):
            oldest = self.output_sessions.pop(next(iter(self.output_sessions)))
            retained -= oldest.retained_bytes
            self._cleanup_session_scratch(oldest)

    def _complete_session(self, session: ExecSession) -> None:
        session.refresh_status()
        if session.process.poll() is None:
            return
        with self.sessions_lock:
            self.sessions.pop(session.session_id, None)
        self._remember_output_session(session)

    def _prune_sessions(self) -> None:
        with self.sessions_lock:
            active = list(self.sessions.values())
        for session in active:
            session.refresh_status()
            if session.process.poll() is not None:
                self._complete_session(session)
        cutoff = time.time() - COMPLETED_SESSION_TTL_SECONDS
        with self.sessions_lock:
            expired = [
                session_id
                for session_id, session in self.output_sessions.items()
                if session.completed_at is not None and session.completed_at < cutoff
            ]
            for session_id in expired:
                expired_session = self.output_sessions.pop(session_id, None)
                if expired_session is not None:
                    self._cleanup_session_scratch(expired_session)
            self._evict_retained_locked()

    def _get_output_session(self, session_id: str) -> ExecSession:
        self._prune_sessions()
        with self.sessions_lock:
            session = self.sessions.get(session_id) or self.output_sessions.get(session_id)
        if session is None:
            raise ToolFailure("SESSION_NOT_FOUND", "Output session not found.", category="runtime")
        return session

    def _format_session_output(
        self,
        session: ExecSession,
        payload: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any]:
        terminal = payload.get("status") != "running"
        if terminal:
            self._complete_session(session)
        if payload.get("status") == "running":
            payload["next_action"] = {
                "tool": "poll_session",
                "arguments": {
                    "session_id": session.session_id,
                    "yield_time_ms": 10000,
                    "output_mode": "delta",
                    "after_cursor": payload.get("cursor", {"stdout": 0, "stderr": 0}),
                },
            }
        output_refs = {
            "stdout": f"session:{session.session_id}:stdout",
            "stderr": f"session:{session.session_id}:stderr",
        }
        truncated_streams: list[str] = []
        for stream in ("stdout", "stderr"):
            omitted = payload.get(f"{stream}_omitted_bytes")
            if payload.get(f"{stream}_truncated") or (
                isinstance(omitted, int) and omitted > 0
            ):
                truncated_streams.append(stream)
        output_stream = (
            truncated_streams[0]
            if truncated_streams
            else "stderr"
            if not payload.get("stdout") and payload.get("stderr")
            else "stdout"
        )
        output_ref = output_refs[output_stream]
        truncated = bool(payload.get("truncated"))
        if truncated:
            if not truncated_streams:
                truncated_streams.append(output_stream)
            if terminal:
                self._remember_output_session(session)
            payload["output_ref"] = output_ref
            payload["output_stream"] = output_stream
            payload["output_refs"] = output_refs
            payload["output_truncated"] = True
            payload["truncated_output_streams"] = truncated_streams
            read_actions = [read_output_action(output_refs[stream]) for stream in truncated_streams]
            payload["next_actions"] = read_actions
            if terminal:
                payload["next_action"] = read_actions[0]
        verbosity = str(args.get("verbosity", "")).strip().lower()
        if payload.get("output_mode") == "summary" and not verbosity:
            verbosity = "summary"
        if not verbosity:
            return payload
        if verbosity not in {"summary", "preview", "full"}:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "verbosity must be one of: summary, preview, full.",
                category="validation",
            )
        if terminal and not truncated:
            self._remember_output_session(session)
        payload["summary"] = self._session_output_summary(session, payload)
        payload["output_ref"] = output_ref
        payload["output_stream"] = output_stream
        payload["output_refs"] = output_refs
        if verbosity == "full":
            return payload
        compact = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "stdout",
                "stderr",
                "stdout_truncated",
                "stderr_truncated",
                "stdout_truncated_by",
                "stderr_truncated_by",
                "stdout_output_lines",
                "stderr_output_lines",
                "stdout_output_bytes",
                "stderr_output_bytes",
                "stdout_omitted_bytes",
                "stderr_omitted_bytes",
            }
        }
        if verbosity == "preview":
            preview_limit = int(args.get("preview_bytes", EXEC_PREVIEW_BYTES))
            preview, preview_truncated = truncate_bytes(session.retained_output_bytes(), preview_limit)
            compact["preview"] = preview
            compact["preview_truncated"] = preview_truncated
            compact["truncated"] = bool(compact.get("truncated") or preview_truncated)
            if preview_truncated and not compact.get("truncated_output_streams"):
                preview_streams = [
                    stream
                    for stream in ("stdout", "stderr")
                    if session.retained_stream_bytes(stream)[2] > 0
                ]
                compact["truncated_output_streams"] = preview_streams
                preview_actions = [read_output_action(output_refs[stream]) for stream in preview_streams]
                compact["next_actions"] = preview_actions
                if terminal and preview_actions:
                    compact["next_action"] = preview_actions[0]
        return compact

    def _snapshot_session(
        self,
        session: ExecSession,
        args: dict[str, Any],
        max_output_bytes: int,
    ) -> dict[str, Any]:
        output_mode = str(args.get("output_mode", "delta") or "delta").strip().lower()
        if output_mode not in {"delta", "tail", "none", "summary", "full"}:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "output_mode must be one of: delta, tail, none, summary, full.",
                category="validation",
            )
        raw_cursor = args.get("after_cursor", args.get("cursor"))
        after_cursor: dict[str, int] | None = None
        if raw_cursor is not None:
            if not isinstance(raw_cursor, dict):
                raise ToolFailure("INVALID_ARGUMENT", "after_cursor must be an object.", category="validation")
            try:
                after_cursor = {
                    "stdout": max(0, int(raw_cursor.get("stdout", 0))),
                    "stderr": max(0, int(raw_cursor.get("stderr", 0))),
                }
            except (TypeError, ValueError) as exc:
                raise ToolFailure(
                    "INVALID_ARGUMENT",
                    "after_cursor.stdout and after_cursor.stderr must be integers.",
                    category="validation",
                ) from exc
        try:
            tail_lines = max(1, min(int(args.get("tail_lines", 20)), 1000))
        except (TypeError, ValueError) as exc:
            raise ToolFailure("INVALID_ARGUMENT", "tail_lines must be an integer.", category="validation") from exc
        return session.snapshot_since_cursor(
            max_output_bytes,
            after_cursor=after_cursor,
            output_mode=output_mode,
            tail_lines=tail_lines,
        )

    def _session_output_summary(self, session: ExecSession, payload: dict[str, Any]) -> str:
        retained = session.retained_output_bytes().decode("utf-8", errors="replace")
        lines = retained.splitlines()
        tail = next((line.strip() for line in reversed(lines) if line.strip()), "")
        if len(tail) > 120:
            tail = tail[:117] + "..."
        elapsed = float(payload.get("elapsed_ms") or 0) / 1000.0
        exit_code = payload.get("exit_code")
        status = f"exit {exit_code}" if exit_code is not None else str(payload.get("status", "running"))
        parts = [status, f"{elapsed:.1f}s", f"{len(lines)} lines"]
        if tail:
            parts.append(f"tail: {tail!r}")
        return " | ".join(parts)

    def read_output(self, args: dict[str, Any]) -> dict[str, Any]:
        output_ref = str(args.get("output_ref", ""))
        match = re.fullmatch(r"session:([^:]+):(full|stdout|stderr)", output_ref)
        if not match:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "output_ref must look like session:<id>:stdout or session:<id>:stderr.",
                category="validation",
            )
        session = self._get_output_session(match.group(1))
        session.refresh_status()
        ref_stream = match.group(2)
        requested_stream = str(args.get("stream", "") or "")
        if requested_stream and requested_stream not in {"stdout", "stderr"}:
            raise ToolFailure("INVALID_ARGUMENT", "stream must be stdout or stderr.", category="validation")
        if ref_stream in {"stdout", "stderr"} and requested_stream and requested_stream != ref_stream:
            raise ToolFailure("INVALID_ARGUMENT", "stream does not match output_ref.", category="validation")
        stream = ref_stream if ref_stream in {"stdout", "stderr"} else requested_stream or "stdout"
        data, retained_start_offset, total_stream_bytes, dropped_bytes = session.retained_stream_bytes(stream)
        requested_offset = max(0, int(args.get("offset", 0)))
        offset = max(requested_offset, retained_start_offset)
        limit = max(1, min(int(args.get("limit", EXEC_PREVIEW_BYTES)), SESSION_BUFFER_BYTES))
        buffer_offset = max(0, offset - retained_start_offset)
        chunk = data[buffer_offset : buffer_offset + limit]
        next_offset = offset + len(chunk) if offset + len(chunk) < total_stream_bytes else None
        omitted_bytes = max(0, retained_start_offset - requested_offset)
        warnings: list[str] = []
        if omitted_bytes:
            warnings.append(f"{stream} offset skipped dropped bytes")
        if dropped_bytes:
            warnings.append(f"older {stream} output was dropped from the rolling session buffer")
        if ref_stream == "full":
            warnings.append("legacy full output_ref defaults to stdout; use output_refs for stable stream paging")
        result = {
            "output_ref": output_ref,
            "stream_output_ref": f"session:{session.session_id}:{stream}",
            "stream": stream,
            "offset": offset,
            "requested_offset": requested_offset,
            "limit": limit,
            "content": chunk.decode("utf-8", errors="replace"),
            "next_offset": next_offset,
            "total_retained_bytes": len(data),
            "retained_start_offset": retained_start_offset,
            "total_stream_bytes": total_stream_bytes,
            "stdout_dropped_bytes": session.stdout_dropped_bytes,
            "stderr_dropped_bytes": session.stderr_dropped_bytes,
            "stream_dropped_bytes": dropped_bytes,
            "omitted_bytes": omitted_bytes,
            "truncated": next_offset is not None,
            "ok": True,
            "warnings": warnings,
        }
        if next_offset is not None:
            result["next_action"] = read_output_action(
                str(result["stream_output_ref"]), offset=next_offset, limit=limit
            )
        return result

    def session_metadata(
        self,
        session: ExecSession,
        *,
        include_process_tree: bool = False,
        redact_command: Callable[[Any], Any],
    ) -> dict[str, Any]:
        session.refresh_status()
        with session.lock:
            cursor = {"stdout": session.stdout_total_bytes, "stderr": session.stderr_total_bytes}
            retained = len(session.stdout) + len(session.stderr)
        now = time.time()
        item: dict[str, Any] = {
            "session_id": session.session_id,
            "pid": session.process.pid,
            "status": (
                "timeout"
                if session.timed_out
                else "running"
                if session.process.poll() is None
                else "terminated"
                if session.signal_name
                else "exited"
            ),
            "exit_code": session.exit_code,
            "signal": session.signal_name,
            "started_at": datetime.fromtimestamp(session.started_at, timezone.utc).isoformat(),
            "completed_at": (
                datetime.fromtimestamp(session.completed_at, timezone.utc).isoformat()
                if session.completed_at is not None
                else None
            ),
            "elapsed_ms": int(((session.completed_at or now) - session.started_at) * 1000),
            "timeout_at": session.timeout_at,
            "cwd": session.cwd,
            "scratch_dir": session.scratch_dir or None,
            "command": redact_command(session.command_preview),
            "retained_output_bytes": retained,
            "cursor": cursor,
            "output_refs": {
                "stdout": f"session:{session.session_id}:stdout",
                "stderr": f"session:{session.session_id}:stderr",
            },
        }
        if include_process_tree:
            item["process_tree"] = process_tree_for_pid(session.process.pid)
        return item

    def list_sessions(
        self,
        args: dict[str, Any],
        *,
        redact_command: Callable[[Any], Any],
    ) -> dict[str, Any]:
        self._prune_sessions()
        include_completed = bool(args.get("include_completed", True))
        include_tree = bool(args.get("include_process_tree", False))
        with self.sessions_lock:
            sessions = list(self.sessions.values())
            if include_completed:
                sessions += list(self.output_sessions.values())
        sessions.sort(key=lambda item: item.started_at)
        return {
            "sessions": [
                self.session_metadata(
                    session,
                    include_process_tree=include_tree,
                    redact_command=redact_command,
                )
                for session in sessions
            ],
            "active": sum(1 for session in sessions if session.process.poll() is None),
            "completed": sum(1 for session in sessions if session.process.poll() is not None),
        }

    def process_tree(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._get_output_session(str(args.get("session_id", "")))
        return {
            "session_id": session.session_id,
            "pid": session.process.pid,
            "process_tree": process_tree_for_pid(session.process.pid),
        }

    def kill_tree(self, args: dict[str, Any]) -> dict[str, Any]:
        kill_args = dict(args)
        kill_args["signal"] = "KILL" if bool(args.get("force", True)) else "TERM"
        kill_args.setdefault("output_mode", "summary")
        return self.kill_session(kill_args)

    def tail_output(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._get_output_session(str(args.get("session_id", "")))
        session.refresh_status()
        stream = str(args.get("stream", "stdout"))
        lines = max(1, min(int(args.get("lines", 20)), 1000))
        max_bytes = max(1, min(int(args.get("max_bytes", 16384)), SESSION_BUFFER_BYTES))
        data, start_offset, total_bytes, dropped_bytes = session.retained_stream_bytes(stream)
        truncation = truncate_output_bytes_tail(data, max_bytes, max_lines=lines)
        return {
            "session_id": session.session_id,
            "stream": stream,
            "content": truncation.content,
            "lines": lines,
            "truncated": truncation.truncated,
            "truncated_by": truncation.truncated_by,
            "retained_start_offset": start_offset,
            "total_stream_bytes": total_bytes,
            "dropped_bytes": dropped_bytes,
            "cursor": {"stdout": session.stdout_total_bytes, "stderr": session.stderr_total_bytes},
            "ok": True,
        }

    def find_output(
        self,
        args: dict[str, Any],
        *,
        truncate_line_chars: Callable[[str, int], tuple[str, bool]],
    ) -> dict[str, Any]:
        session = self._get_output_session(str(args.get("session_id", "")))
        session.refresh_status()
        query = str(args.get("query", ""))
        stream = str(args.get("stream", "both"))
        case_sensitive = bool(args.get("case_sensitive", False))
        use_regex = bool(args.get("regex", False))
        max_results = max(1, min(int(args.get("max_results", 100)), 1000))
        streams = [stream] if stream in {"stdout", "stderr"} else ["stdout", "stderr"]
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if use_regex else re.escape(query), flags)
        except re.error as exc:
            raise ToolFailure("INVALID_ARGUMENT", f"Invalid regex: {exc}", category="validation") from exc
        matches: list[dict[str, Any]] = []
        for stream_name in streams:
            data, _start, _total, _dropped = session.retained_stream_bytes(stream_name)
            text = data.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), 1):
                match = pattern.search(line)
                if not match:
                    continue
                matches.append(
                    {
                        "stream": stream_name,
                        "line": line_number,
                        "column": match.start() + 1,
                        "preview": truncate_line_chars(line, 500)[0],
                    }
                )
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break
        return {
            "session_id": session.session_id,
            "query": query,
            "matches": matches,
            "truncated": len(matches) >= max_results,
            "max_results": max_results,
            "ok": True,
        }

    def poll_session(self, args: dict[str, Any]) -> dict[str, Any]:
        """Poll without opening stdin, preserving an explicit reconnect cursor."""
        poll_args = dict(args)
        poll_args["chars"] = ""
        return self.write_stdin(poll_args)

    def write_stdin(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._get_session(session_id)
        session.refresh_status()
        chars = str(args.get("chars", ""))
        if session.process.poll() is not None:
            if chars:
                raise ToolFailure("SESSION_CLOSED", "Session is closed; stdin write blocked.", category="runtime")
            payload = self._snapshot_session(session, args, int(args.get("max_output_bytes", 65536)))
            return self._format_session_output(session, payload, args)
        if chars:
            session.write_input(chars.encode("utf-8"))
        wait_until = time.time() + (int(args.get("yield_time_ms", 10000)) / 1000.0)
        first_output_at: float | None = None
        while time.time() < wait_until and session.process.poll() is None:
            time.sleep(0.02)
            has_new_output = self._session_has_new_output(session, args)
            if has_new_output:
                if not chars:
                    break
                if first_output_at is None:
                    first_output_at = time.time()
                if time.time() - first_output_at >= 0.05:
                    break
        payload = self._snapshot_session(session, args, int(args.get("max_output_bytes", 65536)))
        return self._format_session_output(session, payload, args)

    def _session_has_new_output(self, session: ExecSession, args: dict[str, Any]) -> bool:
        raw_cursor = args.get("after_cursor", args.get("cursor"))
        if isinstance(raw_cursor, dict):
            try:
                stdout_cursor = max(0, int(raw_cursor.get("stdout", 0)))
                stderr_cursor = max(0, int(raw_cursor.get("stderr", 0)))
            except (TypeError, ValueError):
                stdout_cursor = stderr_cursor = 0
        else:
            stdout_cursor = session.stdout_cursor
            stderr_cursor = session.stderr_cursor
        with session.lock:
            return session.stdout_total_bytes > stdout_cursor or session.stderr_total_bytes > stderr_cursor

    def _wait_for_session_exit(self, session: ExecSession, wait_seconds: float) -> bool:
        try:
            session.process.wait(timeout=max(0.0, wait_seconds))
        except subprocess.TimeoutExpired:
            pass
        session.refresh_status()
        session.drain_readers()
        return session.process.poll() is not None

    def kill_session(self, args: dict[str, Any]) -> dict[str, Any]:
        session_id = str(args.get("session_id", ""))
        session = self._get_session(session_id)
        signal_name = str(args.get("signal", "TERM"))
        force = signal_name == "KILL"
        signum = {"TERM": signal.SIGTERM, "KILL": HARD_KILL_SIGNAL, "INT": signal.SIGINT}.get(
            signal_name,
            signal.SIGTERM,
        )
        evict = True
        if session.process.poll() is None:
            session.terminating = True
            terminate_process_group(session.process, signum, force=force)
            exited = self._wait_for_session_exit(session, int(args.get("wait_ms", 5000)) / 1000.0)
            if not exited and not force:
                force = True
                terminate_process_group(session.process, HARD_KILL_SIGNAL, force=True)
                exited = self._wait_for_session_exit(session, int(args.get("kill_wait_ms", 2000)) / 1000.0)
            if exited:
                killed = True
                status = "killed" if force else "terminated"
            else:
                killed = False
                evict = False
                status = "terminating"
        else:
            killed = False
            status = "exited"
        signal_sent = "SIGKILL" if force else signal.Signals(signum).name
        payload = self._snapshot_session(session, args, int(args.get("max_output_bytes", 65536)))
        payload.update({"killed": killed, "status": status, "evicted": evict, "signal_sent": signal_sent})
        payload = self._format_session_output(session, payload, args)
        if status == "terminating":
            warnings = list(payload.get("warnings", []))
            warnings.append("Process did not exit after TERM/SIGKILL; session retained for retry or watchdog cleanup.")
            payload["warnings"] = warnings
            payload["next_action"] = "retry kill_session or wait for watchdog cleanup"
        if evict:
            with self.sessions_lock:
                self.sessions.pop(session_id, None)
                self.output_sessions.pop(session_id, None)
            self._cleanup_session_scratch(session)
        return payload

    def cancel_session(self, session_id: str) -> None:
        with self.sessions_lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return
        session.refresh_status()
        if session.process.poll() is None:
            terminate_process_group(session.process, signal.SIGTERM)

    def _get_session(self, session_id: str) -> ExecSession:
        self._prune_sessions()
        with self.sessions_lock:
            session = self.sessions.get(session_id) or self.output_sessions.get(session_id)
        if session is None:
            raise ToolFailure("SESSION_NOT_FOUND", "Session not found; stdin access denied.", category="not_found")
        return session


__all__ = [
    "COMPLETED_SESSION_TTL_SECONDS",
    "ExecutionRegistry",
    "MAX_RETAINED_OUTPUT_SESSIONS",
    "MAX_RUNTIME_OUTPUT_BYTES",
    "PermissionGrant",
]
