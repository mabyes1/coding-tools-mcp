from __future__ import annotations

import os
import json
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from .errors import ToolFailure
from .textutils import DEFAULT_MAX_LINES, TextTruncation, truncate_text_tail


SESSION_BUFFER_BYTES = 524_288
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def terminate_process_group(
    process: subprocess.Popen[bytes],
    signum: signal.Signals,
    *,
    force: bool = False,
) -> None:
    if not hasattr(os, "killpg"):
        if os.name == "nt" and (force or signum == HARD_KILL_SIGNAL):
            taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
            if taskkill:
                try:
                    subprocess.run(
                        [taskkill, "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        check=False,
                    )
                    process.wait(timeout=2)
                    return
                except (OSError, subprocess.TimeoutExpired):
                    pass
        if os.name == "nt" and not force:
            event = getattr(signal, "CTRL_BREAK_EVENT", None)
            if event is not None:
                try:
                    process.send_signal(event)
                    process.wait(timeout=1)
                    return
                except Exception:
                    pass
        try:
            if force:
                process.kill()
            else:
                process.terminate()
            process.wait(timeout=1)
        except Exception:
            process.kill()
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, HARD_KILL_SIGNAL)
        except Exception:
            process.kill()


def process_tree_for_pid(pid: int) -> list[dict[str, Any]]:
    """Return a bounded process tree rooted at *pid* without psutil."""
    if pid <= 0:
        return []
    rows: list[dict[str, Any]] = []
    if os.name == "nt":
        shell = next(
            (
                candidate
                for candidate in (
                    shutil.which("pwsh.exe"),
                    r"C:\Program Files\PowerShell\7\pwsh.exe",
                    shutil.which("powershell.exe"),
                )
                if candidate and (Path(candidate).is_file() or shutil.which(candidate))
            ),
            None,
        )
        if not shell:
            return []
        script = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
            "ConvertTo-Json -Compress"
        )
        try:
            completed = subprocess.run(
                [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            parsed = json.loads(completed.stdout or "[]")
            if isinstance(parsed, dict):
                parsed = [parsed]
            for row in parsed if isinstance(parsed, list) else []:
                if not isinstance(row, dict):
                    continue
                try:
                    row_pid = int(row.get("ProcessId"))
                    parent_pid = int(row.get("ParentProcessId"))
                except (TypeError, ValueError):
                    continue
                rows.append(
                    {
                        "pid": row_pid,
                        "parent_pid": parent_pid,
                        "name": str(row.get("Name") or ""),
                        "command_line": str(row.get("CommandLine") or "")[:300],
                    }
                )
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, json.JSONDecodeError):
            return []
    else:
        proc_root = "/proc"
        try:
            for name in os.listdir(proc_root):
                if not name.isdigit():
                    continue
                try:
                    item_pid = int(name)
                    status = Path(proc_root, name, "status").read_text(errors="replace")
                    parent_line = next((line for line in status.splitlines() if line.startswith("PPid:")), "")
                    parent_pid = int(parent_line.split()[1]) if parent_line.split() else 0
                    command_line = Path(proc_root, name, "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")[:300]
                    comm = Path(proc_root, name, "comm").read_text(errors="replace").strip()
                    rows.append({"pid": item_pid, "parent_pid": parent_pid, "name": comm, "command_line": command_line})
                except (OSError, ValueError, IndexError):
                    continue
        except OSError:
            return []
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_parent.setdefault(int(row["parent_pid"]), []).append(row)
    selected: list[dict[str, Any]] = []
    pending = [pid]
    seen: set[int] = set()
    while pending and len(selected) < 256:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        row = next((item for item in rows if int(item["pid"]) == current), None)
        if row is not None:
            selected.append(row)
        pending.extend(int(item["pid"]) for item in by_parent.get(current, []))
    return selected


def spawn_process(
    command: Any,
    *,
    cwd: str,
    shell: bool,
    env: dict[str, str],
    tty: bool,
    popen_kwargs: dict[str, Any],
) -> tuple[subprocess.Popen[bytes], int | None]:
    """Spawn a pipe-backed or true POSIX PTY-backed process."""

    if not tty:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            **popen_kwargs,
        )
        return process, None
    if os.name == "nt":
        raise ToolFailure(
            "TTY_UNSUPPORTED",
            "tty=true requires ConPTY support, which is not available in this build.",
            category="runtime",
            details={"platform": os.name, "retry_hint": "Run the command without tty=true."},
        )
    try:
        import pty

        master_fd, slave_fd = pty.openpty()
    except (ImportError, OSError) as exc:
        raise ToolFailure(
            "TTY_UNSUPPORTED",
            "A POSIX pseudo-terminal could not be created.",
            category="runtime",
        ) from exc
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            **popen_kwargs,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return process, master_fd


