from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import ToolFailure
from .processes import HARD_KILL_SIGNAL, ExecSession, terminate_process_group


MAX_RETAINED_OUTPUT_SESSIONS = 32
COMPLETED_SESSION_TTL_SECONDS = 300
MAX_RUNTIME_OUTPUT_BYTES = 16 * 1024 * 1024


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
