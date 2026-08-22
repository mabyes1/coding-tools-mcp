from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


def run_execution_checks(server: Any, runtime_module: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="coding-tools-exec-contract-") as temporary:
        exec_workspace = Path(temporary)
        exec_runtime = server.Runtime(exec_workspace, enable_view_image=False, permission_mode="dangerous")
        original_interactive_exec = runtime_module.request_interactive_exec
        try:
            quick_command = "Write-Output EXEC_CONTRACT" if os.name == "nt" else "printf 'EXEC_CONTRACT\\n'"
            quick = exec_runtime.exec_command(
                {
                    "cmd": quick_command,
                    "yield_time_ms": 30000,
                    "output_mode": "full",
                    "max_output_bytes": 65536,
                }
            )
            if quick.get("status") != "exited" or quick.get("exit_code") != 0:
                raise RuntimeError("managed exec terminal status contract drifted")
            if "EXEC_CONTRACT" not in str(quick.get("stdout") or ""):
                raise RuntimeError("managed exec stdout contract drifted")

            slow_command = (
                "Start-Sleep -Milliseconds 400; Write-Output EXEC_LATE"
                if os.name == "nt"
                else "sleep 0.4; printf 'EXEC_LATE\\n'"
            )
            running = exec_runtime.exec_command(
                {
                    "cmd": slow_command,
                    "yield_time_ms": 0,
                    "output_mode": "delta",
                    "max_output_bytes": 65536,
                }
            )
            if running.get("status") != "running" or not running.get("session_id"):
                raise RuntimeError("managed exec running-session contract drifted")
            next_action = running.get("next_action")
            if not isinstance(next_action, dict) or next_action.get("tool") != "poll_session":
                raise RuntimeError("managed exec running session lost poll continuation")
            exec_runtime.kill_session(
                {
                    "session_id": str(running["session_id"]),
                    "signal": "KILL",
                    "wait_ms": 1000,
                    "kill_wait_ms": 1000,
                    "output_mode": "summary",
                }
            )

            def fake_interactive_exec(**kwargs: object) -> dict[str, object]:
                if kwargs.get("cwd") != str(exec_workspace):
                    raise RuntimeError("active-user broker cwd mapping drifted")
                return {
                    "status": "exited",
                    "exit_code": 0,
                    "timed_out": False,
                    "elapsed_ms": 12,
                    "stdout": "ACTIVE_EXEC\\n",
                    "stderr": "",
                    "execution_identity": {"username": "validator"},
                    "process_id": 4242,
                }

            runtime_module.request_interactive_exec = fake_interactive_exec
            active = exec_runtime._exec_command_active_user(
                cmd="echo active",
                workdir=exec_workspace,
                args={"verbosity": "summary"},
                timeout_ms=1000,
                max_output_bytes=65536,
            )
            if active.get("execution_context") != "active_user" or active.get("managed_session") is not False:
                raise RuntimeError("active-user execution context contract drifted")
            if active.get("polling_supported") is not False or active.get("process_id") != 4242:
                raise RuntimeError("active-user one-shot metadata contract drifted")
            if "stdout" in active or "stderr" in active or "active_user" not in str(active.get("summary") or ""):
                raise RuntimeError("active-user summary formatting contract drifted")

            missing_tool_payload: dict[str, object] = {
                "status": "exited",
                "exit_code": 127,
                "stdout": "",
                "stderr": "definitely-missing-command: command not found",
            }
            exec_runtime._add_exec_diagnostics(missing_tool_payload)
            if missing_tool_payload.get("error_kind") != "tool_not_found":
                raise RuntimeError("exec diagnostic tool-not-found classification drifted")
            process_error = missing_tool_payload.get("process_error")
            if not isinstance(process_error, dict) or process_error.get("exit_code") != 127:
                raise RuntimeError("exec diagnostic process-error metadata drifted")

            timeout_payload: dict[str, object] = {
                "status": "timeout",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "timed_out": True,
            }
            exec_runtime._add_exec_diagnostics(timeout_payload)
            if timeout_payload.get("error_kind") != "timeout":
                raise RuntimeError("exec diagnostic timeout classification drifted")
        finally:
            runtime_module.request_interactive_exec = original_interactive_exec
            exec_runtime.close()
