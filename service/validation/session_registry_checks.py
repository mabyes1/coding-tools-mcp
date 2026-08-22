from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


class ExitedProcess:
    pid = 0

    @staticmethod
    def poll() -> int:
        return 0


def run_session_registry_checks(server: Any) -> None:
    close_registry = server.ExecutionRegistry()
    close_process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **server.process_group_popen_kwargs(),
    )
    close_session = server.ExecSession("registry-close-contract", close_process)
    close_registry.sessions[close_session.session_id] = close_session
    try:
        close_registry.close()
        if not close_registry.closed:
            raise RuntimeError("ExecutionRegistry.close did not mark the registry closed")
        if close_registry.sessions or close_registry.output_sessions:
            raise RuntimeError("ExecutionRegistry.close did not clear session maps")
        if close_process.poll() is None:
            raise RuntimeError("ExecutionRegistry.close did not terminate a live child process")
        close_registry.close()
    finally:
        if close_process.poll() is None:
            close_process.kill()
            close_process.wait(timeout=5)

    with tempfile.TemporaryDirectory(prefix="coding-tools-retention-check-") as temporary:
        retention_workspace = Path(temporary)
        retention_runtime = server.Runtime(retention_workspace, enable_view_image=False)
        try:
            promoted = server.ExecSession("retention-promoted", ExitedProcess())
            retention_runtime.sessions[promoted.session_id] = promoted
            retention_runtime._complete_session(promoted)
            if promoted.session_id in retention_runtime.sessions:
                raise RuntimeError("completed session remained in the active registry")
            if retention_runtime.output_sessions.get(promoted.session_id) is not promoted:
                raise RuntimeError("completed session was not promoted to retained output")

            retention_runtime.output_sessions.clear()
            oldest_scratch = retention_workspace / "scratch-oldest"
            oldest_scratch.mkdir()
            total_to_remember = server.MAX_RETAINED_OUTPUT_SESSIONS + 1
            for index in range(total_to_remember):
                scratch = str(oldest_scratch) if index == 0 else ""
                session = server.ExecSession(
                    f"retention-{index:03d}",
                    ExitedProcess(),
                    scratch_dir=scratch,
                )
                session.stdout.extend(f"output-{index}".encode("utf-8"))
                retention_runtime._remember_output_session(session)
            if len(retention_runtime.output_sessions) != server.MAX_RETAINED_OUTPUT_SESSIONS:
                raise RuntimeError("retained-session count eviction contract drifted")
            if "retention-000" in retention_runtime.output_sessions:
                raise RuntimeError("retained-session eviction stopped removing the oldest session")
            if oldest_scratch.exists():
                raise RuntimeError("retained-session eviction stopped cleaning the oldest scratch directory")

            retention_runtime.output_sessions.clear()
            expired_scratch = retention_workspace / "scratch-expired"
            expired_scratch.mkdir()
            expired = server.ExecSession(
                "retention-expired",
                ExitedProcess(),
                scratch_dir=str(expired_scratch),
            )
            expired.closed = True
            expired.exit_code = 0
            expired.completed_at = time.time() - server.COMPLETED_SESSION_TTL_SECONDS - 1
            retention_runtime.output_sessions[expired.session_id] = expired
            retention_runtime._prune_sessions()
            if expired.session_id in retention_runtime.output_sessions:
                raise RuntimeError("completed-session TTL prune contract drifted")
            if expired_scratch.exists():
                raise RuntimeError("completed-session TTL prune stopped cleaning scratch directories")

            for lookup, expected_category in (
                (retention_runtime._get_output_session, "runtime"),
                (retention_runtime._get_session, "not_found"),
            ):
                try:
                    lookup("retention-missing")
                except server.ToolFailure as exc:
                    if exc.code != "SESSION_NOT_FOUND" or exc.category != expected_category:
                        raise RuntimeError("session lookup missing-error contract drifted") from exc
                else:
                    raise RuntimeError("missing session lookup stopped raising SESSION_NOT_FOUND")
        finally:
            retention_runtime.close()
