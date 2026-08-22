from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .processes import HARD_KILL_SIGNAL, ExecSession, terminate_process_group


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


__all__ = ["ExecutionRegistry", "PermissionGrant"]
