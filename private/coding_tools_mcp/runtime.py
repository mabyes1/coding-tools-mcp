from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .activity import append_activity_log, append_activity_start, redact_for_trace
from .command_policy import CommandPolicy
from .envutils import ENV_PREFIX
from .errors import JsonRpcError, ToolFailure, summarize_exception
from .elevated_actions import request_elevated_action, request_permission_approval
from .execution import (
    ExecutionService,
    MAX_ACTIVE_EXEC_SESSIONS,
    add_exec_diagnostics,
    base_command_env,
    command_env,
    interactive_command_env,
)
from .interactive_exec import (
    interactive_broker_status,
    request_computer_use,
    request_human_help,
    request_interactive_exec,
)
from .oauth import OAuthConfig
from .patching import AtomicPatchCommitter
from .permissions import PermissionService
from .processes import ExecSession
from .project_context import ProjectContext, load_project_context
from .protocol import PROTOCOL_VERSION
from .runtime_config import (
    PERMISSION_MODE_CAPABILITIES,
    PERMISSION_MODE_CHOICES,
    SHELL_ENV_INHERIT_CHOICES,
    ShellEnvPolicy,
)
from .runtime_meta import SERVER_NAME, SERVER_TITLE, runtime_build_identity, runtime_version
from .runtime_support import (
    ECOSYSTEM_CACHE_ENV_NAMES,
    LITERAL_DIRECTORY_CHANGE_RE,
    SPECIAL_DEVICE_PATHS,
    WINDOWS_CORE_ENV_NAMES,
    cached_which,
    configured_executable_allowlist,
    configured_tool_path,
    env_pattern_matches,
    exec_output_diagnostics,
    fallback_runtime_dir_for_workspace,
    is_allowed_external_executable,
    is_core_command_env_name,
    is_filtered_env_var,
    permission_failure_diagnostics,
    require_git,
    runtime_dir_for_workspace,
    structured_error_kind,
)
from .sandbox import (
    guard_allow_roots,
    landlock_exec_argv,
    landlock_status_payload,
    landlock_unavailable_warning,
    open_landlock_ruleset,
)
from .session_store import ExecutionRegistry
from .telemetry import SessionTelemetry
from .tool_catalog import PUBLIC_TOOL_NAMES, TOOL_REGISTRY, tool_definition
from .tool_results import make_tool_result
from .tool_schemas import INLINE_SCRIPT_PERMISSION, validate_arguments
from .tools.desktop import desktop_ui_action, human_help_tool
from .tools.diagnostics import (
    discover_tools,
    exec_environment_summary,
    execution_session_summary,
    landlock_enforced,
    server_info_payload as build_server_info_payload,
    skill_catalog,
)
from .tools.filesystem import (
    list_dir_tool,
    list_files_tool,
    read_file_tool,
    search_text_tool,
    truncate_line_chars,
)
from .tools.git_tools import GitTools
from .tools.images import view_image_tool
from .tools.patch_tools import apply_patch_tool
from .transport_http import MCP_ENDPOINT_PATH
from .workspace import (
    ResolvedPath,
    Workspace,
    is_relative_to,
    normalize_rel_display,
    workspace_catalog_from_env,
    workspace_entry_for_selector,
)

