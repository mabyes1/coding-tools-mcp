from __future__ import annotations

"""Repeatable Coding Tools MCP regression test entrypoint.

The existing ``validate-private-source.py`` checks a large set of source
contracts.  This runner gives those checks a stable environment, adds a real
isolated Streamable HTTP smoke test, and prints a small machine-readable
summary suitable for update/build scripts.

The runner deliberately does not invoke Computer Use, Browser Use, UAC, service
restart, update, or rollback against the user's live desktop. Safe chat-only
HUMAN HELP formatting is covered; real delivery and response remain manual.
"""

import argparse
import base64
import compileall
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


class TestFailure(RuntimeError):
    """An expected test failure with a concise user-facing message."""


@dataclass
class TestResult:
    name: str
    status: str
    duration_ms: int
    detail: str = ""


class TestRunner:
    def __init__(self, *, mode: str, package_parent: Path, workspace: Path) -> None:
        self.mode = mode
        self.package_parent = package_parent.resolve()
        self.package_root = self.package_parent / "coding_tools_mcp"
        self.workspace = workspace.resolve()
        self.results: list[TestResult] = []
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.runtime_root: Path | None = None
        self.e2e_workspace: Path | None = None
        self._previous_environment: dict[str, str | None] = {}

    def run(self) -> int:
        if not self.package_root.is_dir():
            self.record(
                "preflight.package",
                lambda: self.fail(f"package root is missing: {self.package_root}"),
            )
            return self.finish()
        self._temporary = tempfile.TemporaryDirectory(
            prefix="coding-tools-mcp-tests-",
            ignore_cleanup_errors=(os.name == "nt"),
        )
        root = Path(self._temporary.name)
        self.runtime_root = root / "runtime"
        self.e2e_workspace = root / "workspace"
        self.e2e_workspace.mkdir(parents=True, exist_ok=True)
        self._configure_environment()
        try:
            self.record("preflight.python", self.check_python_dependencies)
            self.record("source.compile", self.check_compile)
            self.run_source_contracts()
            self.run_core_runtime_smoke()
            self.record("auto.oauth.state_tokens", self.run_oauth_state_smoke)
            if self.mode in {"full", "system"}:
                self.record("auto.oauth.http", self.run_oauth_http_smoke)
                self.record("auto.http.endpoint_controls", self.run_http_endpoint_controls)
                self.record("mcp.http", self.run_http_smoke)
            if self.mode == "system":
                self.record("installed.consistency", self.check_installed_consistency)
                self.record("installed.brokers", self.check_installed_brokers)
            self.add_manual_checks()
        finally:
            self._restore_environment()
            if self._temporary is not None:
                self._temporary.cleanup()
        return self.finish()

    def _configure_environment(self) -> None:
        assert self.runtime_root is not None
        for name in (
            "CODING_TOOLS_MCP_RUNTIME_ROOT",
            "CODING_TOOLS_MCP_WORKSPACE_ALLOWLIST",
            "CODING_TOOLS_MCP_PERMISSION_MODE",
        ):
            self._previous_environment[name] = os.environ.get(name)
        os.environ["CODING_TOOLS_MCP_RUNTIME_ROOT"] = str(self.runtime_root)
        os.environ["CODING_TOOLS_MCP_WORKSPACE_ALLOWLIST"] = f"test={self.e2e_workspace}"
        os.environ["CODING_TOOLS_MCP_PERMISSION_MODE"] = "dangerous"

    def _restore_environment(self) -> None:
        # The runner is normally a short-lived process.  Restoring values makes
        # direct embedding and unit-test invocation safe as well.
        for name, value in self._previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def record(self, name: str, function: Callable[[], None]) -> None:
        started = time.perf_counter()
        try:
            function()
        except TestFailure as exc:
            result = TestResult(name, "FAIL", elapsed_ms(started), str(exc))
        except Exception as exc:  # noqa: BLE001 - report every test failure
            result = TestResult(name, "ERROR", elapsed_ms(started), f"{exc.__class__.__name__}: {exc}")
        else:
            result = TestResult(name, "PASS", elapsed_ms(started))
        self.results.append(result)
        suffix = f" - {result.detail}" if result.detail else ""
        print(f"[{result.status}] {result.name}{suffix}", flush=True)

    @staticmethod
    def fail(message: str) -> None:
        raise TestFailure(message)

    def check_python_dependencies(self) -> None:
        missing: list[str] = []
        for module in ("jwt",):
            try:
                __import__(module)
            except ImportError:
                missing.append(module)
        if missing:
            self.fail(
                "missing Python dependency: "
                + ", ".join(missing)
                + ". Use the service venv or install the project's runtime dependencies."
            )

    def check_compile(self) -> None:
        if not compileall.compile_dir(str(self.package_root), quiet=1, force=False):
            self.fail(f"compileall failed for {self.package_root}")

    def run_source_contracts(self) -> None:
        validator = Path(__file__).with_name("validate-private-source.py")
        if not validator.is_file():
            self.results.append(TestResult("source.runner", "FAIL", 0, f"source validator is missing: {validator}"))
            print(f"[FAIL] source.runner — source validator is missing: {validator}", flush=True)
            return
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.package_parent), str(validator.parent), env.get("PYTHONPATH", "")]
        )
        report_path = (self.e2e_workspace or self.workspace).parent / "source-check-report.json"
        command = [
            sys.executable,
            str(validator),
            "--package-parent",
            str(self.package_parent),
            "--workspace",
            str(self.workspace),
            "--skip-desktop-surfaces",
            "--report-json",
            str(report_path),
        ]
        started = time.perf_counter()
        completed = subprocess.run(
            command,
            cwd=str(validator.parent),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
        if completed.stdout:
            print(completed.stdout.rstrip(), flush=True)
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr, flush=True)
        report: dict[str, Any] | None = None
        if report_path.is_file():
            try:
                loaded = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    report = loaded
            except (OSError, json.JSONDecodeError):
                report = None
        if report is not None and isinstance(report.get("checks"), list):
            for item in report["checks"]:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "source.unknown")
                status = str(item.get("status") or "ERROR")
                detail = str(item.get("detail") or "")
                duration = int(item.get("duration_ms") or 0)
                self.results.append(TestResult(name, status, duration, detail))
        if completed.returncode != 0:
            output = " ".join(completed.stdout.strip().split())
            detail = f"validate-private-source.py exited {completed.returncode}: {tail(output)}"
            self.results.append(TestResult("source.runner", "FAIL", elapsed_ms(started), detail))
            print(f"[FAIL] source.runner — {detail}", flush=True)
        elif "PRIVATE_MCP_SOURCE_CHECK_OK" not in completed.stdout:
            detail = "source validator did not emit PRIVATE_MCP_SOURCE_CHECK_OK"
            self.results.append(TestResult("source.runner", "FAIL", elapsed_ms(started), detail))
            print(f"[FAIL] source.runner — {detail}", flush=True)
        elif report is None:
            detail = "source validator did not produce source-check-report.json"
            self.results.append(TestResult("source.runner", "FAIL", elapsed_ms(started), detail))
            print(f"[FAIL] source.runner — {detail}", flush=True)

    def _import_server(self) -> Any:
        parent = str(self.package_parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        from coding_tools_mcp import server

        return server

    def run_core_runtime_smoke(self) -> None:
        # Keep the original end-to-end smoke as one safety net, then expose
        # the less frequently used internal/compatibility handlers as named
        # checklist rows below.  A failure in one row must not hide the rest.
        self.record("auto.public.core_files_git_exec", self._run_core_runtime_smoke_legacy)
        self.run_extended_runtime_smoke()

    def _run_core_runtime_smoke_legacy(self) -> None:
        assert self.e2e_workspace is not None
        server = self._import_server()
        workspace = self.e2e_workspace
        (workspace / "sample.txt").write_text("alpha\nneedle beta\n", encoding="utf-8")
        (workspace / "nested").mkdir()
        (workspace / "nested" / "child.txt").write_text("child\n", encoding="utf-8")
        image = (
            b"\x89PNG\r\n\x1a\n"
            + b"\x00" * 8
            + (1).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
        )
        (workspace / "pixel.png").write_bytes(image)
        runtime = server.Runtime(workspace, enable_view_image=True, permission_mode="dangerous")
        try:
            self._assert(runtime.initialize({"name": "regression", "version": "1"})["serverInfo"]["name"] == "coding-tools-mcp", "initialize")
            listed = runtime.list_tools()["tools"]
            names = {str(item.get("name")) for item in listed}
            expected = set(server.PUBLIC_TOOL_NAMES)
            self._assert(names == expected, f"tools/list mismatch: expected {sorted(expected)}, got {sorted(names)}")

            self._assert_ok(runtime.call_tool("server_info", {}), "server_info")
            self._assert_ok(runtime.call_tool("check_exec_environment", {"tools": ["git"]}), "check_exec_environment")
            self._assert_ok(runtime.call_tool("get_default_cwd", {}), "get_default_cwd")
            self._assert_ok(runtime.call_tool("set_default_cwd", {"path": "nested"}), "set_default_cwd")
            cwd = runtime.call_tool("get_default_cwd", {})["structuredContent"]
            self._assert(cwd.get("default_cwd") == "nested", f"set_default_cwd returned {cwd}")
            self._assert_ok(runtime.call_tool("set_default_cwd", {"path": "."}), "set_default_cwd reset")

            file_result = runtime.call_tool("read_file", {"path": "sample.txt"})["structuredContent"]
            self._assert(
                str(file_result.get("content", "")).replace("\r\n", "\n") == "alpha\nneedle beta\n",
                "read_file content",
            )
            files = runtime.call_tool("list_files", {"path": ".", "patterns": ["*.txt"]})["structuredContent"]
            self._assert(any(item.get("path") == "sample.txt" for item in files.get("files", [])), "list_files sample.txt")
            matches = runtime.call_tool("search_text", {"query": "needle", "path": "."})["structuredContent"]
            self._assert(matches.get("total_matches", 0) >= 1, "search_text needle")
            image_result = runtime.call_tool("view_image", {"path": "pixel.png"})["structuredContent"]
            self._assert(image_result.get("mime_type") == "image/png", "view_image MIME")
            self._assert(image_result.get("width") == 1 and image_result.get("height") == 1, "view_image dimensions")

            patch = "*** Begin Patch\n*** Update File: sample.txt\n@@\n-alpha\n+alpha changed\n*** End Patch\n"
            patched = runtime.call_tool("apply_patch", {"patch": patch, "intent": "regression patch"})["structuredContent"]
            self._assert_ok_payload(patched, "apply_patch")
            self._assert("alpha changed" in (workspace / "sample.txt").read_text(encoding="utf-8"), "apply_patch file result")

            git = shutil.which("git")
            if git:
                self._prepare_git_repo(workspace, git)
                self._assert_ok(runtime.call_tool("git_status", {"path": "."}), "git_status")
                self._assert_ok(runtime.call_tool("git_diff", {"path": "."}), "git_diff")
                self._assert_ok(runtime.call_tool("git_log", {"path": ".", "max_count": 5}), "git_log")
            else:
                print("[SKIP] core.runtime git smoke — git executable not available", flush=True)

            command = self._python_command("print('coding-tools-e2e')")
            executed = runtime.call_tool(
                "exec_command",
                {"cmd": command, "intent": "runtime regression smoke", "yield_time_ms": 30000},
            )["structuredContent"]
            self._assert(executed.get("exit_code") == 0, f"exec_command result: {executed}")
            self._assert("coding-tools-e2e" in str(executed.get("stdout", "")), "exec_command stdout")
        finally:
            runtime.close()

    def run_extended_runtime_smoke(self) -> None:
        """Exercise safe public edges and compatibility handlers one by one."""

        assert self.e2e_workspace is not None
        server = self._import_server()
        root = self.e2e_workspace.parent
        workspace = root / "extended-workspace"
        other_workspace = root / "switch-target"
        workspace.mkdir(parents=True, exist_ok=True)
        other_workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "extended.txt").write_text("extended output\nneedle extended\n", encoding="utf-8")
        (workspace / "sample.txt").write_text("alpha\nneedle beta\n", encoding="utf-8")
        (workspace / "nested").mkdir(exist_ok=True)
        (workspace / "nested" / "child.txt").write_text("child\n", encoding="utf-8")
        (other_workspace / "other.txt").write_text("other workspace\n", encoding="utf-8")

        allowlist_name = "CODING_TOOLS_MCP_WORKSPACE_ALLOWLIST"
        previous_allowlist = os.environ.get(allowlist_name)
        os.environ[allowlist_name] = (
            f"test={workspace}{os.pathsep}other={other_workspace}"
        )
        runtime = server.Runtime(workspace, enable_view_image=False, permission_mode="dangerous")
        session_box: dict[str, Any] = {}
        try:
            runtime.initialize({"name": "extended-regression", "version": "1"})

            def check(name: str, callback: Callable[[], None]) -> None:
                self.record(f"auto.runtime.{name}", callback)

            def check_which_tools() -> None:
                payload = runtime.which_tools({"tools": ["git", "coding-tools-definitely-missing"]})
                rows = payload.get("tools", [])
                self._assert(len(rows) == 2, f"which_tools returned {payload}")
                by_name = {str(row.get("name")): row for row in rows}
                self._assert(bool(by_name.get("git", {}).get("available")), f"git discovery: {payload}")
                self._assert(by_name.get("coding-tools-definitely-missing", {}).get("available") is False, f"missing discovery: {payload}")

            check("which_tools", check_which_tools)

            def check_list_workspaces() -> None:
                payload = runtime.list_workspaces({})
                names = [str(item.get("name")) for item in payload.get("workspaces", [])]
                self._assert(names == ["test", "other"], f"list_workspaces: {payload}")
                self._assert(any(item.get("active") for item in payload.get("workspaces", [])), f"active workspace: {payload}")

            check("list_workspaces", check_list_workspaces)

            def check_list_dir() -> None:
                payload = runtime.list_dir({"path": ".", "recursive": True, "max_depth": 2})
                paths = {str(item.get("path")) for item in payload.get("entries", [])}
                self._assert("extended.txt" in paths and "nested/child.txt" in paths, f"list_dir: {payload}")

            check("list_dir", check_list_dir)

            def check_switch_workspace() -> None:
                switched = runtime.switch_workspace({"workspace": "other"})
                self._assert(switched.get("name") == "other", f"switch_workspace: {switched}")
                self._assert(runtime.workspace.root == other_workspace.resolve(), f"switch target: {runtime.workspace.root}")
                restored = runtime.switch_workspace({"workspace": "test"})
                self._assert(restored.get("name") == "test", f"switch restore: {restored}")
                self._assert(runtime.workspace.root == workspace.resolve(), f"switch restore target: {runtime.workspace.root}")

            check("switch_workspace", check_switch_workspace)

            def check_human_help_chat_only() -> None:
                result = runtime.call_tool(
                    "human_help_me",
                    {
                        "reason": "need_information",
                        "request": "Return the visible diagnostic result.",
                        "delivery": "chat_only",
                    },
                )
                payload = result.get("structuredContent", {})
                self._assert(result.get("isError") is False and payload.get("status") == "human_action_required", f"human_help_me: {result}")
                self._assert(payload.get("visibility") == "must_surface_to_user", f"human_help visibility: {payload}")

            check("human_help_chat_only", check_human_help_chat_only)

            def check_permission_schema_boundary() -> None:
                try:
                    server.validate_arguments("request_permissions", {"tool_name": "exec_command"})
                except server.JsonRpcError as exc:
                    self._assert(exc.code == -32602, f"request_permissions validation code: {exc.code}")
                else:
                    self.fail("request_permissions accepted an incomplete approval request")

            check("request_permissions_schema", check_permission_schema_boundary)

            git = shutil.which("git")
            if git:
                self._prepare_git_repo(workspace, git)

                def check_git_show() -> None:
                    payload = runtime.git_show({"rev": "HEAD", "path": "sample.txt", "include_diff": True})
                    self._assert(payload.get("is_repo") is True, f"git_show repo: {payload}")
                    self._assert("initial test commit" in str(payload.get("content") or ""), f"git_show content: {payload}")

                check("git_show", check_git_show)

                def check_git_blame() -> None:
                    payload = runtime.git_blame({"path": "sample.txt", "start_line": 1, "max_lines": 5})
                    lines = payload.get("lines", [])
                    self._assert(payload.get("is_repo") is True and len(lines) >= 1, f"git_blame: {payload}")
                    self._assert(all(item.get("commit") for item in lines), f"git_blame commits: {payload}")

                check("git_blame", check_git_blame)
            else:
                print("[SKIP] auto.runtime.git_show — git executable not available", flush=True)
                print("[SKIP] auto.runtime.git_blame — git executable not available", flush=True)

            def start_output_session() -> None:
                result = runtime.call_tool(
                    "exec_command",
                    {
                        "cmd": self._long_running_command("extended-output", 2),
                        "intent": "runtime session checklist",
                        "yield_time_ms": 0,
                    },
                )
                payload = result.get("structuredContent", {})
                session_id = str(payload.get("session_id") or "")
                self._assert(session_id, f"session start: {result}")
                session_box["id"] = session_id
                session_box["payload"] = payload

            check("session_start", start_output_session)

            def require_session() -> str:
                session_id = str(session_box.get("id") or "")
                self._assert(bool(session_id), "session was not created")
                return session_id

            def check_list_sessions() -> None:
                session_id = require_session()
                payload = runtime.list_sessions({"include_completed": True, "include_process_tree": True})
                rows = payload.get("sessions", [])
                self._assert(any(str(row.get("session_id")) == session_id for row in rows), f"list_sessions: {payload}")

            check("list_sessions", check_list_sessions)

            def check_process_tree() -> None:
                session_id = require_session()
                payload = runtime.process_tree({"session_id": session_id})
                self._assert(payload.get("session_id") == session_id and isinstance(payload.get("process_tree"), list), f"process_tree: {payload}")

            check("process_tree", check_process_tree)

            def check_write_stdin() -> None:
                session_id = require_session()
                result = runtime.call_tool(
                    "write_stdin",
                    {"session_id": session_id, "chars": "", "yield_time_ms": 100},
                )
                self._assert(result.get("isError") is False, f"write_stdin: {result}")

            check("write_stdin", check_write_stdin)

            def check_poll_session() -> None:
                session_id = require_session()
                payload: dict[str, Any] = {}
                for _ in range(8):
                    payload = runtime.poll_session({"session_id": session_id, "yield_time_ms": 600})
                    if payload.get("status") != "running":
                        break
                self._assert(payload.get("status") in {"exited", "terminated", "killed"}, f"poll_session: {payload}")
                session_box["payload"] = payload

            check("poll_session", check_poll_session)

            def check_tail_output() -> None:
                session_id = require_session()
                payload = runtime.tail_output({"session_id": session_id, "stream": "stdout", "lines": 5})
                self._assert("extended-output" in str(payload.get("content") or ""), f"tail_output: {payload}")

            check("tail_output", check_tail_output)

            def check_find_output() -> None:
                session_id = require_session()
                payload = runtime.find_output({"session_id": session_id, "query": "extended-output", "stream": "stdout"})
                self._assert(len(payload.get("matches", [])) >= 1, f"find_output: {payload}")

            check("find_output", check_find_output)

            def check_read_output() -> None:
                session_id = require_session()
                output_ref = str(session_box.get("payload", {}).get("output_refs", {}).get("stdout") or f"session:{session_id}:stdout")
                result = runtime.call_tool("read_output", {"output_ref": output_ref, "offset": 0, "limit": 4096})
                payload = result.get("structuredContent", {})
                self._assert(result.get("isError") is False and "extended-output" in str(payload.get("content") or ""), f"read_output: {result}")

            check("read_output", check_read_output)

            def check_kill_session() -> None:
                result = runtime.call_tool(
                    "exec_command",
                    {
                        "cmd": self._long_running_command("kill-session", 10),
                        "intent": "kill session checklist",
                        "yield_time_ms": 0,
                    },
                )
                payload = result.get("structuredContent", {})
                session_id = str(payload.get("session_id") or "")
                self._assert(session_id, f"kill session start: {result}")
                killed = runtime.call_tool(
                    "kill_session",
                    {"session_id": session_id, "signal": "KILL", "wait_ms": 3000},
                ).get("structuredContent", {})
                self._assert(killed.get("status") in {"killed", "terminated", "exited"}, f"kill_session: {killed}")

            check("kill_session", check_kill_session)

            def check_kill_tree() -> None:
                payload = runtime.exec_command(
                    {
                        "cmd": self._long_running_command("kill-tree", 10),
                        "intent": "kill process tree checklist",
                        "yield_time_ms": 0,
                    }
                )
                session_id = str(payload.get("session_id") or "")
                self._assert(session_id, f"kill tree start: {payload}")
                killed = runtime.kill_tree({"session_id": session_id, "force": True, "wait_ms": 3000})
                self._assert(killed.get("status") in {"killed", "terminated", "exited"}, f"kill_tree: {killed}")

            check("kill_tree", check_kill_tree)
        finally:
            runtime.close()
            if previous_allowlist is None:
                os.environ.pop(allowlist_name, None)
            else:
                os.environ[allowlist_name] = previous_allowlist

    def _prepare_git_repo(self, workspace: Path, git: str) -> None:
        run = lambda args: subprocess.run(
            [git, *args], cwd=str(workspace), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        run(["init", "-q"])
        run(["config", "user.email", "coding-tools-tests@example.invalid"])
        run(["config", "user.name", "Coding Tools Tests"])
        run(["add", "sample.txt", "nested/child.txt"])
        run(["commit", "-q", "-m", "initial test commit"])

    def run_oauth_state_smoke(self) -> None:
        """Exercise OAuth persistence, PKCE, JWT validation, and refresh rotation."""

        server = self._import_server()
        with tempfile.TemporaryDirectory(prefix="coding-tools-oauth-state-") as temporary:
            state_path = Path(temporary) / "oauth-state.sqlite"
            store = server.OAuthStateStore(state_path)
            registry = server.OAuthClientRegistry(store)
            config = server.OAuthConfig(
                password="acceptance-password",
                server_url="https://mcp.example.invalid",
                token_secret=b"t" * 32,
                state_store=store,
                registry=registry,
            )
            try:
                registered = registry.register(
                    {
                        "client_name": "acceptance",
                        "redirect_uris": ["http://127.0.0.1/callback"],
                        "token_endpoint_auth_method": "none",
                    }
                )
                client_id = str(registered.get("client_id") or "")
                self._assert(client_id and registered.get("redirect_uris") == ["http://127.0.0.1/callback"], f"OAuth register: {registered}")
                reloaded = server.OAuthClientRegistry(store)
                self._assert(reloaded.get(client_id) is not None, "OAuth client persistence")

                try:
                    registry.register({"redirect_uris": ["http://example.invalid/callback"]})
                except ValueError:
                    pass
                else:
                    self.fail("OAuth accepted a non-loopback HTTP redirect URI")

                verifier = "V" * 43
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
                self._assert(server.valid_pkce_challenge(challenge), "PKCE challenge format")
                self._assert(server.verify_pkce(verifier, challenge), "PKCE verification")
                code = "authorization-code-for-test"
                config.put_pending_code(
                    code,
                    {
                        "code_challenge": challenge,
                        "client_id": client_id,
                        "redirect_uri": "http://127.0.0.1/callback",
                        "state": "state",
                        "expires_at": time.time() + 60,
                        "server_url": "https://mcp.example.invalid",
                        "resource": "https://mcp.example.invalid/mcp",
                    },
                )
                consumed = config.consume_pending_code(code)
                self._assert(consumed is not None and config.consume_pending_code(code) is None, "OAuth authorization-code one-time use")

                token = server.create_access_token(
                    config,
                    "https://mcp.example.invalid",
                    client_id=client_id,
                    audience="https://mcp.example.invalid/mcp",
                )
                self._assert(
                    server.validate_access_token(
                        token,
                        config,
                        "https://mcp.example.invalid",
                        audience="https://mcp.example.invalid/mcp",
                    ),
                    "OAuth access-token validation",
                )
                self._assert(
                    not server.validate_access_token(
                        token + "invalid",
                        config,
                        "https://mcp.example.invalid",
                        audience="https://mcp.example.invalid/mcp",
                    ),
                    "OAuth invalid-token rejection",
                )

                refresh, _expires_at, family = store.issue_refresh_token(client_id, ttl=120)
                consumed_refresh, consumed_family = store.consume_refresh_token(refresh, client_id)
                self._assert(consumed_refresh and consumed_family == family, "OAuth refresh-token consume")
                reused, _ = store.consume_refresh_token(refresh, client_id)
                self._assert(not reused, "OAuth refresh-token replay rejection")
            finally:
                config.close()

    def run_oauth_http_smoke(self) -> None:
        """Run an isolated OAuth HTTP authorization-code + refresh flow."""

        assert self.e2e_workspace is not None
        package = self.package_parent
        port = free_port()
        health_port = free_port({port})
        state_path = (self.e2e_workspace.parent / "oauth-http-state.sqlite").resolve()
        server_url = f"http://127.0.0.1:{port}"
        redirect_uri = "http://127.0.0.1/callback"
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(package),
                "CODING_TOOLS_MCP_RUNTIME_ROOT": str(self.runtime_root),
                "CODING_TOOLS_MCP_HEALTH_PORT": str(health_port),
                "CODING_TOOLS_MCP_PERMISSION_MODE": "dangerous",
                "CODING_TOOLS_MCP_TELEMETRY": "off",
                "CODING_TOOLS_MCP_SERVER_URL": server_url,
                "CODING_TOOLS_MCP_OAUTH_PASSWORD": "acceptance-password",
                "CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET": "6f" * 32,
                "CODING_TOOLS_MCP_OAUTH_STATE_PATH": str(state_path),
                "CODING_TOOLS_MCP_OAUTH_ALLOW_DYNAMIC_REGISTRATION": "1",
            }
        )
        command = [
            sys.executable,
            "-m",
            "coding_tools_mcp",
            "--workspace",
            str(self.e2e_workspace),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--permission-mode",
            "dangerous",
            "--oauth-mode",
        ]
        process = subprocess.Popen(
            command,
            cwd=str(self.package_parent.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        try:
            wait_http(health_port, "/healthz", timeout=20)
            authorization_metadata = http_json("GET", port, "/.well-known/oauth-authorization-server")
            self._assert(authorization_metadata.get("issuer") == server_url, f"OAuth AS metadata: {authorization_metadata}")
            self._assert(authorization_metadata.get("code_challenge_methods_supported") == ["S256"], "OAuth PKCE metadata")
            resource_metadata = http_json("GET", port, "/.well-known/oauth-protected-resource")
            self._assert(resource_metadata.get("resource") == f"{server_url}/mcp", f"OAuth resource metadata: {resource_metadata}")

            unauth_status, unauth_headers, _unauth_body = self._http_raw(
                "POST",
                port,
                "/mcp",
                headers={"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"},
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}),
            )
            self._assert(unauth_status == 401 and "Bearer" in str(unauth_headers.get("WWW-Authenticate") or ""), f"OAuth unauthorized MCP: status={unauth_status}, headers={unauth_headers}")

            register_status, _register_headers, register_body = self._http_raw(
                "POST",
                port,
                "/oauth/register",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"client_name": "HTTP acceptance", "redirect_uris": [redirect_uri], "token_endpoint_auth_method": "none"}),
            )
            self._assert(register_status == 201, f"OAuth dynamic registration status={register_status}: {register_body[:500]!r}")
            registered = json.loads(register_body.decode("utf-8"))
            client_id = str(registered.get("client_id") or "")
            self._assert(client_id, f"OAuth dynamic registration payload: {registered}")

            verifier = "W" * 43
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
            resource = f"{server_url}/mcp"
            auth_query = urllib.parse.urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "acceptance-state",
                    "resource": resource,
                }
            )
            auth_status, _auth_headers, auth_body = self._http_raw("GET", port, "/oauth/authorize?" + auth_query)
            self._assert(auth_status == 200 and b"Authorize Coding Tools MCP" in auth_body, "OAuth authorize login page")

            form = urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "acceptance-state",
                    "resource": resource,
                    "password": "wrong-password",
                }
            )
            wrong_status, _wrong_headers, _wrong_body = self._http_raw(
                "POST", port, "/oauth/authorize", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=form
            )
            self._assert(wrong_status == 401, f"OAuth wrong-password status={wrong_status}")
            form = urllib.parse.urlencode({**urllib.parse.parse_qs(form), "password": ["acceptance-password"]}, doseq=True)
            good_status, good_headers, _good_body = self._http_raw(
                "POST", port, "/oauth/authorize", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=form
            )
            self._assert(good_status == 302, f"OAuth authorize status={good_status}: {good_headers}")
            location = str(good_headers.get("Location") or good_headers.get("location") or "")
            code_values = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("code", [])
            self._assert(bool(code_values), f"OAuth authorization redirect: {location}")
            code = str(code_values[0])

            token_form = urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": verifier,
                    "client_id": client_id,
                    "resource": resource,
                }
            )
            token_status, _token_headers, token_body = self._http_raw(
                "POST", port, "/oauth/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=token_form
            )
            self._assert(token_status == 200, f"OAuth token status={token_status}: {token_body[:500]!r}")
            token_payload = json.loads(token_body.decode("utf-8"))
            access_token = str(token_payload.get("access_token") or "")
            refresh_token = str(token_payload.get("refresh_token") or "")
            self._assert(access_token and refresh_token, f"OAuth token payload: {token_payload}")

            init, init_headers = self._http_rpc(
                port,
                {
                    "jsonrpc": "2.0",
                    "id": "oauth-init",
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "oauth-acceptance", "version": "1"}},
                },
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                    "Authorization": f"Bearer {access_token}",
                },
            )
            self._assert("error" not in init, f"OAuth bearer initialize: {init}")
            self._assert(str(init_headers.get("Mcp-Session-Id") or init_headers.get("mcp-session-id") or ""), "OAuth bearer session header")

            refresh_form = urllib.parse.urlencode(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "resource": resource,
                }
            )
            refresh_status, _refresh_headers, refresh_body = self._http_raw(
                "POST", port, "/oauth/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=refresh_form
            )
            self._assert(refresh_status == 200 and json.loads(refresh_body.decode("utf-8")).get("access_token"), f"OAuth refresh status={refresh_status}: {refresh_body[:500]!r}")
            replay_status, _replay_headers, replay_body = self._http_raw(
                "POST", port, "/oauth/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=refresh_form
            )
            replay_payload = json.loads(replay_body.decode("utf-8"))
            self._assert(replay_status == 400 and replay_payload.get("error") == "invalid_grant", f"OAuth refresh replay: status={replay_status}, payload={replay_payload}")
        finally:
            terminate_process(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def run_http_smoke(self) -> None:
        assert self.e2e_workspace is not None
        package = self.package_parent
        port = free_port()
        health_port = free_port({port})
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(package),
                "CODING_TOOLS_MCP_RUNTIME_ROOT": str(self.runtime_root),
                "CODING_TOOLS_MCP_HEALTH_PORT": str(health_port),
                "CODING_TOOLS_MCP_PERMISSION_MODE": "dangerous",
                "CODING_TOOLS_MCP_TELEMETRY": "off",
            }
        )
        command = [
            sys.executable,
            "-m",
            "coding_tools_mcp",
            "--workspace",
            str(self.e2e_workspace),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--permission-mode",
            "dangerous",
        ]
        process = subprocess.Popen(
            command,
            cwd=str(self.package_parent.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        active_session = ""
        mcp_headers: dict[str, str] | None = None
        try:
            wait_http(health_port, "/healthz", timeout=20)
            health = http_json("GET", health_port, "/healthz")
            self._assert(health.get("status") == "ok", f"healthz: {health}")
            self._assert(int(health.get("mcp", {}).get("port", 0)) == port, f"health MCP port: {health}")

            discovered = http_json("GET", port, "/.well-known/mcp/server-card.json")
            self._assert(discovered.get("tools", {}).get("count") == 20, "server card tool count")
            self._assert(discovered.get("transport", {}).get("endpoint") == "/mcp", "server card endpoint")

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            }
            initialize = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "regression", "version": "1"}}},
                headers,
            )
            self._assert("error" not in initialize[0], f"initialize response: {initialize[0]}")
            session_id = initialize[1].get("Mcp-Session-Id")
            self._assert(bool(session_id), "initialize session header")
            headers["Mcp-Session-Id"] = str(session_id)
            mcp_headers = headers

            listed = self._http_rpc(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers)[0]
            self._assert(len(listed.get("result", {}).get("tools", [])) == 20, "HTTP tools/list")
            info = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "server_info", "arguments": {}}},
                headers,
            )[0]
            self._assert(info.get("result", {}).get("structuredContent", {}).get("ok") is True, "HTTP server_info")

            unknown = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "removed_tool", "arguments": {}}},
                headers,
            )[0]
            self._assert(
                unknown.get("error", {}).get("code") == -32602,
                f"unknown tool JSON-RPC error: {unknown}",
            )

            read = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "read_file", "arguments": {"path": "sample.txt"}}},
                headers,
            )[0]
            self._assert("needle" in read.get("result", {}).get("structuredContent", {}).get("content", ""), "HTTP read_file")

            changed_cwd = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 51, "method": "tools/call", "params": {"name": "set_default_cwd", "arguments": {"path": "nested"}}},
                headers,
            )[0].get("result", {}).get("structuredContent", {})
            self._assert(changed_cwd.get("default_cwd") == "nested", f"HTTP set_default_cwd: {changed_cwd}")
            self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 52, "method": "tools/call", "params": {"name": "set_default_cwd", "arguments": {"path": "."}}},
                headers,
            )
            listed_files = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 53, "method": "tools/call", "params": {"name": "list_files", "arguments": {"patterns": ["*.txt"]}}},
                headers,
            )[0].get("result", {}).get("structuredContent", {})
            self._assert(any(item.get("path") == "sample.txt" for item in listed_files.get("files", [])), "HTTP list_files")
            searched = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 54, "method": "tools/call", "params": {"name": "search_text", "arguments": {"query": "needle"}}},
                headers,
            )[0].get("result", {}).get("structuredContent", {})
            self._assert(searched.get("total_matches", 0) >= 1, "HTTP search_text")
            viewed = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 55, "method": "tools/call", "params": {"name": "view_image", "arguments": {"path": "pixel.png"}}},
                headers,
            )[0].get("result", {})
            self._assert(viewed.get("structuredContent", {}).get("mime_type") == "image/png", "HTTP view_image")
            patch = "*** Begin Patch\n*** Update File: sample.txt\n@@\n-needle beta\n+needle http\n*** End Patch\n"
            patched = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 56, "method": "tools/call", "params": {"name": "apply_patch", "arguments": {"patch": patch, "intent": "HTTP patch regression"}}},
                headers,
            )[0].get("result", {}).get("structuredContent", {})
            self._assert(patched.get("ok") is True, f"HTTP apply_patch: {patched}")

            command_result = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "exec_command", "arguments": {"cmd": self._python_command("print('http-e2e')"), "intent": "HTTP regression smoke", "yield_time_ms": 30000}}},
                headers,
            )[0]
            command_payload = command_result.get("result", {}).get("structuredContent", {})
            self._assert(command_payload.get("exit_code") == 0, f"HTTP exec_command: {command_payload}")
            self._assert("http-e2e" in str(command_payload.get("stdout", "")), "HTTP exec stdout")

            running = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "exec_command", "arguments": {"cmd": self._python_command("import time; print('long-start', flush=True); time.sleep(5)"), "intent": "session lifecycle smoke", "yield_time_ms": 0}}},
                headers,
            )[0].get("result", {}).get("structuredContent", {})
            session = str(running.get("session_id") or "")
            active_session = session
            self._assert(session, f"long exec did not return session: {running}")
            polled: dict[str, Any] = {}
            for request_id in range(8, 14):
                polled = self._http_rpc(
                    port,
                    {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": "write_stdin", "arguments": {"session_id": session, "chars": "", "yield_time_ms": 1500}}},
                    headers,
                )[0].get("result", {}).get("structuredContent", {})
                if polled.get("status") != "running":
                    break
            self._assert(polled.get("status") in {"exited", "terminated", "killed"}, f"write_stdin lifecycle: {polled}")
            output_ref = str(polled.get("output_refs", {}).get("stdout") or f"session:{session}:stdout")
            self._assert(output_ref.startswith("session:"), f"missing output ref: {polled}")
            output = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "read_output", "arguments": {"output_ref": output_ref}}},
                headers,
            )[0].get("result", {}).get("structuredContent", {})
            self._assert("long-start" in output.get("content", ""), "read_output content")
            active_session = ""

            kill_running = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 15, "method": "tools/call", "params": {"name": "exec_command", "arguments": {"cmd": "Start-Sleep -Seconds 10", "intent": "kill session regression", "yield_time_ms": 0}}},
                headers,
            )[0].get("result", {}).get("structuredContent", {})
            kill_session_id = str(kill_running.get("session_id") or "")
            active_session = kill_session_id
            self._assert(kill_session_id, f"kill test did not return session: {kill_running}")
            killed = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": 16, "method": "tools/call", "params": {"name": "kill_session", "arguments": {"session_id": kill_session_id, "signal": "KILL", "wait_ms": 3000}}},
                headers,
            )[0].get("result", {}).get("structuredContent", {})
            self._assert(killed.get("status") in {"killed", "terminated", "exited"}, f"kill_session lifecycle: {killed}")
            active_session = ""
        finally:
            if active_session and mcp_headers is not None:
                try:
                    self._http_rpc(
                        port,
                        {"jsonrpc": "2.0", "id": "cleanup", "method": "tools/call", "params": {"name": "kill_session", "arguments": {"session_id": active_session, "signal": "KILL", "wait_ms": 2000}}},
                        mcp_headers,
                    )
                except Exception:
                    pass
            terminate_process(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def run_http_endpoint_controls(self) -> None:
        """Check the non-session HTTP control/metadata endpoints in isolation."""

        assert self.e2e_workspace is not None
        package = self.package_parent
        port = free_port()
        health_port = free_port({port})
        env = os.environ.copy()
        env.update(
            {
                "PYTHONPATH": str(package),
                "CODING_TOOLS_MCP_RUNTIME_ROOT": str(self.runtime_root),
                "CODING_TOOLS_MCP_HEALTH_PORT": str(health_port),
                "CODING_TOOLS_MCP_PERMISSION_MODE": "dangerous",
                "CODING_TOOLS_MCP_TELEMETRY": "off",
            }
        )
        command = [
            sys.executable,
            "-m",
            "coding_tools_mcp",
            "--workspace",
            str(self.e2e_workspace),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--permission-mode",
            "dangerous",
        ]
        process = subprocess.Popen(
            command,
            cwd=str(self.package_parent.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        try:
            wait_http(health_port, "/healthz", timeout=20)

            status, _headers, body = self._http_raw("GET", port, "/.well-known/mcp.json")
            self._assert(status == 200, f"mcp metadata status={status}: {body[:300]!r}")
            metadata = json.loads(body.decode("utf-8"))
            self._assert(metadata.get("tools", {}).get("count") == 20, f"mcp metadata: {metadata}")

            status, _headers, _body = self._http_raw("OPTIONS", port, "/mcp")
            self._assert(status == 204, f"MCP OPTIONS status={status}")

            discover = self._http_rpc(
                port,
                {
                    "jsonrpc": "2.0",
                    "id": "discover",
                    "method": "server/discover",
                    "params": {},
                },
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                },
            )[0]
            supported = discover.get("result", {}).get("supportedVersions", [])
            self._assert("2025-11-25" in supported and "2026-07-28" in supported, f"server/discover: {discover}")

            ping = self._http_rpc(
                port,
                {"jsonrpc": "2.0", "id": "ping", "method": "ping", "params": {}},
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                },
            )[0]
            self._assert(ping.get("result") == {}, f"ping: {ping}")

            notify = http_json("POST", health_port, "/notify-tools-changed")
            self._assert(notify.get("status") == "ok" and int(notify.get("tool_list_generation", 0)) >= 1, f"notify-tools-changed: {notify}")
            pruned = http_json("POST", health_port, "/prune")
            self._assert(pruned.get("status") == "ok" and isinstance(pruned.get("http_sessions"), dict), f"prune: {pruned}")

            status, _headers, _body = self._http_raw("GET", port, "/not-an-endpoint")
            self._assert(status == 404, f"unknown endpoint status={status}")

            init, init_headers = self._http_rpc(
                port,
                {
                    "jsonrpc": "2.0",
                    "id": "delete-init",
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "endpoint-check", "version": "1"}},
                },
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-11-25",
                },
            )
            self._assert("error" not in init, f"endpoint initialize: {init}")
            session_id = str(init_headers.get("Mcp-Session-Id") or init_headers.get("mcp-session-id") or "")
            self._assert(session_id, f"endpoint session header: {init_headers}")
            status, _headers, _body = self._http_raw(
                "DELETE",
                port,
                "/mcp",
                headers={"Mcp-Session-Id": session_id},
            )
            self._assert(status == 200, f"DELETE /mcp status={status}")
        finally:
            terminate_process(process)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)

    def _http_raw(
        self,
        method: str,
        port: int,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
        host: str = "127.0.0.1",
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(host, port, timeout=15)
        try:
            request_body: bytes | None
            if isinstance(body, str):
                request_body = body.encode("utf-8")
            else:
                request_body = body
            connection.request(method, path, body=request_body, headers=headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, {key: value for key, value in response.getheaders()}, raw
        finally:
            connection.close()

    def _http_rpc(self, port: int, payload: dict[str, Any], headers: dict[str, str]) -> tuple[dict[str, Any], dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            body = json.dumps(payload, separators=(",", ":"))
            connection.request("POST", "/mcp", body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if response.status != 200:
                self.fail(f"HTTP MCP status {response.status}: {raw[:500]!r}")
            decoded = json.loads(raw.decode("utf-8"))
            return decoded, {key: value for key, value in response.getheaders()}
        finally:
            connection.close()

    def check_installed_consistency(self) -> None:
        health_url = os.environ.get("CODING_TOOLS_MCP_TEST_HEALTH_URL", "http://127.0.0.1:8766/healthz")
        parsed = health_url.split("://", 1)[-1].split("/", 1)
        host_port = parsed[0].split(":", 1)
        host = host_port[0]
        port = int(host_port[1]) if len(host_port) > 1 else 80
        path = "/" + parsed[1] if len(parsed) > 1 else "/healthz"
        health = http_json("GET", port, path, host=host)
        self._assert(health.get("status") == "ok", f"installed health: {health}")
        identity = health.get("build_identity") if isinstance(health.get("build_identity"), dict) else {}
        installed_sha = str(identity.get("git_sha") or "")
        current_sha = git_head(self.package_parent.parent)
        source_version = ""
        try:
            from coding_tools_mcp import __version__

            source_version = str(__version__)
        except ImportError:
            pass
        self._assert(installed_sha and current_sha, "could not determine installed or source Git identity")
        if source_version:
            self._assert(
                str(identity.get("package_version") or health.get("version") or "") == source_version,
                f"installed package version mismatch: running={identity.get('package_version')}, source={source_version}",
            )
        self._assert(
            current_sha.startswith(installed_sha) or installed_sha.startswith(current_sha),
            f"installed source mismatch: running={installed_sha}, source={current_sha}",
        )

    def check_installed_brokers(self) -> None:
        service_root = Path(os.environ.get("CODING_TOOLS_MCP_TEST_SERVICE_ROOT", r"C:\ProgramData\WebGPTCodingToolsMCPService"))
        for name in ("elevated-broker-launcher.exe", "interactive-broker-launcher.exe"):
            path = service_root / name
            if not path.is_file():
                self.fail(f"installed broker artifact is missing: {path}")
            completed = subprocess.run([str(path), "--self-test"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            if completed.returncode != 0:
                self.fail(f"{name} --self-test exited {completed.returncode}: {tail(completed.stdout)}")

    def add_manual_checks(self) -> None:
        if self.mode == "quick":
            return
        manual = (
            ("manual.web_console.visual", "requires opening the live Web Console and checking visible tabs/settings"),
            ("manual.web_console.system_actions", "start/restart/update/rollback buttons change live services"),
            ("manual.human_help.live_delivery", "requires a real visible human response and Web Console/desktop routing"),
            ("manual.request_permissions.approval", "opens a Windows approval dialog for the signed-in user"),
            ("manual.request_elevated_action", "runs a registered administrator/deployment action"),
            ("manual.exec_command.active_user", "requires the signed-in desktop broker and may interact with the user's session"),
            ("manual.uac_update_rollback_restart", "changes installed services or machine state"),
        )
        for name, detail in manual:
            self.results.append(TestResult(name, "MANUAL", 0, detail))
            print(f"[MANUAL] {name} - {detail}", flush=True)
        paused = (
            ("paused.computer_use", "paused by Ken; no desktop-control action is invoked"),
            ("paused.browser_use", "paused by Ken; no browser-control action is invoked"),
        )
        for name, detail in paused:
            self.results.append(TestResult(name, "PAUSED", 0, detail))
            print(f"[PAUSED] {name} - {detail}", flush=True)

    def _python_command(self, code: str) -> str:
        # exec_command intentionally goes through the platform shell.  Keep
        # the smoke commands shell-native on Windows so nested quote parsing
        # cannot turn a passing test into a PowerShell parser error.
        if os.name == "nt":
            if "long-start" in code:
                return "Write-Output 'long-start'; Start-Sleep -Seconds 5"
            marker = "coding-tools-e2e" if "coding-tools-e2e" in code else "http-e2e"
            return f"Write-Output '{marker}'"
        import shlex

        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    def _long_running_command(self, marker: str, seconds: int) -> str:
        """Return a shell-native command used only inside the disposable test workspace."""
        if os.name == "nt":
            return f"Write-Output '{marker}'; Start-Sleep -Seconds {int(seconds)}"
        import shlex

        code = f"print({marker!r}, flush=True); import time; time.sleep({int(seconds)})"
        return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    @staticmethod
    def _assert(condition: Any, message: str) -> None:
        if not condition:
            raise TestFailure(message)

    def _assert_ok(self, result: dict[str, Any], name: str) -> None:
        payload = result.get("structuredContent") if isinstance(result, dict) else None
        self._assert(isinstance(payload, dict) and payload.get("ok") is True and result.get("isError") is False, f"{name}: {result}")

    def _assert_ok_payload(self, payload: dict[str, Any], name: str) -> None:
        self._assert(payload.get("ok") is True, f"{name}: {payload}")

    def finish(self) -> int:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.status] = counts.get(result.status, 0) + 1
        print("TEST_SUMMARY " + json.dumps({"mode": self.mode, "counts": counts}, sort_keys=True), flush=True)
        if any(result.status in {"FAIL", "ERROR"} for result in self.results):
            return 1
        return 0


def elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def tail(value: str, limit: int = 1200) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[-limit:]


def free_port(excluded: set[int] | None = None) -> int:
    excluded = excluded or set()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        for _ in range(8):
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
            if port not in excluded:
                return port
            sock.close()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raise TestFailure("could not allocate two distinct loopback test ports")


def wait_http(port: int, path: str, *, timeout: float, host: str = "127.0.0.1") -> None:
    deadline = time.time() + timeout
    last_error = "not probed"
    while time.time() < deadline:
        try:
            response = http_json("GET", port, path, host=host)
            if response.get("status") == "ok":
                return
            last_error = str(response)
        except Exception as exc:  # noqa: BLE001 - retain retry detail
            last_error = str(exc)
        time.sleep(0.2)
    raise TestFailure(f"HTTP endpoint {host}:{port}{path} did not become ready: {last_error}")


def http_json(method: str, port: int, path: str, *, host: str = "127.0.0.1") -> dict[str, Any]:
    connection = http.client.HTTPConnection(host, port, timeout=5)
    try:
        connection.request(method, path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        if response.status >= 400:
            raise TestFailure(f"HTTP {method} {path} returned {response.status}: {raw[:300]!r}")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise TestFailure(f"HTTP {method} {path} returned a non-object JSON payload")
        return payload
    finally:
        connection.close()


def git_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Coding Tools MCP regression tests.")
    parser.add_argument("--mode", choices=("quick", "full", "system"), default="quick")
    parser.add_argument("--package-parent", default=str(Path(__file__).resolve().parents[1] / "private"))
    parser.add_argument("--workspace", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--health-url", default=None, help="installed healthz URL for --mode system")
    parser.add_argument("--service-root", default=None, help="installed service root for --mode system")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.health_url:
        os.environ["CODING_TOOLS_MCP_TEST_HEALTH_URL"] = args.health_url
    if args.service_root:
        os.environ["CODING_TOOLS_MCP_TEST_SERVICE_ROOT"] = args.service_root
    runner = TestRunner(
        mode=args.mode,
        package_parent=Path(args.package_parent),
        workspace=Path(args.workspace),
    )
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