@dataclass
class ExecSession:
    session_id: str
    process: subprocess.Popen[bytes]
    command_preview: str = ""
    cwd: str = ""
    scratch_dir: str = ""
    timeout_at: float | None = None
    warnings: list[str] = field(default_factory=list)
    stdout: bytearray = field(default_factory=bytearray)
    stderr: bytearray = field(default_factory=bytearray)
    stdout_start_offset: int = 0
    stderr_start_offset: int = 0
    stdout_cursor: int = 0
    stderr_cursor: int = 0
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    stdout_dropped_bytes: int = 0
    stderr_dropped_bytes: int = 0
    buffer_limit: int = SESSION_BUFFER_BYTES
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader_threads: list[threading.Thread] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    closed: bool = False
    exit_code: int | None = None
    signal_name: str | None = None
    timed_out: bool = False
    terminating: bool = False
    pty_master_fd: int | None = None
    _stdin_closed: bool = False

    @property
    def retained_bytes(self) -> int:
        with self.lock:
            return len(self.stdout) + len(self.stderr)

    def append_stdout(self, chunk: bytes) -> None:
        with self.lock:
            self.stdout.extend(chunk)
            self.stdout_total_bytes += len(chunk)
            self.stdout_dropped_bytes += _trim_buffer(
                self.stdout,
                total_bytes=self.stdout_total_bytes,
                start_offset_attr="stdout_start_offset",
                session=self,
            )

    def append_stderr(self, chunk: bytes) -> None:
        with self.lock:
            self.stderr.extend(chunk)
            self.stderr_total_bytes += len(chunk)
            self.stderr_dropped_bytes += _trim_buffer(
                self.stderr,
                total_bytes=self.stderr_total_bytes,
                start_offset_attr="stderr_start_offset",
                session=self,
            )

    def write_input(self, data: bytes) -> None:
        if self._stdin_closed:
            raise ToolFailure("SESSION_CLOSED", "Session stdin is closed.", category="runtime")
        try:
            if self.pty_master_fd is not None:
                os.write(self.pty_master_fd, data)
                return
            if self.process.stdin is None or self.process.stdin.closed:
                raise ToolFailure("SESSION_CLOSED", "Session stdin is closed.", category="runtime")
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ToolFailure("SESSION_CLOSED", "Session stdin is closed.", category="runtime") from exc

    def close_stdin(self) -> None:
        if self.pty_master_fd is not None or self._stdin_closed:
            return
        self._stdin_closed = True
        if self.process.stdin is not None:
            try:
                self.process.stdin.close()
            except OSError:
                pass

    def snapshot_since_cursor(
        self,
        max_output_bytes: int,
        *,
        after_cursor: dict[str, int] | None = None,
        output_mode: str = "delta",
        tail_lines: int = 20,
    ) -> dict[str, Any]:
        """Return output using an explicit, reconnect-safe cursor.

        ``after_cursor`` is deliberately caller-owned: a reconnecting HTTP
        client can resume from the cursor it received before disconnecting,
        without mutating a global cursor shared by another client.  Calls that
        omit it retain the legacy per-process cursor behavior.
        """
        if output_mode not in {"delta", "tail", "none", "summary", "full"}:
            output_mode = "delta"
        tail_lines = max(1, min(int(tail_lines), 1000))
        self.refresh_status()
        with self.lock:
            stdout_omitted = max(0, self.stdout_start_offset - self.stdout_cursor)
            stderr_omitted = max(0, self.stderr_start_offset - self.stderr_cursor)
            explicit_cursor = after_cursor is not None
            after_stdout = max(0, int((after_cursor or {}).get("stdout", self.stdout_cursor)))
            after_stderr = max(0, int((after_cursor or {}).get("stderr", self.stderr_cursor)))
            stdout_omitted = max(0, self.stdout_start_offset - after_stdout)
            stderr_omitted = max(0, self.stderr_start_offset - after_stderr)
            stdout_start = max(0, after_stdout - self.stdout_start_offset)
            stderr_start = max(0, after_stderr - self.stderr_start_offset)
            stdout_delta = bytes(self.stdout[stdout_start:])
            stderr_delta = bytes(self.stderr[stderr_start:])
            stdout_full = bytes(self.stdout)
            stderr_full = bytes(self.stderr)
            stdout_total = self.stdout_total_bytes
            stderr_total = self.stderr_total_bytes
            if not explicit_cursor and output_mode == "delta":
                self.stdout_cursor = self.stdout_total_bytes
                self.stderr_cursor = self.stderr_total_bytes

        if output_mode == "none" or output_mode == "summary":
            stdout_bytes = b""
            stderr_bytes = b""
        elif output_mode == "tail":
            stdout_bytes = stdout_full
            stderr_bytes = stderr_full
        elif output_mode == "full":
            stdout_bytes = stdout_full
            stderr_bytes = stderr_full
        else:
            stdout_bytes = stdout_delta
            stderr_bytes = stderr_delta
        stdout_truncation = truncate_output_bytes_tail(stdout_bytes, max_output_bytes, max_lines=tail_lines)
        stderr_truncation = truncate_output_bytes_tail(stderr_bytes, max_output_bytes, max_lines=tail_lines)
        if self.timed_out:
            status = "timeout"
        elif self.terminating and self.process.poll() is None:
            status = "running"
        elif self.signal_name is not None:
            status = "terminated"
        else:
            status = "running" if self.process.poll() is None else "exited"
        payload: dict[str, Any] = {
            "session_id": self.session_id,
            "status": status,
            "exit_code": self.exit_code,
            "signal": self.signal_name,
            "timed_out": self.timed_out,
            "stdout": stdout_truncation.content,
            "stderr": stderr_truncation.content,
            "stdout_truncated": stdout_truncation.truncated,
            "stderr_truncated": stderr_truncation.truncated,
            "stdout_truncated_by": stdout_truncation.truncated_by,
            "stderr_truncated_by": stderr_truncation.truncated_by,
            "stdout_output_lines": stdout_truncation.output_lines,
            "stderr_output_lines": stderr_truncation.output_lines,
            "stdout_output_bytes": stdout_truncation.output_bytes,
            "stderr_output_bytes": stderr_truncation.output_bytes,
            "stdout_dropped_bytes": self.stdout_dropped_bytes,
            "stderr_dropped_bytes": self.stderr_dropped_bytes,
            "stdout_omitted_bytes": stdout_omitted,
            "stderr_omitted_bytes": stderr_omitted,
            "stdout_total_bytes": stdout_total,
            "stderr_total_bytes": stderr_total,
            "cursor": {"stdout": stdout_total, "stderr": stderr_total},
            "output_mode": output_mode,
            "truncated": (
                stdout_truncation.truncated
                or stderr_truncation.truncated
                or (output_mode == "delta" and (stdout_omitted > 0 or stderr_omitted > 0))
            ),
            "ok": True,
        }
        warnings: list[str] = list(self.warnings)
        if stdout_truncation.truncated:
            warnings.append(f"stdout truncated from tail by {stdout_truncation.truncated_by}")
        if stderr_truncation.truncated:
            warnings.append(f"stderr truncated from tail by {stderr_truncation.truncated_by}")
        if stdout_omitted > 0:
            warnings.append("stdout cursor skipped dropped bytes")
        if stderr_omitted > 0:
            warnings.append("stderr cursor skipped dropped bytes")
        if warnings:
            payload["warnings"] = warnings
        return payload

    def refresh_status(self) -> None:
        if self.timeout_at is not None and not self.timed_out and self.process.poll() is None and time.time() >= self.timeout_at:
            self.timed_out = True
            terminate_process_group(self.process, signal.SIGTERM)
            self.drain_readers()
        code = self.process.poll()
        if code is None:
            return
        self.drain_readers()
        self.exit_code = code
        self.terminating = False
        if code < 0:
            values = {item.value for item in signal.Signals}
            self.signal_name = signal.Signals(-code).name if -code in values else str(-code)
        self.closed = True
        if self.completed_at is None:
            self.completed_at = time.time()

    def drain_readers(self, timeout: float = 0.2) -> None:
        deadline = time.time() + timeout
        for thread in list(self.reader_threads):
            remaining = max(0.0, deadline - time.time())
            if remaining <= 0:
                break
            thread.join(timeout=remaining)

    def retained_output_bytes(self) -> bytes:
        with self.lock:
            stdout = bytes(self.stdout)
            stderr = bytes(self.stderr)
        sections: list[bytes] = []
        if stdout:
            sections.extend([b"--- stdout ---\n", stdout])
        if stderr:
            if sections:
                sections.append(b"\n")
            sections.extend([b"--- stderr ---\n", stderr])
        return b"".join(sections)

    def retained_stream_bytes(self, stream: str) -> tuple[bytes, int, int, int]:
        with self.lock:
            if stream == "stdout":
                return bytes(self.stdout), self.stdout_start_offset, self.stdout_total_bytes, self.stdout_dropped_bytes
            if stream == "stderr":
                return bytes(self.stderr), self.stderr_start_offset, self.stderr_total_bytes, self.stderr_dropped_bytes
        raise ValueError(f"Unknown output stream: {stream}")