class Runtime:
    def __init__(
        self,
        workspace: Path,
        *,
        enable_view_image: bool = True,
        permission_mode: str = "safe",
        shell_env_policy: ShellEnvPolicy | None = None,
        allow_network: bool = False,
        auth_token: str | None = None,
        oauth_config: OAuthConfig | None = None,
        project_context: ProjectContext | None = None,
        fake_readonly_annotations: bool = False,
        transport: str = "stdio",
        execution_registry: ExecutionRegistry | None = None,
    ) -> None:
        self.workspace = Workspace(workspace)
        self.enable_view_image = enable_view_image
        self._exposed_tool_names = [
            name
            for name in PUBLIC_TOOL_NAMES
            for spec in (TOOL_REGISTRY[name],)
            if spec.gated_by is None or getattr(self, spec.gated_by)
        ]
        self._exposed_tool_name_set = frozenset(self._exposed_tool_names)
        if permission_mode not in PERMISSION_MODE_CHOICES:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Unknown permission mode: {permission_mode}",
                category="validation",
                details={"supported": list(PERMISSION_MODE_CHOICES)},
            )
        self.permission_mode = permission_mode
        self.capabilities = PERMISSION_MODE_CAPABILITIES[permission_mode]
        self.dangerously_skip_all_permissions = self.capabilities.skip_all_permissions
        # Faking annotations is only defensible where the caller has already
        # asserted the workspace is disposable, so bind it to that assertion
        # instead of letting it be set orthogonally.
        if fake_readonly_annotations and permission_mode != "dangerous":
            raise ToolFailure(
                "INVALID_ARGUMENT",
                "fake_readonly_annotations requires permission_mode=dangerous.",
                category="validation",
                details={"permission_mode": permission_mode},
            )
        self.fake_readonly_annotations = fake_readonly_annotations
        self.shell_env_policy = shell_env_policy or ShellEnvPolicy()
        if self.shell_env_policy.inherit not in SHELL_ENV_INHERIT_CHOICES:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"Unknown shell env inherit policy: {self.shell_env_policy.inherit}",
                category="validation",
                details={"supported": list(SHELL_ENV_INHERIT_CHOICES)},
            )
        self.allow_network = allow_network or self.capabilities.network
        self.auth_token = auth_token or None
        self.oauth_config = oauth_config
        self.execution_registry = execution_registry or ExecutionRegistry()
        self._owns_execution_registry = execution_registry is None
        self.server_instance_id = secrets.token_urlsafe(12)
        if self.execution_registry.runtime_dir is None:
            self._set_runtime_dir(runtime_dir_for_workspace(self.workspace.root, self.server_instance_id))
            self.fallback_runtime_dir = fallback_runtime_dir_for_workspace(self.workspace.root, self.server_instance_id)
            self.execution_registry.runtime_dir = self.runtime_dir
            self.execution_registry.fallback_runtime_dir = self.fallback_runtime_dir
        else:
            self._set_runtime_dir(self.execution_registry.runtime_dir)
            self.fallback_runtime_dir = self.execution_registry.fallback_runtime_dir
        self.default_cwd = self.workspace.root
        self.state_owner: str | None = None
        self._closed = False
        self.http_session_id = secrets.token_urlsafe(24)
        self.protocol_version = PROTOCOL_VERSION
        self.patch_baselines: dict[str, str | None] = {}
        self.patch_lock = threading.Lock()
        self.patch_committer = AtomicPatchCommitter()
        # ProjectContext is frozen and derived only from the workspace tree, so
        # per-session HTTP runtimes reuse the server's copy instead of re-running
        # discovery (git ls-files / directory walk) on every connect.
        self.project_context: ProjectContext = (
            project_context if project_context is not None else load_project_context(self.workspace.root)
        )
        self.request_sessions: dict[str | int, str] = {}
        self.request_sessions_lock = threading.Lock()
        self.request_context = threading.local()
        self.initialized = False
        self.telemetry = SessionTelemetry(permission_mode=self.permission_mode, transport=transport)
        self._tool_handlers = {name: getattr(self, name) for name in TOOL_REGISTRY}

    def _set_runtime_dir(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.home_dir = self.runtime_dir / "home"
        self.tmp_dir = self.runtime_dir / "tmp"
        self.cache_dir = self.runtime_dir / "cache"

    def close(self) -> None:
        with self.sessions_lock:
            if self._closed:
                return
            self._closed = True
        if self._owns_execution_registry:
            self.execution_registry.close()
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
            if self.fallback_runtime_dir is not None and self.fallback_runtime_dir != self.runtime_dir:
                shutil.rmtree(self.fallback_runtime_dir, ignore_errors=True)
        self.telemetry.finish()

    @property
    def sessions(self) -> dict[str, ExecSession]:
        return self.execution_registry.sessions

    @property
    def output_sessions(self) -> dict[str, ExecSession]:
        return self.execution_registry.output_sessions

    @property
    def sessions_lock(self) -> threading.Lock:
        return self.execution_registry.sessions_lock

    @property
    def starting_sessions(self) -> int:
        return self.execution_registry.starting_sessions

    @starting_sessions.setter
    def starting_sessions(self, value: int) -> None:
        self.execution_registry.starting_sessions = value

    def _ensure_runtime_dirs(self) -> None:
        candidates = [self.runtime_dir]
        if self.fallback_runtime_dir is not None and self.fallback_runtime_dir not in candidates:
            candidates.append(self.fallback_runtime_dir)
        errors: list[str] = []
        for runtime_dir in candidates:
            self._set_runtime_dir(runtime_dir)
            try:
                for path in (
                    self.runtime_dir.parent,
                    self.runtime_dir,
                    self.home_dir,
                    self.tmp_dir,
                    self.cache_dir,
                ):
                    path.mkdir(parents=True, mode=0o700, exist_ok=True)
                    if os.name != "nt":
                        try:
                            path.chmod(0o700)
                        except OSError:
                            pass
                return
            except OSError as exc:
                errors.append(f"{runtime_dir}: {exc}")
        raise ToolFailure(
            "RUNTIME_DIR_UNWRITABLE",
            "Runtime directory could not be created outside the workspace.",
            category="runtime",
            details={"attempted": errors},
        )

    def command_home_dir(self) -> Path:
        return self.home_dir

    def command_tmp_dir(self) -> Path:
        return self.tmp_dir

    def global_tmp_write_policy(self) -> str:
        return self.capabilities.global_tmp_write

    def shell_expansion_policy(self) -> str:
        return "allowed" if self.capabilities.shell_expansion else "blocked"

    def inline_script_policy(self) -> str:
        return "allowed" if self.capabilities.inline_script else "blocked"

    def secret_env_filter_policy(self) -> str:
        return "enabled" if self.capabilities.secret_env_filter else "disabled"

    def landlock_enabled(self) -> bool:
        return self.capabilities.landlock

    def landlock_write_roots(self) -> list[Path]:
        return [self.runtime_dir]

    def is_allowed_command_tmp_path(self, candidate: str) -> bool:
        if self.capabilities.skip_all_permissions:
            return False
        try:
            resolved = Path(candidate).expanduser().resolve(strict=False)
        except OSError:
            return False
        return is_relative_to(resolved, self.runtime_dir)

    def initialize(self, client_info: dict[str, Any] | None = None) -> dict[str, Any]:
        self.telemetry.record_session_start(client_info, self.protocol_version)
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {
                "name": SERVER_NAME,
                "title": SERVER_TITLE,
                "version": runtime_version(),
            },
            "instructions": self.project_context.server_instructions(),
        }

    def list_tools(self) -> dict[str, Any]:
        return {
            "tools": [
                tool_definition(name, fake_readonly=self.fake_readonly_annotations)
                for name in self.exposed_tool_names()
            ]
        }

    def exposed_tool_names(self) -> list[str]:
        return list(self._exposed_tool_names)

    def auth_enabled(self) -> bool:
        return self.auth_token is not None or self.oauth_config is not None

    def oauth_enabled(self) -> bool:
        return self.oauth_config is not None

    def default_cwd_display(self) -> str:
        return normalize_rel_display(self.effective_default_cwd(), self.workspace.root)

    def _owner_cwd_key(self) -> tuple[str, str] | None:
        if self.state_owner is None:
            return None
        return self.state_owner, os.path.normcase(str(self.workspace.root))

    def _permission_owner(self) -> str:
        return self.state_owner or f"mcp-session:{self.http_session_id}"

    def _permission_service(self) -> PermissionService:
        return PermissionService(
            store=self.execution_registry.permission_store,
            workspace_root=lambda: self.workspace.root,
            owner=self._permission_owner,
            dangerously_skip_all_permissions=self.dangerously_skip_all_permissions,
            request_context=self.request_context,
            request_approval=request_permission_approval,
        )

    @staticmethod
    def _permission_arguments_digest(arguments: dict[str, Any]) -> str:
        return PermissionService.arguments_digest(arguments)

    def _permission_granted(self, permission: str) -> bool:
        return self._permission_service().granted(permission)

    def _finish_permission_grants(self) -> None:
        self._permission_service().finish_request()

    def effective_default_cwd(self) -> Path:
        key = self._owner_cwd_key()
        if key is not None:
            with self.execution_registry.state_lock:
                shared = self.execution_registry.owner_default_cwds.get(key)
            if shared is not None:
                return shared
        return self.default_cwd

    def resolve_existing(self, raw_path: str = ".") -> ResolvedPath:
        return self.workspace.resolve_existing_at(self.effective_default_cwd(), raw_path)

    def resolve_for_write(self, raw_path: str) -> ResolvedPath:
        return self.workspace.resolve_for_write_at(self.effective_default_cwd(), raw_path)

    def git_path_filter(self, raw_path: str) -> str:
        if raw_path == ".":
            return self.default_cwd_display()
        return self.resolve_for_write(raw_path).display

    def _exec_environment_summary(self) -> dict[str, Any]:
        return exec_environment_summary(
            workspace_root=self.workspace.root,
            permission_mode=self.permission_mode,
            network_allowed=self.allow_network,
            runtime_dir=self.runtime_dir,
            home_dir=self.command_home_dir(),
            tmp_dir=self.command_tmp_dir(),
            cache_dir=self.cache_dir,
        )

    def _execution_session_summary(self) -> dict[str, Any]:
        self._prune_sessions()
        with self.sessions_lock:
            running = len(self.sessions)
            starting = self.starting_sessions
            retained_output = len(self.output_sessions)
        return execution_session_summary(
            running=running,
            starting=starting,
            retained_output=retained_output,
            max_running=MAX_ACTIVE_EXEC_SESSIONS,
        )

    def _landlock_enforced(self, landlock: dict[str, Any]) -> bool:
        return landlock_enforced(landlock, enabled=self.landlock_enabled())

    def _skill_catalog(self) -> list[dict[str, str]]:
        return skill_catalog(self.workspace.root)

    def server_info_payload(self) -> dict[str, Any]:
        tools = self.exposed_tool_names()
        landlock = landlock_status_payload()
        landlock["enabled"] = self._landlock_enforced(landlock)
        http_session_stats = (
            self.execution_registry.http_session_stats_provider()
            if self.execution_registry.http_session_stats_provider is not None
            else None
        )
        oauth_state_path = (
            str(self.oauth_config.state_store.path)
            if self.oauth_config is not None and self.oauth_config.state_store is not None
            else None
        )
        return build_server_info_payload(
            server=SERVER_NAME,
            title=SERVER_TITLE,
            version=runtime_version(),
            build_identity=runtime_build_identity(),
            protocol_version=self.protocol_version,
            exec_environment=self._exec_environment_summary(),
            workspace_allowlist=[
                {"name": entry.name, "path": str(entry.path)}
                for entry in workspace_catalog_from_env()
            ],
            default_cwd=self.default_cwd_display(),
            default_cwd_scope="oauth_owner_workspace" if self.state_owner else "mcp_session",
            auth_enabled=self.auth_enabled(),
            oauth={
                "enabled": self.oauth_enabled(),
                "persistent_state": oauth_state_path is not None,
                "state_path": oauth_state_path,
                "access_token_ttl_seconds": (
                    self.oauth_config.token_ttl if self.oauth_config is not None else None
                ),
                "refresh_token_ttl_seconds": (
                    self.oauth_config.refresh_token_ttl if self.oauth_config is not None else None
                ),
            },
            dangerously_skip_all_permissions=self.dangerously_skip_all_permissions,
            annotation_override="fake_readonly" if self.fake_readonly_annotations else None,
            landlock=landlock,
            exec_policy={
                "shell_expansion": self.shell_expansion_policy(),
                "inline_script": self.inline_script_policy(),
                "global_tmp_write": self.global_tmp_write_policy(),
                "secret_env_filter": self.secret_env_filter_policy(),
            },
            shell_env_inherit=self.shell_env_policy.inherit,
            shell_env_include_only=list(self.shell_env_policy.include_only),
            shell_env_exclude=list(self.shell_env_policy.exclude),
            endpoint_path=MCP_ENDPOINT_PATH,
            project_context={
                "root_instruction_files": [item.path for item in self.project_context.root_files],
                "nested_instruction_files": list(self.project_context.nested_files),
                "warnings": list(self.project_context.warnings),
            },
            skills=self._skill_catalog(),
            http_sessions=http_session_stats,
            execution=self._execution_session_summary(),
            tools=tools,
        )

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        request_id: str | int | None = None,
    ) -> dict[str, Any]:
        started_at = time.time()
        args = arguments or {}
        handler = self._tool_handlers.get(name) if name in self._exposed_tool_name_set else None
        if handler is None:
            raise JsonRpcError(-32602, f"Unknown tool: {name}", {"reason": "unknown_tool"})
        spec = TOOL_REGISTRY[name]
        validate_arguments(name, args)
        try:
            append_activity_start(name, args)
            self.request_context.request_id = request_id
            self.request_context.tool_name = name
            self.request_context.arguments = args
            self.request_context.claimed_permission_grants = set()
            try:
                payload = handler(args)
            finally:
                self._finish_permission_grants()
                if request_id is not None:
                    with self.request_sessions_lock:
                        self.request_sessions.pop(request_id, None)
                self.request_context.request_id = None
                self.request_context.tool_name = None
                self.request_context.arguments = None
            payload.setdefault("ok", True)
            self.emit_tool_trace(name, args, payload, started_at)
            content = spec.content_builder(payload) if spec.content_builder else None
            return make_tool_result(name, payload, is_error=payload.get("ok") is False, content=content)
        except ToolFailure as exc:
            payload = {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "kind": structured_error_kind(exc.code, exc.category, exc.message),
                    "message": exc.message,
                    "category": exc.category,
                    "retryable": exc.retryable,
                    "details": exc.details,
                },
            }
            if spec.error_status:
                payload["status"] = spec.error_status
            diagnostics = permission_failure_diagnostics(exc)
            if diagnostics:
                payload["diagnostics"] = diagnostics
            if exc.code == "PERMISSION_REQUIRED":
                permission = exc.details.get("permission")
                payload["permission_request"] = {
                    "tool_name": name,
                    "permission": permission or "unknown",
                    "status": "approval_required",
                    "retryable": True,
                    "interactive_approval_supported": True,
                    "next_action": "Call request_permissions with the blocked tool arguments; a Windows approval dialog will open for the signed-in user.",
                }
            if exc.code == "ELICITATION_UNSUPPORTED":
                payload["status"] = "unsupported"
            self.emit_tool_trace(name, args, payload, started_at)
            return make_tool_result(name, payload, is_error=True)
        except Exception as exc:  # noqa: BLE001 - tool failures must stay structured
            error_message, error_leaves = summarize_exception(exc)
            payload = {
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "kind": "runtime_error",
                    "message": error_message,
                    "category": "internal",
                    "retryable": False,
                    "details": {"leaf_errors": error_leaves},
                },
            }
            if spec.error_status:
                payload["status"] = spec.error_status
            self.emit_tool_trace(name, args, payload, started_at)
            return make_tool_result(name, payload, is_error=True)

    def server_info(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.server_info_payload()

    def human_help_me(self, args: dict[str, Any]) -> dict[str, Any]:
        return human_help_tool(args, request_human_help=request_human_help)

    def _desktop_ui_action(self, args: dict[str, Any], *, browser_only: bool) -> dict[str, Any]:
        return desktop_ui_action(
            args,
            browser_only=browser_only,
            request_computer_use=request_computer_use,
        )

    def computer_use(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._desktop_ui_action(args, browser_only=False)

    def browser_use(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._desktop_ui_action(args, browser_only=True)

    def check_exec_environment(self, args: dict[str, Any]) -> dict[str, Any]:
        landlock = landlock_status_payload()
        warnings: list[str] = []
        if not landlock.get("available"):
            warnings.append("Linux Landlock filesystem confinement is unavailable")
        if self.capabilities.skip_all_permissions:
            warnings.append("permission_mode=dangerous disables MCP safety gates")
        if self.fake_readonly_annotations:
            warnings.append(
                "tools/list annotations are faked as read-only; apply_patch and exec_command still mutate and execute"
            )
        requested = args.get("tools")
        names = [str(item) for item in requested] if isinstance(requested, list) and requested else [
            "rg", "fd", "git", "node", "dotnet", "pwsh", "powershell", "python", "adb"
        ]
        return {
            "ok": True,
            **self._exec_environment_summary(),
            "execution": self._execution_session_summary(),
            "execution_contexts": {
                "service": {
                    "available": True,
                    "managed_sessions": True,
                    "interactive_desktop": False,
                    "elevated": False,
                },
                "active_user": {
                    **interactive_broker_status(),
                    "managed_sessions": False,
                    "one_shot": True,
                    "interactive_desktop": True,
                },
            },
            "landlock_enabled": self._landlock_enforced(landlock),
            "landlock_abi": landlock.get("abi_version"),
            "global_tmp_write": self.global_tmp_write_policy(),
            "tool_discovery": self._discover_tools(names),
            "permission_elicitation_supported": True,
            "permission_approval_transport": "local_windows_broker",
            "executable_allowlist": list(configured_executable_allowlist()),
            "warnings": warnings,
        }

    def _discover_tools(self, names: list[str]) -> list[dict[str, Any]]:
        return discover_tools(names, configured_tool_path=configured_tool_path)

    def which_tools(self, args: dict[str, Any]) -> dict[str, Any]:
        requested = args.get("tools")
        names = [str(item) for item in requested] if isinstance(requested, list) else []
        if not names:
            names = ["rg", "fd", "git", "node", "dotnet", "pwsh", "powershell", "python", "adb"]
        return {"tools": self._discover_tools(names)}

    def get_default_cwd(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace.root),
            "default_cwd": self.default_cwd_display(),
            "scope": "oauth_owner_workspace" if self.state_owner else "mcp_session",
        }

    def list_workspaces(self, args: dict[str, Any]) -> dict[str, Any]:
        entries = workspace_catalog_from_env()
        current_key = os.path.normcase(str(self.workspace.root))
        return {
            "workspaces": [
                {
                    "name": entry.name,
                    "path": str(entry.path),
                    "active": os.path.normcase(str(entry.path)) == current_key,
                }
                for entry in entries
            ],
            "switching_enabled": bool(entries),
        }

    def switch_workspace(self, args: dict[str, Any]) -> dict[str, Any]:
        entry = workspace_entry_for_selector(str(args.get("workspace", "")))
        selected = Workspace(entry.path)
        with self.sessions_lock:
            if self.sessions or self.starting_sessions:
                raise ToolFailure(
                    "WORKSPACE_SWITCH_BUSY",
                    "Stop running commands before switching workspaces.",
                    category="runtime",
                    retryable=True,
                    details={"running": len(self.sessions), "starting": self.starting_sessions},
                )
            # Retained output refs are tied to the previous workspace.  They
            # cannot be safely interpreted after a root switch, so discard
            # completed buffers while preserving the command/session registry.
            self.output_sessions.clear()
            self.workspace = selected
            self.default_cwd = selected.root
            self.project_context = load_project_context(selected.root)
            self.patch_baselines.clear()
        with self.request_sessions_lock:
            self.request_sessions.clear()
        return {
            "workspace": str(selected.root),
            "name": entry.name,
            "default_cwd": self.default_cwd_display(),
        }

    def set_default_cwd(self, args: dict[str, Any]) -> dict[str, Any]:
        project_name = str(args.get("project_name") or "").strip()
        if project_name:
            if project_name in {".", ".."} or "/" in project_name or "\\" in project_name:
                raise ToolFailure(
                    "INVALID_PROJECT_NAME",
                    "project_name must be a single first-level workspace directory name.",
                    category="validation",
                )
            exact_match: Path | None = None
            folded_matches: list[Path] = []
            try:
                for child in self.workspace.root.iterdir():
                    if not child.is_dir():
                        continue
                    if child.name == project_name:
                        exact_match = child
                        break
                    if child.name.casefold() == project_name.casefold():
                        folded_matches.append(child)
            except OSError:
                folded_matches = []
            selected = exact_match or (folded_matches[0] if len(folded_matches) == 1 else self.workspace.root)
            return self._store_default_cwd(selected)

        resolved = self.workspace.resolve_existing(str(args.get("path", ".")))
        if not resolved.path.is_dir():
            raise ToolFailure("NOT_A_DIRECTORY", "Default cwd must be a directory.", category="validation")
        return self._store_default_cwd(resolved.path)

    def _store_default_cwd(self, path: Path) -> dict[str, Any]:
        self.default_cwd = path
        key = self._owner_cwd_key()
        if key is not None:
            with self.execution_registry.state_lock:
                self.execution_registry.owner_default_cwds[key] = path
        return {
            "workspace": str(self.workspace.root),
            "default_cwd": normalize_rel_display(path, self.workspace.root),
            "scope": "oauth_owner_workspace" if self.state_owner else "mcp_session",
        }

    def _literal_directory_change(self, cmd: str, base: Path) -> Path | None:
        match = LITERAL_DIRECTORY_CHANGE_RE.fullmatch(cmd)
        if match is None:
            return None
        target = next((group.strip() for group in match.groups() if group is not None), "")
        # Expansion and wildcard semantics vary by shell. Only persist a path
        # whose literal meaning is unambiguous; all other commands run normally.
        if not target or any(marker in target for marker in ("$", "%", "`", "*", "?", "~")):
            return None
        candidate = Path(target)
        if not candidate.is_absolute() and not re.match(r"^[A-Za-z]:[\\/]", target):
            candidate = base / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if not resolved.is_dir() or not is_relative_to(resolved, self.workspace.root):
            return None
        return resolved

    def emit_tool_trace(self, name: str, args: dict[str, Any], payload: dict[str, Any], started_at: float) -> None:
        raw_error = payload.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        duration_ms = int((time.time() - started_at) * 1000)
        self.telemetry.record_tool_call(
            name,
            ok=bool(payload.get("ok")),
            error_code=error.get("code"),
            duration_ms=duration_ms,
            truncated=bool(payload.get("truncated")),
        )
        append_activity_log(name, args, payload, duration_ms)
        if os.environ.get(f"{ENV_PREFIX}_TRACE") != "1":
            return
        event = {
            "event": "tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "tool": name,
            "ok": bool(payload.get("ok", False)),
            "status": payload.get("status"),
            "error_code": error.get("code"),
            "duration_ms": duration_ms,
            "session_id": payload.get("session_id"),
            "truncated": payload.get("truncated"),
            "args": redact_for_trace(args),
        }
        print(json.dumps(event, sort_keys=True, separators=(",", ":")), file=sys.stderr, flush=True)

    def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        return read_file_tool(args, resolve_existing=self.resolve_existing)

    def list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        return list_dir_tool(
            args,
            resolve_existing=self.resolve_existing,
            workspace=self.workspace,
        )

    def list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        return list_files_tool(
            args,
            resolve_existing=self.resolve_existing,
            workspace=self.workspace,
            cached_which=cached_which,
        )

    def search_text(self, args: dict[str, Any]) -> dict[str, Any]:
        return search_text_tool(
            args,
            resolve_existing=self.resolve_existing,
            workspace=self.workspace,
            cached_which=cached_which,
        )

    def apply_patch(self, args: dict[str, Any]) -> dict[str, Any]:
        return apply_patch_tool(
            args,
            workspace=self.workspace,
            resolve_existing=self.resolve_existing,
            resolve_for_write=self.resolve_for_write,
            default_cwd_display=self.default_cwd_display,
            patch_lock=self.patch_lock,
            patch_committer=self.patch_committer,
            patch_baselines=self.patch_baselines,
        )

    def _execution_service(self) -> ExecutionService:
        return ExecutionService(
            registry=self.execution_registry,
            resolve_existing=self.resolve_existing,
            literal_directory_change=self._literal_directory_change,
            store_default_cwd=self._store_default_cwd,
            check_command_policy=self._check_command_policy,
            command_env=self._command_env,
            interactive_command_env=self._interactive_command_env,
            ensure_runtime_dirs=self._ensure_runtime_dirs,
            tmp_dir=lambda: self.tmp_dir,
            workspace_root=lambda: self.workspace.root,
            landlock_enabled=self.landlock_enabled,
            guard_allow_roots=guard_allow_roots,
            landlock_write_roots=self.landlock_write_roots,
            open_landlock_ruleset=open_landlock_ruleset,
            landlock_exec_argv=landlock_exec_argv,
            landlock_unavailable_warning=landlock_unavailable_warning,
            request_interactive_exec=request_interactive_exec,
            add_exec_diagnostics=self._add_exec_diagnostics,
            register_request_session=self._register_request_session,
            runtime_closed=lambda: self._closed,
            env_prefix=ENV_PREFIX,
        )

    def _register_request_session(self, session_id: str) -> None:
        request_id = getattr(self.request_context, "request_id", None)
        if isinstance(request_id, (str, int)) and not isinstance(request_id, bool):
            with self.request_sessions_lock:
                self.request_sessions[request_id] = session_id

    def exec_command(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._execution_service().execute(args)

    def _command_policy_service(self) -> CommandPolicy:
        return CommandPolicy(
            workspace=self.workspace,
            permission_granted=self._permission_granted,
            dangerously_skip_all_permissions=self.dangerously_skip_all_permissions,
            allow_network=self.allow_network,
            inline_script_allowed=self.capabilities.inline_script,
            shell_expansion_allowed=self.capabilities.shell_expansion,
            inline_script_permission=INLINE_SCRIPT_PERMISSION,
            is_filtered_env_var=is_filtered_env_var,
            is_allowed_tmp_path=self.is_allowed_command_tmp_path,
            is_allowed_external_executable=is_allowed_external_executable,
            special_device_paths=SPECIAL_DEVICE_PATHS,
        )

    def _check_command_policy(self, cmd: str, args: dict[str, Any]) -> None:
        self._command_policy_service().check(cmd, args)

    def _add_exec_diagnostics(self, payload: dict[str, Any], *, session: ExecSession | None = None) -> None:
        add_exec_diagnostics(
            payload,
            session=session,
            exec_output_diagnostics=exec_output_diagnostics,
        )

    def _check_command_paths(self, cmd: str) -> None:
        self._command_policy_service().check_paths(cmd)

    def _check_command_path_candidate(self, candidate: str) -> None:
        self._command_policy_service().check_path_candidate(candidate)

    def _reject_setuid_executable(self, executable: str) -> None:
        self._command_policy_service().reject_setuid_executable(executable)

    def _command_env(self, extra: Any) -> dict[str, str]:
        return command_env(
            extra,
            shell_env_policy=self.shell_env_policy,
            base_env=self._base_command_env,
            dangerously_skip_all_permissions=self.dangerously_skip_all_permissions,
            permission_granted=self._permission_granted,
            is_filtered_env_var=is_filtered_env_var,
            ecosystem_cache_env_names=ECOSYSTEM_CACHE_ENV_NAMES,
            env_pattern_matches=env_pattern_matches,
            ensure_runtime_dirs=self._ensure_runtime_dirs,
            command_tmp_dir=self.command_tmp_dir,
            command_home_dir=self.command_home_dir,
            cache_dir=lambda: self.cache_dir,
        )

    def _interactive_command_env(self, extra: Any) -> tuple[dict[str, str], dict[str, Any]]:
        return interactive_command_env(
            extra,
            shell_env_policy=self.shell_env_policy,
            dangerously_skip_all_permissions=self.dangerously_skip_all_permissions,
            permission_granted=self._permission_granted,
            is_filtered_env_var=is_filtered_env_var,
            windows_core_env_names=WINDOWS_CORE_ENV_NAMES,
        )

    def _exec_command_active_user(
        self,
        *,
        cmd: str,
        workdir: Path,
        args: dict[str, Any],
        timeout_ms: int,
        max_output_bytes: int,
    ) -> dict[str, Any]:
        return self._execution_service().execute_active_user(
            cmd=cmd,
            workdir=workdir,
            args=args,
            timeout_ms=timeout_ms,
            max_output_bytes=max_output_bytes,
        )

    def _git_tools(self) -> GitTools:
        return GitTools(
            workspace=self.workspace,
            default_cwd=lambda: self.default_cwd,
            resolve_existing=self.resolve_existing,
            resolve_for_write=self.resolve_for_write,
            git_path_filter=self.git_path_filter,
            command_env=self._command_env,
            require_git=require_git,
            patch_baselines=self.patch_baselines,
        )

    def _git_repo_scope(self, args: dict[str, Any]) -> tuple[Path | None, list[str]]:
        return self._git_tools().git_repo_scope(args)

    def _base_command_env(self) -> dict[str, str]:
        return base_command_env(
            self.shell_env_policy,
            is_core_command_env_name=is_core_command_env_name,
        )

    def _make_session(
        self,
        process: subprocess.Popen[bytes],
        *,
        command_preview: str = "",
        cwd: str = "",
        scratch_dir: str = "",
        timeout_at: float | None = None,
        warnings: list[str] | None = None,
        pty_master_fd: int | None = None,
    ) -> ExecSession:
        return self._execution_service().make_session(
            process,
            command_preview=command_preview,
            cwd=cwd,
            scratch_dir=scratch_dir,
            timeout_at=timeout_at,
            warnings=warnings or [],
            pty_master_fd=pty_master_fd,
        )

    def _remember_output_session(self, session: ExecSession) -> None:
        self.execution_registry._remember_output_session(session)

    def _cleanup_session_scratch(self, session: ExecSession) -> None:
        self.execution_registry._cleanup_session_scratch(session)

    def _retained_output_bytes_locked(self) -> int:
        return self.execution_registry._retained_output_bytes_locked()

    def _evict_retained_locked(self) -> None:
        self.execution_registry._evict_retained_locked()

    def _complete_session(self, session: ExecSession) -> None:
        self.execution_registry._complete_session(session)

    def _prune_sessions(self) -> None:
        self.execution_registry._prune_sessions()

    def _get_output_session(self, session_id: str) -> ExecSession:
        return self.execution_registry._get_output_session(session_id)

    def _format_session_output(self, session: ExecSession, payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry._format_session_output(session, payload, args)

    def _snapshot_session(
        self,
        session: ExecSession,
        args: dict[str, Any],
        max_output_bytes: int,
    ) -> dict[str, Any]:
        return self.execution_registry._snapshot_session(session, args, max_output_bytes)

    def _session_output_summary(self, session: ExecSession, payload: dict[str, Any]) -> str:
        return self.execution_registry._session_output_summary(session, payload)

    def read_output(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.read_output(args)

    def _session_metadata(self, session: ExecSession, *, include_process_tree: bool = False) -> dict[str, Any]:
        return self.execution_registry.session_metadata(
            session,
            include_process_tree=include_process_tree,
            redact_command=redact_for_trace,
        )

    def list_sessions(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.list_sessions(args, redact_command=redact_for_trace)

    def process_tree(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.process_tree(args)

    def kill_tree(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.kill_tree(args)

    def tail_output(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.tail_output(args)

    def find_output(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.find_output(args, truncate_line_chars=truncate_line_chars)

    def poll_session(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.poll_session(args)

    def write_stdin(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.write_stdin(args)

    def _session_has_new_output(self, session: ExecSession, args: dict[str, Any]) -> bool:
        return self.execution_registry._session_has_new_output(session, args)

    def _wait_for_session_exit(self, session: ExecSession, wait_seconds: float) -> bool:
        return self.execution_registry._wait_for_session_exit(session, wait_seconds)

    def kill_session(self, args: dict[str, Any]) -> dict[str, Any]:
        return self.execution_registry.kill_session(args)

    def cancel_session(self, session_id: str) -> None:
        self.execution_registry.cancel_session(session_id)

    def cancel_request(self, request_id: str | int) -> None:
        with self.request_sessions_lock:
            session_id = self.request_sessions.get(request_id)
        if session_id is not None:
            self.cancel_session(session_id)

    def _get_session(self, session_id: str) -> ExecSession:
        return self.execution_registry._get_session(session_id)

    def git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._git_tools().status(args)

    def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._git_tools().diff(args)

    def git_log(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._git_tools().log(args)

    def git_show(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._git_tools().show(args)

    def git_blame(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._git_tools().blame(args)

    def request_permissions(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._permission_service().request(args)

    def request_elevated_action(self, args: dict[str, Any]) -> dict[str, Any]:
        action = str(args.get("action", ""))
        timeout_ms = int(args.get("timeout_ms", 300000))
        result = request_elevated_action(action, timeout_seconds=timeout_ms / 1000.0)
        return {
            "action": action,
            "approved": True,
            "exit_code": result.get("exit_code", 0),
            "message": result.get("message", "Elevated action completed."),
            "request_id": result.get("request_id"),
        }

    def view_image(self, args: dict[str, Any]) -> dict[str, Any]:
        return view_image_tool(args, resolve_existing=self.resolve_existing)

__all__ = ["Runtime"]
