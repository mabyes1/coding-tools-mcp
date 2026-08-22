from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .session_registry_checks import ExitedProcess


def run_process_control_checks(server: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="coding-tools-control-check-") as temporary:
        control_workspace = Path(temporary)
        session_runtime = server.Runtime(control_workspace, enable_view_image=False)
        kill_process: subprocess.Popen[bytes] | None = None
        cancel_process: subprocess.Popen[bytes] | None = None
        try:
            completed = server.ExecSession("control-completed", ExitedProcess())
            completed.append_stdout(b"done")
            session_runtime.output_sessions[completed.session_id] = completed
            polled = session_runtime.poll_session(
                {
                    "session_id": completed.session_id,
                    "output_mode": "delta",
                    "after_cursor": {"stdout": 0, "stderr": 0},
                    "yield_time_ms": 0,
                }
            )
            if polled.get("status") != "exited" or polled.get("stdout") != "done":
                raise RuntimeError("completed-session poll contract drifted")
            try:
                session_runtime.write_stdin(
                    {
                        "session_id": completed.session_id,
                        "chars": "x",
                    }
                )
            except server.ToolFailure as exc:
                if exc.code != "SESSION_CLOSED" or exc.category != "runtime":
                    raise RuntimeError("closed-session stdin error contract drifted") from exc
            else:
                raise RuntimeError("write_stdin stopped rejecting input to a completed session")

            output_probe = server.ExecSession("control-output-probe", ExitedProcess())
            output_probe.append_stdout(b"abcd")
            if not session_runtime._session_has_new_output(
                output_probe,
                {"after_cursor": {"stdout": 0, "stderr": 0}},
            ):
                raise RuntimeError("session new-output detection missed bytes after explicit cursor")
            if session_runtime._session_has_new_output(
                output_probe,
                {"after_cursor": {"stdout": 4, "stderr": 0}},
            ):
                raise RuntimeError("session new-output detection ignored an up-to-date explicit cursor")

            kill_process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **server.process_group_popen_kwargs(),
            )
            kill_session = server.ExecSession("control-kill", kill_process)
            session_runtime.sessions[kill_session.session_id] = kill_session
            killed = session_runtime.kill_session(
                {
                    "session_id": kill_session.session_id,
                    "signal": "KILL",
                    "wait_ms": 1000,
                    "kill_wait_ms": 1000,
                    "output_mode": "none",
                }
            )
            if killed.get("status") != "killed" or killed.get("signal_sent") != "SIGKILL":
                raise RuntimeError("forced kill-session result contract drifted")
            if killed.get("evicted") is not True or kill_process.poll() is None:
                raise RuntimeError("forced kill-session did not terminate and evict the live child")
            if kill_session.session_id in session_runtime.sessions or kill_session.session_id in session_runtime.output_sessions:
                raise RuntimeError("forced kill-session left the session in a registry map")

            cancel_process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **server.process_group_popen_kwargs(),
            )
            cancelled = server.ExecSession("control-cancel", cancel_process)
            session_runtime.sessions[cancelled.session_id] = cancelled
            session_runtime.cancel_session(cancelled.session_id)
            if cancelled.session_id in session_runtime.sessions:
                raise RuntimeError("cancel_session stopped removing the active session mapping")
        finally:
            session_runtime.close()
            for process in (kill_process, cancel_process):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
