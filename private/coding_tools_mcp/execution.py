from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import ToolFailure
from .processes import (
    ExecSession,
    process_group_popen_kwargs,
    spawn_process,
    start_reader_threads,
    start_session_watchdog,
    terminate_process_group,
    truncate_output_bytes_tail,
)
from .session_store import EXEC_PREVIEW_BYTES, ExecutionRegistry, truncate_bytes
from .workspace import ResolvedPath


MAX_ACTIVE_EXEC_SESSIONS = 16


class ExecutionService:
    """Managed and active-user command execution with explicit Runtime hooks."""

    def __init__(
        self,
        *,
        registry: ExecutionRegistry,
        resolve_existing: Callable[[str], ResolvedPath],
        literal_directory_change: Callable[[str, Path], Path | None],
        store_default_cwd: Callable[[Path], dict[str, Any]],
        check_command_policy: Callable[[str, dict[str, Any]], None],
        command_env: Callable[[Any], dict[str, str]],
        interactive_command_env: Callable[[Any], tuple[dict[str, str], dict[str, Any]]],
        ensure_runtime_dirs: Callable[[], None],
        tmp_dir: Callable[[], Path],
        workspace_root: Callable[[], Path],
        landlock_enabled: Callable[[], bool],
        guard_allow_roots: Callable[[], tuple[str, ...]],
        landlock_write_roots: Callable[[], tuple[str, ...]],
        open_landlock_ruleset: Callable[..., int],
        landlock_exec_argv: Callable[[int, str], Any],
        landlock_unavailable_warning: Callable[[ToolFailure], str],
        request_interactive_exec: Callable[..., dict[str, Any]],
        add_exec_diagnostics: Callable[..., None],
        register_request_session: Callable[[str], None],
        runtime_closed: Callable[[], bool],
        env_prefix: str,
    ) -> None:
        self.registry = registry
        self.resolve_existing = resolve_existing
        self.literal_directory_change = literal_directory_change
        self.store_default_cwd = store_default_cwd
        self.check_command_policy = check_command_policy
        self.command_env = command_env
        self.interactive_command_env = interactive_command_env
        self.ensure_runtime_dirs = ensure_runtime_dirs
        self.tmp_dir = tmp_dir
        self.workspace_root = workspace_root
        self.landlock_enabled = landlock_enabled
        self.guard_allow_roots = guard_allow_roots
        self.landlock_write_roots = landlock_write_roots
        self.open_landlock_ruleset = open_landlock_ruleset
        self.landlock_exec_argv = landlock_exec_argv
        self.landlock_unavailable_warning = landlock_unavailable_warning
        self.request_interactive_exec = request_interactive_exec
        self.add_exec_diagnostics = add_exec_diagnostics
        self.register_request_session = register_request_session
        self.runtime_closed = runtime_closed
        self.env_prefix = env_prefix

    def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        self.registry._prune_sessions()
        cmd = str(args.get("cmd", ""))
        if not cmd:
            raise ToolFailure("INVALID_ARGUMENT", "cmd is required.", category="validation")
        execution_context = str(args.get("execution_context", "service") or "service").strip().lower()
        if execution_context not in {"service", "active_user"}:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "execution_context must be one of: service, active_user.",
                category="validation",
            )
        workdir_arg = args.get("workdir", args.get("cwd", "."))
        if "workdir" in args and "cwd" in args and str(args["workdir"]) != str(args["cwd"]):
            raise ToolFailure("INVALID_ARGUMENT", "workdir and cwd refer to different directories.", category="validation")
        workdir = self.resolve_existing(str(workdir_arg))
        if not workdir.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "workdir is not a directory.", category="validation")
        directory_change = self.literal_directory_change(cmd, workdir.path)
        if directory_change is not None:
            cwd_state = self.store_default_cwd(directory_change)
            display = str(cwd_state["default_cwd"])
            return {
                "status": "exited",
                "exit_code": 0,
                "elapsed_ms": 0,
                "stdout": "",
                "stderr": "",
                "cwd": str(directory_change),
                "default_cwd": display,
                "cwd_persisted": True,
                "summary": f"Default cwd changed to {display} and will persist across connector reconnects.",
            }
        self.check_command_policy(cmd, args)
        timeout_ms = int(args.get("timeout_ms", 30000))
        yield_ms = int(args.get("yield_time_ms", 10000))
        max_output_bytes = int(args.get("max_output_bytes", 65536))
        tty = bool(args.get("tty", False))
        stdin_text = str(args.get("stdin", ""))
        if execution_context == "active_user":
            if tty:
                raise ToolFailure(
                    "INTERACTIVE_CONTEXT_ONE_SHOT",
                    "active_user execution is one-shot in this version and does not support tty=true.",
                    category="validation",
                    details={"execution_context": execution_context, "managed_sessions": False},
                )
            if stdin_text:
                raise ToolFailure(
                    "INTERACTIVE_CONTEXT_ONE_SHOT",
                    "active_user execution is one-shot in this version and does not support stdin.",
                    category="validation",
                    details={"execution_context": execution_context, "managed_sessions": False},
                )
            return self.execute_active_user(
                cmd=cmd,
                workdir=workdir.path,
                args=args,
                timeout_ms=timeout_ms,
                max_output_bytes=max_output_bytes,
            )

        env = self.command_env(args.get("env", {}))
        self.ensure_runtime_dirs()
        scratch_dir = self.tmp_dir() / f"session-{secrets.token_urlsafe(12)}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        env["MCP_SESSION_TMP"] = str(scratch_dir)
        env["TMPDIR"] = str(scratch_dir)
        if os.name == "nt":
            env["TEMP"] = str(scratch_dir)
            env["TMP"] = str(scratch_dir)
        start = time.time()
        deadline = start + (timeout_ms / 1000.0)
        landlock_fd: int | None = None
        landlock_warning: str | None = None
        popen_cmd: Any = cmd
        popen_shell = True
        popen_extra = process_group_popen_kwargs()
        if self.landlock_enabled():
            try:
                landlock_fd = self.open_landlock_ruleset(
                    self.workspace_root(),
                    self.guard_allow_roots(),
                    write_roots=self.landlock_write_roots(),
                )
                popen_cmd = self.landlock_exec_argv(landlock_fd, cmd)
                popen_shell = False
                popen_extra["pass_fds"] = (landlock_fd,)
            except ToolFailure as exc:
                if exc.code != "SANDBOX_UNAVAILABLE":
                    raise
                landlock_warning = self.landlock_unavailable_warning(exc)
        if os.name == "nt" and popen_shell:
            configured_pwsh = (os.environ.get(f"{self.env_prefix}_PWSH_PATH") or "").strip()
            candidates = [
                configured_pwsh,
                r"C:\Program Files\PowerShell\7\pwsh.exe",
                "pwsh.exe",
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ]
            pwsh_path = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate and (Path(candidate).is_file() or shutil.which(candidate))
                ),
                None,
            )
            if pwsh_path is None:
                raise ToolFailure(
                    "POWERSHELL_NOT_FOUND",
                    "PowerShell was not found for Windows command execution.",
                    category="runtime",
                    details={"configured_path": configured_pwsh or None},
                )
            popen_cmd = [
                pwsh_path,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                cmd,
            ]
            popen_shell = False
        with self.registry.sessions_lock:
            if self.runtime_closed():
                if landlock_fd is not None:
                    os.close(landlock_fd)
                raise ToolFailure("SESSION_CLOSED", "Runtime is closed.", category="runtime")
            if len(self.registry.sessions) + self.registry.starting_sessions >= MAX_ACTIVE_EXEC_SESSIONS:
                if landlock_fd is not None:
                    os.close(landlock_fd)
                active = len(self.registry.sessions)
                starting = self.registry.starting_sessions
                raise ToolFailure(
                    "SESSION_LIMIT_REACHED",
                    f"Execution session limit reached: {active} running, {starting} starting.",
                    category="runtime",
                    retryable=True,
                    details={
                        "active_sessions": active,
                        "starting_sessions": starting,
                        "max_active_sessions": MAX_ACTIVE_EXEC_SESSIONS,
                        "recovery_hint": "Reuse or stop an existing running session, then retry the command.",
                    },
                )
            self.registry.starting_sessions += 1
        process: subprocess.Popen[bytes] | None = None
        session: ExecSession | None = None
        registered = False
        slot_released = False
        try:
            process, pty_master_fd = spawn_process(
                popen_cmd,
                cwd=str(workdir.path),
                shell=popen_shell,
                env=env,
                tty=tty,
                popen_kwargs=popen_extra,
            )
            session = self.make_session(
                process,
                command_preview=" ".join(cmd.split())[:240],
                cwd=str(workdir.path),
                scratch_dir=str(scratch_dir),
                timeout_at=deadline,
                warnings=[landlock_warning] if landlock_warning else None,
                pty_master_fd=pty_master_fd,
            )
            with self.registry.sessions_lock:
                self.registry.starting_sessions -= 1
                slot_released = True
                if not self.runtime_closed():
                    self.registry.sessions[session.session_id] = session
                    registered = True
            if not registered:
                raise ToolFailure("SESSION_CLOSED", "Runtime closed while the command was starting.", category="runtime")
        except Exception:
            with self.registry.sessions_lock:
                if not registered and not slot_released:
                    self.registry.starting_sessions -= 1
            if process is not None and process.poll() is None:
                terminate_process_group(process, signal.SIGTERM)
            shutil.rmtree(scratch_dir, ignore_errors=True)
            raise
        finally:
            if landlock_fd is not None:
                try:
                    os.close(landlock_fd)
                except OSError:
                    pass
        assert session is not None
        self.register_request_session(session.session_id)
        start_reader_threads(session)
        start_session_watchdog(session)
        try:
            if stdin_text:
                session.write_input(stdin_text.encode("utf-8"))
        except ToolFailure:
            if process.poll() is None:
                raise
        finally:
            if not tty:
                session.close_stdin()
        initial_wait = max(0, min(yield_ms, 30000)) / 1000.0

        def finish() -> dict[str, Any]:
            payload = self.registry._snapshot_session(session, args, max_output_bytes)
            payload["elapsed_ms"] = int((time.time() - start) * 1000)
            self.add_exec_diagnostics(payload, session=session)
            return self.registry._format_session_output(session, payload, args)

        while True:
            if process.poll() is not None:
                session.refresh_status()
                session.drain_readers()
                return finish()
            now = time.time()
            if not tty and now >= deadline:
                session.timed_out = True
                terminate_process_group(process, signal.SIGTERM)
                session.refresh_status()
                session.drain_readers()
                return finish()
            with session.lock:
                tty_has_initial_output = bool(
                    len(session.stdout) > session.stdout_cursor
                    or len(session.stderr) > session.stderr_cursor
                )
            if now - start >= initial_wait or (tty and tty_has_initial_output):
                return finish()
            time.sleep(0.02)

    def execute_active_user(
        self,
        *,
        cmd: str,
        workdir: Path,
        args: dict[str, Any],
        timeout_ms: int,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        started = time.time()
        env_overrides, env_policy = self.interactive_command_env(args.get("env", {}))
        result = self.request_interactive_exec(
            cmd=cmd,
            cwd=str(workdir),
            env_overrides=env_overrides,
            env_policy=env_policy,
            timeout_seconds=timeout_ms / 1000.0,
        )
        stdout_raw = str(result.get("stdout") or "")
        stderr_raw = str(result.get("stderr") or "")
        stdout_view = truncate_output_bytes_tail(stdout_raw.encode("utf-8"), max_output_bytes)
        stderr_view = truncate_output_bytes_tail(stderr_raw.encode("utf-8"), max_output_bytes)
        broker_stdout_truncated = bool(result.get("stdout_truncated"))
        broker_stderr_truncated = bool(result.get("stderr_truncated"))
        stdout_truncated = broker_stdout_truncated or stdout_view.truncated
        stderr_truncated = broker_stderr_truncated or stderr_view.truncated
        stdout_total = int(result.get("stdout_total_bytes") or len(stdout_raw.encode("utf-8")))
        stderr_total = int(result.get("stderr_total_bytes") or len(stderr_raw.encode("utf-8")))
        stdout_bytes = len(stdout_view.content.encode("utf-8"))
        stderr_bytes = len(stderr_view.content.encode("utf-8"))
        payload: dict[str, Any] = {
            "status": str(result.get("status") or "exited"),
            "exit_code": result.get("exit_code"),
            "timed_out": bool(result.get("timed_out")),
            "elapsed_ms": int(result.get("elapsed_ms") or ((time.time() - started) * 1000)),
            "stdout": stdout_view.content,
            "stderr": stderr_view.content,
            "stdout_total_bytes": stdout_total,
            "stderr_total_bytes": stderr_total,
            "stdout_output_bytes": stdout_bytes,
            "stderr_output_bytes": stderr_bytes,
            "stdout_output_lines": len(stdout_view.content.splitlines()),
            "stderr_output_lines": len(stderr_view.content.splitlines()),
            "stdout_omitted_bytes": max(0, stdout_total - stdout_bytes),
            "stderr_omitted_bytes": max(0, stderr_total - stderr_bytes),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "stdout_truncated_by": "bytes" if stdout_truncated else None,
            "stderr_truncated_by": "bytes" if stderr_truncated else None,
            "truncated": stdout_truncated or stderr_truncated,
            "execution_context": "active_user",
            "execution_identity": result.get("execution_identity") or {},
            "process_id": result.get("process_id"),
            "managed_session": False,
            "polling_supported": False,
        }
        self.add_exec_diagnostics(payload)
        if payload["truncated"]:
            payload.setdefault("warnings", []).append(
                "active_user is one-shot; truncated output is not retained for read_output in this version"
            )
        verbosity = str(args.get("verbosity", "")).strip().lower()
        if verbosity and verbosity not in {"summary", "preview", "full"}:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "verbosity must be one of: summary, preview, full.",
                category="validation",
            )
        retained = "\n".join(part for part in (stdout_view.content, stderr_view.content) if part)
        tail = next((line.strip() for line in reversed(retained.splitlines()) if line.strip()), "")
        if len(tail) > 120:
            tail = tail[:117] + "..."
        if verbosity:
            status_text = (
                f"exit {payload['exit_code']}" if payload.get("exit_code") is not None else str(payload["status"])
            )
            summary = [status_text, f"{payload['elapsed_ms'] / 1000.0:.1f}s", "active_user"]
            if tail:
                summary.append(f"tail: {tail!r}")
            payload["summary"] = " | ".join(summary)
        if verbosity == "summary":
            payload.pop("stdout", None)
            payload.pop("stderr", None)
        elif verbosity == "preview":
            preview_limit = int(args.get("preview_bytes", EXEC_PREVIEW_BYTES))
            preview, preview_truncated = truncate_bytes(retained.encode("utf-8"), preview_limit)
            payload["preview"] = preview
            payload["preview_truncated"] = preview_truncated
            payload.pop("stdout", None)
            payload.pop("stderr", None)
        return payload

    @staticmethod
    def make_session(
        process: subprocess.Popen[bytes],
        *,
        command_preview: str = "",
        cwd: str = "",
        scratch_dir: str = "",
        timeout_at: float | None = None,
        warnings: list[str] | None = None,
        pty_master_fd: int | None = None,
    ) -> ExecSession:
        return ExecSession(
            session_id=secrets.token_urlsafe(18),
            process=process,
            command_preview=command_preview,
            cwd=cwd,
            scratch_dir=scratch_dir,
            timeout_at=timeout_at,
            warnings=warnings or [],
            pty_master_fd=pty_master_fd,
        )


__all__ = ["ExecutionService", "MAX_ACTIVE_EXEC_SESSIONS"]
