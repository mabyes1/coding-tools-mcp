from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any


def run_cwd_git_permission_checks(server: Any, runtime_module: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="coding-tools-cwd-check-") as temporary:
        cwd_workspace = Path(temporary)
        project = cwd_workspace / "project"
        project.mkdir()
        nested_only = cwd_workspace / "nested" / "deep-project"
        nested_only.mkdir(parents=True)
        primary = server.Runtime(cwd_workspace, enable_view_image=False)
        try:
            primary.state_owner = "selfcheck-owner"
            web_project = primary.set_default_cwd({"project_name": "PROJECT"})
            if web_project.get("default_cwd") != "project":
                raise RuntimeError("Web Project name did not resolve case-insensitively to a first-level directory")
            missing_web_project = primary.set_default_cwd({"project_name": "deep-project"})
            if missing_web_project.get("default_cwd") != ".":
                raise RuntimeError("Web Project resolution searched recursively instead of falling back to workspace root")
            changed = primary.exec_command({"cmd": "cd project"})
            if not changed.get("cwd_persisted") or changed.get("default_cwd") != "project":
                raise RuntimeError("directory-only exec did not persist the new default cwd")
            parent = primary.exec_command({"cmd": "cd .."})
            if parent.get("default_cwd") != ".":
                raise RuntimeError("a safe parent-directory change did not return to the workspace root")
            windows_style = primary.exec_command({"cmd": 'cd /d "project"'})
            if windows_style.get("default_cwd") != "project":
                raise RuntimeError("CMD-style cd /d did not persist the new default cwd")

            patch_target = project / "patch-target.txt"
            patch_target.write_text("before\n", encoding="utf-8")
            subprocess.run(
                [server.require_git(), "init", "-q", str(project)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            patch_result = primary.apply_patch(
                {
                    "patch": "\n".join(
                        [
                            "*** Begin Patch",
                            "*** Update File: patch-target.txt",
                            "@@",
                            "-before",
                            "+after",
                            "*** End Patch",
                        ]
                    )
                }
            )
            if patch_target.read_text(encoding="utf-8") != "after\n" or patch_result.get("base") != "project":
                raise RuntimeError("apply_patch did not resolve a relative path from the default cwd")
            repo, filters = primary._git_repo_scope({"path": "."})
            if repo != project or filters:
                raise RuntimeError("git_diff scope did not resolve '.' from the default cwd")

            git = server.require_git()
            for config_key, config_value in (
                ("user.name", "Coding Tools Validator"),
                ("user.email", "validator@example.invalid"),
            ):
                subprocess.run(
                    [git, "-C", str(project), "config", config_key, config_value],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            subprocess.run(
                [git, "-C", str(project), "add", "patch-target.txt"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                [git, "-C", str(project), "commit", "-q", "-m", "validator baseline"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            patch_target.write_text("after\nchanged\n", encoding="utf-8")

            git_status_result = primary.git_status({"path": "."})
            status_entry = next(
                (
                    item
                    for item in git_status_result.get("entries", [])
                    if item.get("path") == "patch-target.txt"
                ),
                None,
            )
            if not git_status_result.get("is_repo") or status_entry is None:
                raise RuntimeError("git_status repository/change contract drifted")
            if status_entry.get("worktree_status") != "M":
                raise RuntimeError("git_status worktree-status contract drifted")

            git_diff_result = primary.git_diff({"path": "."})
            if "changed" not in str(git_diff_result.get("diff") or ""):
                raise RuntimeError("git_diff content contract drifted")
            if not any(
                item.get("path") == "patch-target.txt" and item.get("status") == "modified"
                for item in git_diff_result.get("files", [])
            ):
                raise RuntimeError("git_diff file metadata contract drifted")

            git_log_result = primary.git_log({"path": ".", "max_count": 1})
            commits = git_log_result.get("commits", [])
            if not commits or commits[0].get("subject") != "validator baseline":
                raise RuntimeError("git_log commit metadata contract drifted")

            git_show_result = primary.git_show(
                {"path": ".", "rev": "HEAD", "include_diff": False}
            )
            if not git_show_result.get("is_repo") or "validator baseline" not in str(
                git_show_result.get("content") or ""
            ):
                raise RuntimeError("git_show metadata-only contract drifted")

            git_blame_result = primary.git_blame(
                {"path": "patch-target.txt", "rev": "HEAD", "start_line": 1, "max_lines": 10}
            )
            blame_lines = git_blame_result.get("lines", [])
            if not git_blame_result.get("is_repo") or not blame_lines:
                raise RuntimeError("git_blame repository/line contract drifted")
            if str(blame_lines[0].get("content") or "").strip() != "after":
                raise RuntimeError("git_blame line-text contract drifted")
            if not str(blame_lines[0].get("commit") or ""):
                raise RuntimeError("git_blame commit attribution contract drifted")

            try:
                server.validate_git_ref("-unsafe")
            except server.ToolFailure as exc:
                if exc.code != "INVALID_ARGUMENT":
                    raise
            else:
                raise RuntimeError("git revision validation accepted an option-like ref")

            reconnect = server.Runtime(
                cwd_workspace,
                enable_view_image=False,
                project_context=primary.project_context,
                execution_registry=primary.execution_registry,
            )
            isolated = server.Runtime(
                cwd_workspace,
                enable_view_image=False,
                project_context=primary.project_context,
                execution_registry=primary.execution_registry,
            )
            try:
                reconnect.state_owner = "selfcheck-owner"
                isolated.state_owner = "different-owner"
                if reconnect.default_cwd_display() != "project":
                    raise RuntimeError("default cwd did not survive an owner reconnect")
                if isolated.default_cwd_display() != ".":
                    raise RuntimeError("default cwd leaked across owners")

                original_approval = runtime_module.request_permission_approval
                runtime_module.request_permission_approval = lambda **_kwargs: {"ok": True, "granted": True}
                try:
                    blocked_arguments = {"cmd": "curl https://example.invalid"}
                    once = primary.request_permissions(
                        {
                            "tool_name": "exec_command",
                            "permission": "network",
                            "reason": "permission self-check",
                            "arguments": blocked_arguments,
                            "scope": "once",
                            "ttl_seconds": 60,
                        }
                    )
                    if once.get("status") != "granted":
                        raise RuntimeError("interactive approval did not create a permission grant")
                    privileged = primary.request_permissions(
                        {
                            "tool_name": "exec_command",
                            "permission": "privileged_executable",
                            "reason": "privilege-boundary self-check",
                            "arguments": {"cmd": "approved-tool"},
                        }
                    )
                    privileged_constraints = privileged.get("constraints", {})
                    if "never grants Administrator" not in str(privileged_constraints.get("privileged_executable_effect", "")):
                        raise RuntimeError("privileged_executable grant does not disclose its OS privilege boundary")
                    primary.request_context.tool_name = "exec_command"
                    primary.request_context.arguments = blocked_arguments
                    primary.request_context.claimed_permission_grants = set()
                    primary._check_command_policy(blocked_arguments["cmd"], blocked_arguments)
                    primary._finish_permission_grants()
                    try:
                        primary._check_command_policy(blocked_arguments["cmd"], blocked_arguments)
                    except server.ToolFailure as exc:
                        if exc.code != "PERMISSION_REQUIRED":
                            raise
                    else:
                        raise RuntimeError("one-shot permission grant was not consumed")

                    session = primary.request_permissions(
                        {
                            "tool_name": "exec_command",
                            "permission": "network",
                            "reason": "session permission self-check",
                            "arguments": blocked_arguments,
                            "scope": "session",
                            "ttl_seconds": 60,
                        }
                    )
                    if session.get("status") != "granted":
                        raise RuntimeError("session approval did not create a permission grant")
                    changed_arguments = {"cmd": "curl https://different.invalid"}
                    reconnect.request_context.tool_name = "exec_command"
                    reconnect.request_context.arguments = changed_arguments
                    reconnect.request_context.claimed_permission_grants = set()
                    reconnect._check_command_policy(changed_arguments["cmd"], changed_arguments)
                finally:
                    runtime_module.request_permission_approval = original_approval

                dangerous = server.Runtime(
                    cwd_workspace,
                    enable_view_image=False,
                    permission_mode="dangerous",
                    project_context=primary.project_context,
                    execution_registry=primary.execution_registry,
                )
                try:
                    dangerous.state_owner = "dangerous-selfcheck-owner"
                    dangerous.request_context.tool_name = "exec_command"
                    dangerous.request_context.arguments = {"cmd": "curl https://yolo.invalid"}
                    dangerous.request_context.claimed_permission_grants = set()
                    dangerous._check_command_policy(
                        "curl https://yolo.invalid",
                        dangerous.request_context.arguments,
                    )
                    if not dangerous.dangerously_skip_all_permissions:
                        raise RuntimeError("dangerous mode did not enable the YOLO permission policy")
                    dangerous_exec = next(
                        tool for tool in dangerous.list_tools()["tools"] if tool["name"] == "exec_command"
                    )
                    dangerous_description = str(dangerous_exec.get("description", ""))
                    if "edit, move, rename, or delete files" not in dangerous_description:
                        raise RuntimeError("dangerous mode did not expose YOLO filesystem mutation in exec_command")

                    safe_exec = next(
                        tool for tool in primary.list_tools()["tools"] if tool["name"] == "exec_command"
                    )
                    if "Never edit files." not in str(safe_exec.get("description", "")):
                        raise RuntimeError("safe mode lost the exec_command filesystem guard")
                finally:
                    dangerous.close()
            finally:
                isolated.close()
                reconnect.close()
        finally:
            primary.close()