def start_reader_threads(session: ExecSession) -> None:
    def reader(stream: BinaryIO, append: Any) -> None:
        try:
            while True:
                chunk = os.read(stream.fileno(), 4096)
                if not chunk:
                    break
                append(chunk)
        except (OSError, ValueError):
            return
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def pty_reader(fd: int) -> None:
        try:
            while True:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                session.append_stdout(chunk)
        except OSError:
            return
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            if session.pty_master_fd == fd:
                session.pty_master_fd = None

    if session.pty_master_fd is not None:
        thread = threading.Thread(target=pty_reader, args=(session.pty_master_fd,), daemon=True)
        session.reader_threads.append(thread)
        thread.start()
        return
    if session.process.stdout is not None:
        thread = threading.Thread(target=reader, args=(session.process.stdout, session.append_stdout), daemon=True)
        session.reader_threads.append(thread)
        thread.start()
    if session.process.stderr is not None:
        thread = threading.Thread(target=reader, args=(session.process.stderr, session.append_stderr), daemon=True)
        session.reader_threads.append(thread)
        thread.start()


def start_session_watchdog(session: ExecSession) -> None:
    if session.timeout_at is None:
        return

    def watchdog() -> None:
        delay = max(0.0, session.timeout_at - time.time()) if session.timeout_at is not None else 0.0
        try:
            session.process.wait(timeout=delay)
        except subprocess.TimeoutExpired:
            pass
        else:
            session.refresh_status()
            return
        if session.process.poll() is not None or session.timed_out:
            return
        session.timed_out = True
        terminate_process_group(session.process, signal.SIGTERM)
        session.refresh_status()

    threading.Thread(
        target=watchdog,
        name=f"coding-tools-watchdog-{session.session_id}",
        daemon=True,
    ).start()


def _trim_buffer(
    buffer: bytearray,
    *,
    total_bytes: int,
    start_offset_attr: str,
    session: ExecSession,
) -> int:
    overflow = len(buffer) - session.buffer_limit
    if overflow <= 0:
        return 0
    del buffer[:overflow]
    setattr(session, start_offset_attr, total_bytes - len(buffer))
    return overflow


def truncate_output_bytes_tail(data: bytes, max_bytes: int, max_lines: int = DEFAULT_MAX_LINES) -> TextTruncation:
    return truncate_text_tail(
        data.decode("utf-8", errors="replace"),
        max_lines=max_lines,
        max_bytes=max_bytes,
    )
