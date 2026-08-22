from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import html
import fnmatch
import functools
import http.server
import json
import os
import posixpath
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from . import __version__
from .command_policy import (
    CommandPolicy,
    DESTRUCTIVE_RE,
    ENV_FLAG_OPTIONS,
    ENV_LONG_OPTIONS_WITH_ARGUMENT,
    ENV_LONG_OPTIONS_WITH_OPTIONAL_ARGUMENT,
    ENV_OPTIONS_WITH_ARGUMENT,
    ENV_SHORT_OPTIONS_WITH_ATTACHED_ARGUMENT,
    HEREDOC_TOKENS,
    NETWORK_LITERAL_COMMANDS,
    NETWORK_RE,
    PATH_ARGUMENT_COMMANDS,
    PATTERN_THEN_PATH_COMMANDS,
    REDIRECTION_TOKENS,
    SCRIPT_COMMANDS,
    SHELL_EXPANSION_RE,
    SHELL_CONTROL_TOKENS,
    command_argument_path_candidates,
    command_executables,
    env_split_command,
    env_wrapped_command,
    explicit_command_path_candidates,
    find_command_path_candidates,
    inline_script_command,
    inline_script_segment,
    is_env_assignment_token,
    is_inspectable_path_argument,
    is_literal_network_reference_command,
    parse_heredoc_delimiter,
    pattern_command_path_candidates,
    script_command_path_candidates,
    shlex_split,
    stdin_script_segment,
    strip_heredoc_payloads,
)
from .envutils import ENV_PREFIX, truthy_env
from .errors import JsonRpcError, ToolFailure
from .elevated_actions import ELEVATED_ACTIONS, request_elevated_action, request_permission_approval
from .execution import ExecutionService, MAX_ACTIVE_EXEC_SESSIONS
from .interactive_exec import (
    interactive_broker_status,
    interactive_queue_path,
    request_computer_use,
    request_human_help,
    request_interactive_exec,
)
from .landlock_exec import libc_syscall
from .oauth import (
    OAUTH_CODE_TTL_SECONDS,
    OAUTH_GRANT_TYPE_AUTHORIZATION_CODE,
    OAUTH_GRANT_TYPE_REFRESH_TOKEN,
    OAUTH_GRANT_TYPES_SUPPORTED,
    OAUTH_MAX_BODY_BYTES,
    OAUTH_RESPONSE_TYPES_SUPPORTED,
    OAUTH_TOKEN_TTL_SECONDS,
    OAuthConfig,
    OAuthClientRegistry,
    OAuthStateStore,
    access_token_client_id,
    create_access_token,
    valid_pkce_challenge,
    validate_access_token,
    verify_pkce,
)
from .patching import AtomicPatchCommitter
from .processes import (
    HARD_KILL_SIGNAL,
    SESSION_BUFFER_BYTES,
    ExecSession,
    process_group_popen_kwargs,
    process_tree_for_pid,
    truncate_output_bytes_tail,
)
from .session_store import (
    COMPLETED_SESSION_TTL_SECONDS,
    MAX_RETAINED_OUTPUT_SESSIONS,
    MAX_RUNTIME_OUTPUT_BYTES,
    ExecutionRegistry,
    PermissionGrant,
    read_output_action,
)
from .protocol import (
    PROTOCOL_VERSION,
    STATELESS_PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    dispatch_rpc,
    jsonrpc_error,
    protocol_version_is_supported,
    response_id,
    validate_rpc_envelope,
)
from .project_context import ProjectContext, load_project_context
from .telemetry import SessionTelemetry
from .textutils import TextTruncation
from .tools.diagnostics import (
    discover_tools,
    exec_environment_summary,
    execution_session_summary,
    landlock_enforced,
    skill_catalog,
)
from .tools.desktop import desktop_ui_action, human_help_tool
from .tools.filesystem import (
    GREP_MAX_LINE_CHARS,
    entry_for_path,
    file_entry,
    find_literal,
    list_dir_tool,
    list_files_tool,
    matches_any_glob,
    path_batches,
    read_file_tool,
    search_match_item,
    search_text_tool,
    sort_value,
    truncate_line_chars,
    truncation_fields,
    walk_files,
)
from .tools.git_tools import (
    GitTools,
    parse_branch_line,
    parse_diff_files,
    parse_git_blame_porcelain,
    validate_git_ref,
)
from .tools.images import (
    identify_image,
    identify_jpeg_size,
    identify_webp_size,
    resize_image_bytes,
    should_resize_image,
    view_image_tool,
)
from .tools.patch_tools import apply_patch_tool
from .tool_catalog import (
    PUBLIC_TOOL_NAMES,
    TOOL_REGISTRY,
    ToolSpec,
    tool_annotations,
    tool_definition,
)
from .tool_results import make_tool_result
from .tool_schemas import (
    IMAGE_RESIZE_MAX_DIMENSION,
    INLINE_SCRIPT_PERMISSION,
    computer_use_action_contract,
    input_schemas,
    object_schema,
    schema_type_matches,
    schema_type_name,
    tool_output_schema,
    validate_arguments,
    validate_schema_value,
)
from .transport_http import HTTPSessionManager, SessionCapacityError
from .transport_stdio import serve_stdio
from .workspace import (
    DEFAULT_EXCLUDED_NAMES,
    WORKSPACE_ALLOWLIST_ENV,
    ResolvedPath,
    Workspace,
    WorkspaceEntry,
    is_relative_to,
    normalize_rel_display,
    validate_workspace_selection,
    workspace_allowlist_from_env,
    workspace_catalog_from_env,
    workspace_entry_for_selector,
)


SERVER_NAME = "coding-tools-mcp"
SERVER_TITLE = "Coding Tools MCP"
MCP_ENDPOINT_PATH = "/mcp"
SENSITIVE_ENV_RE = re.compile(r"(token|secret|credential|api[_-]?key|password|passwd|private)", re.I)
SENSITIVE_VALUE_RE = re.compile(
    r"(COMPLIANCE_SHOULD_NOT_LEAK|-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})"
)
ACTIVITY_INLINE_SECRET_RE = re.compile(
    r"(?i)(\b(?:password|passwd|token|secret|api[_-]?key|authorization|cookie|key\s+content)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)"
)
ACTIVITY_BEARER_RE = re.compile(r"(?i)\bBearer\s+\S+")
ACTIVITY_LONG_VALUE_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{96,}={0,2}(?![A-Za-z0-9+/])")
ACTIVITY_REQUEST_BASE64_RE = re.compile(r"(?i)(--request-base64\s+)\S+")
ACTIVITY_LOG_RETENTION_DAYS = 7
ACTIVITY_LOG_LOCK = threading.Lock()
ACTIVITY_LOG_PATH = (
    Path(
        os.environ.get(
            f"{ENV_PREFIX}_ACTIVITY_LOG",
            r"C:\ProgramData\WebGPTCodingToolsMCPService\logs\ai-activity.log",
        )
    )
    if os.name == "nt"
    else None
)
RISKY_ENV_NAMES = {
    "BASH_ENV",
    "ENV",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "NODE_OPTIONS",
    "PERL5LIB",
    "PERL5OPT",
    "RUBYOPT",
    "RUBYLIB",
}
SHELL_ENV_INHERIT_CHOICES = ("core", "all", "none")
EXECUTABLE_ALLOWLIST_ENV = f"{ENV_PREFIX}_EXECUTABLE_ALLOWLIST"


@functools.cache
def runtime_build_identity() -> dict[str, Any]:
    path = Path(__file__).with_name("build-identity.json")
    payload: dict[str, Any] = {
        "package_version": __version__,
        "display_version": __version__,
        "git_sha": None,
        "dirty": None,
        "build_id": None,
    }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload.update({key: loaded.get(key) for key in payload if key in loaded})
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return payload


def runtime_version() -> str:
    return str(runtime_build_identity().get("display_version") or __version__)


def summarize_exception(exc: BaseException) -> tuple[str, list[str]]:
    """Expose useful leaf errors instead of opaque ExceptionGroup/TaskGroup text."""

    leaves: list[str] = []

    def collect(current: BaseException) -> None:
        if isinstance(current, BaseExceptionGroup):
            for child in current.exceptions:
                collect(child)
            return
        message = str(current).strip() or current.__class__.__name__
        leaves.append(f"{current.__class__.__name__}: {message}")

    collect(exc)
    unique: list[str] = []
    for leaf in leaves:
        if leaf not in unique:
            unique.append(leaf)
    if not unique:
        unique = [f"{exc.__class__.__name__}: {str(exc).strip() or 'unknown error'}"]
    summary = unique[0] if len(unique) == 1 else " | ".join(unique[:4])
    return summary, unique[:16]


def configured_executable_allowlist() -> tuple[str, ...]:
    """Return explicit external executable names/paths trusted by this instance."""
    raw = (os.environ.get(EXECUTABLE_ALLOWLIST_ENV) or "").strip()
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(os.pathsep) if item.strip())


def is_allowed_external_executable(candidate: str) -> bool:
    candidate_path = Path(candidate).expanduser()
    try:
        resolved = candidate_path.resolve(strict=True)
    except OSError:
        resolved = candidate_path
    normalized = os.path.normcase(str(resolved))
    name = resolved.name.casefold()
    for entry in configured_executable_allowlist():
        entry_path = Path(entry).expanduser()
        if entry_path.is_absolute() or re.match(r"^[A-Za-z]:[\\/]", entry):
            try:
                entry_normalized = os.path.normcase(str(entry_path.resolve(strict=False)))
            except OSError:
                entry_normalized = os.path.normcase(str(entry_path))
            if normalized == entry_normalized:
                return True
        elif name == Path(entry).name.casefold():
            return True
    return False


@dataclass(frozen=True)
class ModeCapabilities:
    """What a permission mode allows. Gates consult this instead of comparing mode strings."""

    network: bool
    shell_expansion: bool
    inline_script: bool
    landlock: bool
    secret_env_filter: bool
    global_tmp_write: str  # "blocked" | "tmp-prefix" | "allowed"
    skip_all_permissions: bool


PERMISSION_MODE_CAPABILITIES: dict[str, ModeCapabilities] = {
    "safe": ModeCapabilities(
        network=False,
        shell_expansion=False,
        inline_script=False,
        landlock=True,
        secret_env_filter=True,
        global_tmp_write="blocked",
        skip_all_permissions=False,
    ),
    "trusted": ModeCapabilities(
        network=True,
        shell_expansion=True,
        inline_script=True,
        landlock=True,
        secret_env_filter=True,
        global_tmp_write="tmp-prefix",
        skip_all_permissions=False,
    ),
    "dangerous": ModeCapabilities(
        network=True,
        shell_expansion=True,
        inline_script=True,
        landlock=False,
        secret_env_filter=False,
        global_tmp_write="allowed",
        skip_all_permissions=True,
    ),
}
PERMISSION_MODE_CHOICES = tuple(PERMISSION_MODE_CAPABILITIES)
# Documented kill_session status enum; guarded by test_schema_drift.
KILL_SESSION_STATUSES = ("terminated", "killed", "exited", "terminating", "not_found")
POSIX_CORE_ENV_NAMES = {"PATH", "LANG", "LC_ALL", "TERM"}
# Not POSIX core, but inherited under inherit="core" so git helper subprocesses and
# exec_command share the host's global git config (e.g. safe.directory entries).
GIT_ENV_NAMES = {"GIT_CONFIG_GLOBAL"}
WINDOWS_CORE_ENV_NAMES = {
    "PATH",
    "PATHEXT",
    "COMSPEC",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "WINDIR",
    # Common Windows developer tools resolve SDK/config roots through these
    # variables even when their executable was found through PATH. They are
    # machine-level locations, not user secrets.
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PROGRAMDATA",
    "ALLUSERSPROFILE",
}
LITERAL_DIRECTORY_CHANGE_RE = re.compile(
    r"^\s*(?:cd|chdir|set-location|sl)\s+"
    r"(?:(?:/d|-literalpath|-path)\s+)?"
    r'''(?:"([^"\r\n]+)"|'([^'\r\n]+)'|([^;&|\r\n]+?))\s*$''',
    re.I,
)
MAX_HTTP_REQUEST_BYTES = 1_048_576
RUNTIME_ROOT_DIR_NAME = "coding-tools-mcp"
SPECIAL_DEVICE_PATHS = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")
DNS_RESOLVER_READ_ROOTS = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/gai.conf",
    "/etc/protocols",
    "/etc/services",
    "/run/systemd/resolve",
    "/run/resolvconf",
)
TOOLCHAIN_READ_ROOTS = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/etc/alternatives",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
    "/etc/localtime",
    "/etc/npmrc",
    "/usr/local/sdkman/candidates",
)
OS_METADATA_READ_FILES = (
    "/etc/debian_version",
    "/etc/os-release",
    "/etc/lsb-release",
)
GIT_READ_ROOTS = (
    "/etc/gitconfig",
    "/etc/gitconfig.d",
)
SYSTEM_PATH_ROOT_PREFIXES = (
    "/bin",
    "/sbin",
    "/usr",
    "/lib",
    "/lib64",
    "/etc/alternatives",
    "/usr/local/sdkman/candidates",
)
ECOSYSTEM_CACHE_ENV_NAMES = {
    "MAVEN_USER_HOME",
    "GRADLE_USER_HOME",
    "NPM_CONFIG_CACHE",
    "npm_config_cache",
    "PIP_CACHE_DIR",
    "GOCACHE",
    "GOMODCACHE",
    "CARGO_HOME",
    "RUSTUP_HOME",
}

@dataclass(frozen=True)
class ShellEnvPolicy:
    inherit: str = "core"
    include_only: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    set: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimePolicy:
    permission_mode: str
    shell_env_policy: ShellEnvPolicy
    allow_network: bool
    fake_readonly_annotations: bool = False


OAUTH_TOKEN_AUTH_METHODS = ("client_secret_basic", "client_secret_post", "none")


def _http_base_for_bind_host(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _write_http_body_safely(handler: Any, body: bytes) -> bool:
    """Treat a client disappearing mid-response as a normal disconnect."""
    try:
        handler.wfile.write(body)
        return True
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        handler.close_connection = True
        return False


def _first_header_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def _first_form_value(params: dict[str, list[str]], key: str) -> str:
    values = params.get(key)
    return values[0] if values else ""


def _forwarded_header_param(value: str | None, name: str) -> str:
    first = _first_header_value(value)
    for part in first.split(";"):
        key, sep, raw = part.strip().partition("=")
        if sep and key.lower() == name:
            return raw.strip().strip('"')
    return ""


def _safe_external_host(host: str) -> str:
    host = host.strip()
    if not host or any(ch.isspace() or ch in "/\\@?#" for ch in host):
        return ""
    try:
        parsed = urllib.parse.urlsplit(f"//{host}")
        _ = parsed.port
    except ValueError:
        return ""
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        return ""
    return host


def env_pattern_matches(name: str, patterns: tuple[str, ...]) -> bool:
    upper_name = name.upper()
    return any(fnmatch.fnmatchcase(upper_name, pattern.upper()) for pattern in patterns)


def is_risky_env_name(name: str) -> bool:
    upper = name.upper()
    return upper in RISKY_ENV_NAMES or upper.startswith("DYLD_")


def is_filtered_env_var(name: str, value: str) -> bool:
    return bool(SENSITIVE_ENV_RE.search(name) or is_risky_env_name(name) or SENSITIVE_VALUE_RE.search(value))


def is_core_command_env_name(name: str) -> bool:
    upper = name.upper()
    if os.name == "nt":
        return upper in WINDOWS_CORE_ENV_NAMES
    return upper in POSIX_CORE_ENV_NAMES or upper in GIT_ENV_NAMES or upper.startswith("LC_")


def split_env_patterns(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_shell_env_set(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ENV_PREFIX}_SHELL_ENV_SET must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{ENV_PREFIX}_SHELL_ENV_SET must be a JSON object")
    return {str(key): str(item) for key, item in parsed.items()}


def env_int(name: str, fallback: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


def configured_runtime_root() -> Path | None:
    configured = os.environ.get(f"{ENV_PREFIX}_RUNTIME_ROOT") or ""
    if not configured.strip():
        return None
    return Path(configured).expanduser()


def runtime_parent_root() -> Path:
    return configured_runtime_root() or Path(tempfile.gettempdir()) / RUNTIME_ROOT_DIR_NAME


def runtime_parent_fallback_root() -> Path | None:
    if configured_runtime_root() is not None:
        return None
    if os.name == "nt":
        return None
    fallback = Path("/tmp") / RUNTIME_ROOT_DIR_NAME
    if fallback == runtime_parent_root():
        return None
    return fallback


def workspace_runtime_hash(workspace: Path) -> str:
    resolved = workspace.expanduser().resolve(strict=False)
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]


def runtime_dir_for_workspace(workspace: Path, instance_id: str) -> Path:
    root = runtime_parent_root()
    try:
        root_in_workspace = is_relative_to(root.resolve(strict=False), workspace.expanduser().resolve(strict=False))
    except OSError:
        root_in_workspace = False
    if root_in_workspace:
        if configured_runtime_root() is not None:
            raise ToolFailure(
                "INVALID_ARGUMENT",
                f"{ENV_PREFIX}_RUNTIME_ROOT must be outside the configured workspace.",
                category="validation",
            )
        root = runtime_parent_fallback_root() or root
    return root / workspace_runtime_hash(workspace) / instance_id


def fallback_runtime_dir_for_workspace(workspace: Path, instance_id: str) -> Path | None:
    fallback = runtime_parent_fallback_root()
    if fallback is None:
        return None
    return fallback / workspace_runtime_hash(workspace) / instance_id


def shell_env_policy_from_args(args: argparse.Namespace) -> ShellEnvPolicy:
    raw_inherit = args.shell_env_inherit or os.environ.get(f"{ENV_PREFIX}_SHELL_ENV_INHERIT") or "core"
    inherit = raw_inherit.strip().lower()
    if inherit not in SHELL_ENV_INHERIT_CHOICES:
        supported = ", ".join(SHELL_ENV_INHERIT_CHOICES)
        raise ValueError(f"shell env inherit must be one of: {supported}")
    return ShellEnvPolicy(
        inherit=inherit,
        include_only=split_env_patterns(os.environ.get(f"{ENV_PREFIX}_SHELL_ENV_INCLUDE_ONLY")),
        exclude=split_env_patterns(os.environ.get(f"{ENV_PREFIX}_SHELL_ENV_EXCLUDE")),
        set=parse_shell_env_set(os.environ.get(f"{ENV_PREFIX}_SHELL_ENV_SET")),
    )


def permission_mode_from_args(args: argparse.Namespace) -> str:
    skip_all = bool(getattr(args, "dangerously_skip_all_permissions", False)) or truthy_env(
        os.environ.get(f"{ENV_PREFIX}_DANGEROUSLY_SKIP_ALL_PERMISSIONS")
    )
    raw_mode = (
        getattr(args, "permission_mode", None)
        or os.environ.get(f"{ENV_PREFIX}_PERMISSION_MODE")
        or ("dangerous" if skip_all else "safe")
    )
    mode = raw_mode.strip().lower()
    if mode not in PERMISSION_MODE_CHOICES:
        supported = ", ".join(PERMISSION_MODE_CHOICES)
        raise ValueError(f"permission mode must be one of: {supported}")
    return "dangerous" if skip_all else mode


def fake_readonly_annotations_from_args(args: argparse.Namespace, permission_mode: str) -> bool:
    requested = bool(getattr(args, "dangerously_fake_readonly_annotations", False)) or truthy_env(
        os.environ.get(f"{ENV_PREFIX}_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS")
    )
    if requested and permission_mode != "dangerous":
        raise ValueError(
            "--dangerously-fake-readonly-annotations requires --permission-mode dangerous"
        )
    return requested


def runtime_policy_from_args(args: argparse.Namespace) -> RuntimePolicy:
    permission_mode = permission_mode_from_args(args)
    allow_network = (
        PERMISSION_MODE_CAPABILITIES[permission_mode].network
        or bool(getattr(args, "allow_network", False))
        or truthy_env(os.environ.get(f"{ENV_PREFIX}_ALLOW_NETWORK"))
    )
    return RuntimePolicy(
        permission_mode=permission_mode,
        shell_env_policy=shell_env_policy_from_args(args),
        allow_network=allow_network,
        fake_readonly_annotations=fake_readonly_annotations_from_args(args, permission_mode),
    )


LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
LANDLOCK_ACCESS_FS_REFER = 1 << 13
LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14
LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15


def json_response_payload(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@functools.lru_cache(maxsize=8)
def _configured_allowed_origins(raw: str) -> frozenset[str]:
    return frozenset(item.strip().rstrip("/") for item in raw.split(",") if item.strip())


def is_allowed_origin(origin: str) -> bool:
    # Authentication does not replace browser Origin validation.
    try:
        parsed = urllib.parse.urlparse(origin)
    except ValueError:
        return False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    normalized = origin.rstrip("/")
    configured = _configured_allowed_origins(os.environ.get(f"{ENV_PREFIX}_ALLOWED_ORIGINS", ""))
    return parsed.hostname in {"localhost", "127.0.0.1", "::1"} or normalized in configured


def is_loopback_bind_host(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "::1", ""}


_TOOL_PATHS: dict[str, str] = {}


def configured_tool_path(name: str) -> str | None:
    env_name = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    raw = (os.environ.get(f"{ENV_PREFIX}_{env_name}_PATH") or "").strip()
    if raw and Path(raw).is_file():
        return raw
    return None


def cached_which(*names: str) -> str | None:
    """shutil.which with a success-only cache: absence keeps re-probing so a
    tool installed mid-session is still picked up."""
    cached = _TOOL_PATHS.get(names[0])
    if cached:
        return cached
    for name in names:
        path = configured_tool_path(name) or shutil.which(name)
        if path:
            _TOOL_PATHS[names[0]] = path
            return path
    return None


def landlock_unavailable_warning(exc: ToolFailure) -> str:
    reason = ""
    details = getattr(exc, "details", None)
    if isinstance(details, dict) and details.get("reason"):
        reason = f" ({details['reason']})"
    return (
        "Linux Landlock filesystem confinement is unavailable on this host"
        f"{reason}; exec_command ran with policy checks only. "
        "Use an external sandbox before running untrusted commands."
    )


def landlock_status_payload() -> dict[str, Any]:
    try:
        version = landlock_abi_version()
    except ToolFailure as exc:
        return {
            "available": False,
            "abi_version": None,
            "reason": exc.message,
            "details": exc.details,
        }
    return {
        "available": True,
        "abi_version": version,
    }


def truncate_evidence(text: str, limit: int = 240) -> str:
    text = " ".join(text.strip().split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def diagnostic(
    code: str,
    *,
    evidence: str = "",
    severity: str = "error",
    suggested_fix: str | None = None,
    suggested_next_command: str | None = None,
    suggested_server_flag: str | None = None,
) -> dict[str, str]:
    item = {"code": code, "severity": severity}
    if evidence:
        item["evidence"] = truncate_evidence(evidence)
    if suggested_fix:
        item["suggested_fix"] = suggested_fix
    if suggested_next_command:
        item["suggested_next_command"] = suggested_next_command
    if suggested_server_flag:
        item["suggested_server_flag"] = suggested_server_flag
    return item


PERMISSION_FAILURE_DIAGNOSTICS: dict[str, dict[str, str]] = {
    "network": {
        "code": "NETWORK_PERMISSION_REQUIRED",
        "suggested_fix": "Call request_permissions for this exact operation, or switch the local service to trusted mode.",
        "suggested_server_flag": "--permission-mode trusted",
    },
    "shell_expansion": {
        "code": "SHELL_EXPANSION_PERMISSION_REQUIRED",
        "suggested_fix": "Call request_permissions for this exact operation, or switch the local service to trusted mode.",
        "suggested_server_flag": "--permission-mode trusted",
    },
    INLINE_SCRIPT_PERMISSION: {
        "code": "INLINE_SCRIPT_PERMISSION_REQUIRED",
        "suggested_fix": "Call request_permissions for this exact operation, or switch the local service to trusted mode.",
        "suggested_server_flag": "--permission-mode trusted",
    },
    "sensitive_env": {
        "code": "SECRET_ENV_REJECTED",
        "suggested_fix": "Call request_permissions for this exact operation, or remove secret-looking environment variables.",
    },
}


def permission_failure_diagnostics(exc: ToolFailure) -> list[dict[str, str]]:
    spec = PERMISSION_FAILURE_DIAGNOSTICS.get(str(exc.details.get("permission") or ""))
    if spec is None:
        return []
    return [
        diagnostic(
            spec["code"],
            evidence=exc.message,
            suggested_fix=spec["suggested_fix"],
            suggested_server_flag=spec.get("suggested_server_flag"),
        )
    ]


def structured_error_kind(code: str, category: str, message: str = "") -> str:
    text = f"{code} {message}".lower()
    if code in {
        "PERMISSION_REQUIRED",
        "ABSOLUTE_PATH_DENIED",
        "PATH_OUTSIDE_WORKSPACE",
        "SYMLINK_ESCAPE",
        "WORKSPACE_NOT_ALLOWED",
        "ELEVATED_SCRIPT_HASH_MISMATCH",
        "ELEVATED_SCRIPT_NOT_FOUND",
        "ELEVATION_REQUEST_INVALID",
    }:
        return "policy_denied"
    if code in {"ELEVATED_ACTION_NOT_ALLOWED", "ELEVATION_REQUIRED", "UAC_USER_DENIED"}:
        return "elevation_required"
    if code in {
        "ELEVATION_BROKER_UNAVAILABLE",
        "ELEVATION_QUEUE_UNAVAILABLE",
        "ELEVATION_RESPONSE_INVALID",
        "ELEVATION_BROKER_ERROR",
    }:
        return "uac_unavailable"
    if code in {"ELEVATION_TIMEOUT", "ELEVATION_REQUEST_EXPIRED", "COMMAND_TIMED_OUT", "TIMEOUT"} or "timed out" in text:
        return "timeout"
    if code in {"POWERSHELL_NOT_FOUND", "EXECUTABLE_NOT_FOUND", "TOOL_NOT_FOUND"}:
        return "tool_not_found"
    if code in {"MCP_SESSION_NOT_FOUND", "SESSION_NOT_FOUND", "SESSION_CLOSED"} or category == "connector":
        return "connector_failure"
    if code in {"PROCESS_EXIT", "COMMAND_FAILED", "ELEVATED_ACTION_FAILED"}:
        return "process_exit"
    return "runtime_error"


def exec_output_diagnostics(payload: dict[str, Any]) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    stdout = str(payload.get("stdout", ""))
    stderr = str(payload.get("stderr", ""))
    diagnostic_output = str(payload.get("_diagnostic_output", ""))
    combined = "\n".join(part for part in (stderr, stdout, diagnostic_output) if part)
    lower = combined.lower()
    if payload.get("timed_out") or payload.get("status") == "timeout":
        diagnostics.append(
            diagnostic(
                "COMMAND_TIMED_OUT",
                evidence="command timed out",
                suggested_fix="Increase timeout_ms only for trusted workloads, or run a narrower command.",
            )
        )
    if payload.get("truncated") or payload.get("stdout_truncated") or payload.get("stderr_truncated"):
        diagnostics.append(
            diagnostic(
                "OUTPUT_TRUNCATED",
                evidence="stdout/stderr exceeded max_output_bytes or session buffer limits",
                severity="warning",
                suggested_fix="Increase max_output_bytes or poll the running session more frequently.",
            )
        )
    if "/dev/null" in lower and "permission denied" in lower:
        diagnostics.append(
            diagnostic(
                "DEV_NULL_DENIED",
                evidence=combined,
                suggested_fix="Landlock special device rules should include WRITE_FILE, TRUNCATE, and IOCTL_DEV for /dev/null.",
            )
        )
    if "could not resolve host" in lower or "temporary failure in name resolution" in lower or "name or service not known" in lower:
        diagnostics.append(
            diagnostic(
                "DNS_RESOLUTION_FAILED",
                evidence=combined,
                suggested_next_command="cat /etc/resolv.conf && getent hosts repo.maven.apache.org",
            )
        )
    if "java.security" in lower and ("permission denied" in lower or "could not" in lower or "error loading" in lower):
        diagnostics.append(
            diagnostic(
                "JDK_SECURITY_CONFIG_BLOCKED",
                evidence=combined,
                suggested_fix="Ensure the JDK security configuration path is included in Landlock read roots.",
            )
        )
    if "tmpdir" in lower and ("permission denied" in lower or "not writable" in lower or "cannot write" in lower):
        diagnostics.append(
            diagnostic(
                "TMPDIR_NOT_WRITABLE",
                evidence=combined,
                suggested_next_command="printf ok > \"$TMPDIR/coding-tools-write-test\"",
            )
        )
    home_error_terms = ("permission denied", "not writable", "cannot write", "eacces")
    home_path_error = any(
        re.search(r"(?:\.coding-tools/home|/home(?:/|[\"'\s]|$))", line)
        and any(term in line for term in home_error_terms)
        for line in lower.splitlines()
    )
    home_error = (
        "$home" in lower
        or "home=" in lower
        or re.search(r"\bhome directory\b", lower)
        or "cannot write to home" in lower
        or re.search(r"not writable:\s+\S*home", lower)
        or re.search(r"permission denied:\s+\S*home", lower)
        or home_path_error
    )
    if home_error and any(term in lower for term in home_error_terms):
        diagnostics.append(
            diagnostic(
                "HOME_NOT_WRITABLE",
                evidence=combined,
                suggested_next_command="printf ok > \"$HOME/coding-tools-write-test\"",
            )
        )
    if "permission denied" in lower and any(root in combined for root in ("/usr", "/bin", "/lib", "/etc", "/usr/local/sdkman")):
        diagnostics.append(
            diagnostic(
                "LANDLOCK_READ_ROOT_BLOCKED",
                evidence=combined,
                suggested_fix="Add the missing toolchain path to CODING_TOOLS_MCP_EXEC_ALLOW_ROOTS or the default read roots.",
            )
        )
    if (
        payload.get("exit_code") == 127
        or "command not found" in lower
        or ("not found" in lower and "exec" in lower)
        or "is not recognized as the name of a cmdlet" in lower
        or "commandnotfoundexception" in lower
        or "the term '" in lower and "is not recognized" in lower
    ):
        diagnostics.append(
            diagnostic(
                "EXECUTABLE_NOT_FOUND",
                evidence=combined or "exit_code=127",
                suggested_next_command="command -v <executable>",
            )
        )
    return diagnostics


class SlidingWindowRateLimiter:
    """Small process-local limiter for a single public personal endpoint."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self.rejected = 0

    def allow(self, key: str, *, limit: int, window_seconds: float) -> bool:
        now = time.time()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                self.rejected += 1
                return False
            events.append(now)
            if len(self._events) > 1024:
                self._events = {
                    name: values for name, values in self._events.items() if values and values[-1] > cutoff
                }
            return True


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

    @staticmethod
    def _permission_arguments_digest(arguments: dict[str, Any]) -> str:
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _permission_granted(self, permission: str) -> bool:
        if self.dangerously_skip_all_permissions:
            return True
        claimed = getattr(self.request_context, "claimed_permission_grants", None)
        if isinstance(claimed, set) and permission in claimed:
            return True
        tool_name = str(getattr(self.request_context, "tool_name", ""))
        arguments = getattr(self.request_context, "arguments", {})
        if not tool_name or not isinstance(arguments, dict):
            return False
        owner = self._permission_owner()
        workspace = os.path.normcase(str(self.workspace.root))
        digest = self._permission_arguments_digest(arguments)
        now = time.time()
        matched: PermissionGrant | None = None
        matched_id: str | None = None
        with self.execution_registry.state_lock:
            expired = [grant_id for grant_id, grant in self.execution_registry.permission_grants.items() if grant.expires_at <= now]
            for grant_id in expired:
                self.execution_registry.permission_grants.pop(grant_id, None)
            for grant_id, grant in self.execution_registry.permission_grants.items():
                if (
                    grant.owner == owner
                    and grant.workspace == workspace
                    and grant.tool_name == tool_name
                    and grant.permission == permission
                    and (grant.scope == "session" or grant.arguments_digest == digest)
                ):
                    matched = grant
                    matched_id = grant_id
                    break
            if matched is not None and matched.scope == "once" and matched_id is not None:
                self.execution_registry.permission_grants.pop(matched_id, None)
        if matched is None:
            return False
        if matched.scope == "once":
            claimed = getattr(self.request_context, "claimed_permission_grants", None)
            if not isinstance(claimed, set):
                claimed = set()
                self.request_context.claimed_permission_grants = claimed
            claimed.add(permission)
        return True

    def _finish_permission_grants(self) -> None:
        self.request_context.claimed_permission_grants = set()

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
        return {
            "server": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": runtime_version(),
            "build_identity": runtime_build_identity(),
            "protocol_version": self.protocol_version,
            **self._exec_environment_summary(),
            "workspace_allowlist": [
                {"name": entry.name, "path": str(entry.path)}
                for entry in workspace_catalog_from_env()
            ],
            "default_cwd": self.default_cwd_display(),
            "default_cwd_scope": "oauth_owner_workspace" if self.state_owner else "mcp_session",
            "auth_enabled": self.auth_enabled(),
            "oauth": {
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
            "dangerously_skip_all_permissions": self.dangerously_skip_all_permissions,
            "annotation_override": "fake_readonly" if self.fake_readonly_annotations else None,
            "landlock": landlock,
            "exec_policy": {
                "shell_expansion": self.shell_expansion_policy(),
                "inline_script": self.inline_script_policy(),
                "global_tmp_write": self.global_tmp_write_policy(),
                "secret_env_filter": self.secret_env_filter_policy(),
            },
            "permission_elicitation_supported": True,
            "permission_approval_transport": "local_windows_broker",
            "shell_env_inherit": self.shell_env_policy.inherit,
            "shell_env_include_only": list(self.shell_env_policy.include_only),
            "shell_env_exclude": list(self.shell_env_policy.exclude),
            "endpoint_path": MCP_ENDPOINT_PATH,
            "project_context": {
                "root_instruction_files": [item.path for item in self.project_context.root_files],
                "nested_instruction_files": list(self.project_context.nested_files),
                "warnings": list(self.project_context.warnings),
            },
            "skills": self._skill_catalog(),
            "http_sessions": http_session_stats,
            "execution": self._execution_session_summary(),
            "tools": tools,
            "tool_count": len(tools),
        }

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
        if session is not None and not payload.get("stdout") and not payload.get("stderr"):
            retained = session.retained_output_bytes().decode("utf-8", errors="replace")
            if retained:
                payload["_diagnostic_output"] = retained
        diagnostics = exec_output_diagnostics(payload)
        payload.pop("_diagnostic_output", None)
        if diagnostics:
            payload["diagnostics"] = diagnostics
        if payload.get("timed_out") or payload.get("status") == "timeout":
            payload["error_kind"] = "timeout"
        elif payload.get("exit_code") not in (None, 0):
            diagnostic_codes = {str(item.get("code")) for item in diagnostics}
            payload["error_kind"] = (
                "tool_not_found" if "EXECUTABLE_NOT_FOUND" in diagnostic_codes else "process_exit"
            )
            payload["process_error"] = {
                "kind": payload["error_kind"],
                "exit_code": payload.get("exit_code"),
                "signal": payload.get("signal"),
            }

    def _check_command_paths(self, cmd: str) -> None:
        self._command_policy_service().check_paths(cmd)

    def _check_command_path_candidate(self, candidate: str) -> None:
        self._command_policy_service().check_path_candidate(candidate)

    def _reject_setuid_executable(self, executable: str) -> None:
        self._command_policy_service().reject_setuid_executable(executable)

    def _command_env(self, extra: Any) -> dict[str, str]:
        env = self._base_command_env()
        filter_sensitive = not self.dangerously_skip_all_permissions and not self._permission_granted("sensitive_env")
        if filter_sensitive:
            env = {key: value for key, value in env.items() if not is_filtered_env_var(key, value)}
            env = {key: value for key, value in env.items() if key not in ECOSYSTEM_CACHE_ENV_NAMES}
        if self.shell_env_policy.exclude:
            env = {
                key: value
                for key, value in env.items()
                if not env_pattern_matches(key, self.shell_env_policy.exclude)
            }
        if self.shell_env_policy.include_only:
            env = {
                key: value
                for key, value in env.items()
                if env_pattern_matches(key, self.shell_env_policy.include_only)
            }
        env.update({str(key): str(value) for key, value in self.shell_env_policy.set.items()})
        self._ensure_runtime_dirs()
        tmp_dir = self.command_tmp_dir()
        env["HOME"] = str(self.command_home_dir())
        env["TMPDIR"] = str(tmp_dir)
        if os.name == "nt":
            home_dir = self.command_home_dir()
            appdata_dir = home_dir / "AppData" / "Roaming"
            localappdata_dir = home_dir / "AppData" / "Local"
            nuget_packages_dir = self.cache_dir / "nuget" / "packages"
            for path in (appdata_dir, localappdata_dir, nuget_packages_dir):
                path.mkdir(parents=True, mode=0o700, exist_ok=True)
            env["TEMP"] = str(tmp_dir)
            env["TMP"] = str(tmp_dir)
            env["USERPROFILE"] = str(home_dir)
            env["APPDATA"] = str(appdata_dir)
            env["LOCALAPPDATA"] = str(localappdata_dir)
            env["HOMEDRIVE"] = home_dir.drive
            env["HOMEPATH"] = str(home_dir)[len(home_dir.drive) :] or "\\"
            env["DOTNET_CLI_HOME"] = str(home_dir)
            env["NUGET_PACKAGES"] = str(nuget_packages_dir)
            env.setdefault("DOTNET_NOLOGO", "1")
            env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
        if isinstance(extra, dict):
            for key, value in extra.items():
                key_text = str(key)
                value_text = str(value)
                if filter_sensitive and is_filtered_env_var(key_text, value_text):
                    continue
                env[key_text] = value_text
        return env

    def _interactive_command_env(self, extra: Any) -> tuple[dict[str, str], dict[str, Any]]:
        """Return explicit overrides + filtering policy for the desktop broker.

        The broker intentionally inherits the signed-in user's environment,
        not LocalService's synthetic HOME/APPDATA.  The normal shell-env policy
        is applied inside that broker before the child process starts.
        """
        filter_sensitive = not self.dangerously_skip_all_permissions and not self._permission_granted("sensitive_env")
        overrides: dict[str, str] = {}
        if isinstance(extra, dict):
            for key, value in extra.items():
                key_text = str(key)
                value_text = str(value)
                if filter_sensitive and is_filtered_env_var(key_text, value_text):
                    continue
                overrides[key_text] = value_text
        interactive_core = WINDOWS_CORE_ENV_NAMES | {
            "USERPROFILE",
            "USERNAME",
            "USERDOMAIN",
            "USERDOMAIN_ROAMINGPROFILE",
            "SESSIONNAME",
            "COMPUTERNAME",
            "LOGONSERVER",
            "APPDATA",
            "LOCALAPPDATA",
            "HOMEDRIVE",
            "HOMEPATH",
            "OS",
            "PROCESSOR_ARCHITECTURE",
            "NUMBER_OF_PROCESSORS",
            "TEMP",
            "TMP",
        }
        policy = {
            "inherit": self.shell_env_policy.inherit,
            "include_only": list(self.shell_env_policy.include_only),
            "exclude": list(self.shell_env_policy.exclude),
            "set": {str(key): str(value) for key, value in self.shell_env_policy.set.items()},
            "core_names": sorted(interactive_core),
        }
        return overrides, policy

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
        if self.shell_env_policy.inherit == "none":
            return {}
        if self.shell_env_policy.inherit == "all":
            return {str(key): str(value) for key, value in os.environ.items()}
        return {
            str(key): str(value)
            for key, value in os.environ.items()
            if is_core_command_env_name(str(key))
        }

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
        session.refresh_status()
        with session.lock:
            cursor = {"stdout": session.stdout_total_bytes, "stderr": session.stderr_total_bytes}
            retained = len(session.stdout) + len(session.stderr)
        now = time.time()
        item: dict[str, Any] = {
            "session_id": session.session_id,
            "pid": session.process.pid,
            "status": (
                "timeout"
                if session.timed_out
                else "running"
                if session.process.poll() is None
                else "terminated"
                if session.signal_name
                else "exited"
            ),
            "exit_code": session.exit_code,
            "signal": session.signal_name,
            "started_at": datetime.fromtimestamp(session.started_at, timezone.utc).isoformat(),
            "completed_at": (
                datetime.fromtimestamp(session.completed_at, timezone.utc).isoformat()
                if session.completed_at is not None
                else None
            ),
            "elapsed_ms": int(((session.completed_at or now) - session.started_at) * 1000),
            "timeout_at": session.timeout_at,
            "cwd": session.cwd,
            "scratch_dir": session.scratch_dir or None,
            "command": redact_for_trace(session.command_preview),
            "retained_output_bytes": retained,
            "cursor": cursor,
            "output_refs": {
                "stdout": f"session:{session.session_id}:stdout",
                "stderr": f"session:{session.session_id}:stderr",
            },
        }
        if include_process_tree:
            item["process_tree"] = process_tree_for_pid(session.process.pid)
        return item

    def list_sessions(self, args: dict[str, Any]) -> dict[str, Any]:
        self._prune_sessions()
        include_completed = bool(args.get("include_completed", True))
        include_tree = bool(args.get("include_process_tree", False))
        with self.sessions_lock:
            sessions = list(self.sessions.values())
            if include_completed:
                sessions += list(self.output_sessions.values())
        sessions.sort(key=lambda item: item.started_at)
        return {
            "sessions": [self._session_metadata(session, include_process_tree=include_tree) for session in sessions],
            "active": sum(1 for session in sessions if session.process.poll() is None),
            "completed": sum(1 for session in sessions if session.process.poll() is not None),
        }

    def process_tree(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._get_output_session(str(args.get("session_id", "")))
        return {
            "session_id": session.session_id,
            "pid": session.process.pid,
            "process_tree": process_tree_for_pid(session.process.pid),
        }

    def kill_tree(self, args: dict[str, Any]) -> dict[str, Any]:
        kill_args = dict(args)
        kill_args["signal"] = "KILL" if bool(args.get("force", True)) else "TERM"
        kill_args.setdefault("output_mode", "summary")
        return self.kill_session(kill_args)

    def tail_output(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._get_output_session(str(args.get("session_id", "")))
        session.refresh_status()
        stream = str(args.get("stream", "stdout"))
        lines = max(1, min(int(args.get("lines", 20)), 1000))
        max_bytes = max(1, min(int(args.get("max_bytes", 16384)), SESSION_BUFFER_BYTES))
        data, start_offset, total_bytes, dropped_bytes = session.retained_stream_bytes(stream)
        truncation = truncate_output_bytes_tail(data, max_bytes, max_lines=lines)
        return {
            "session_id": session.session_id,
            "stream": stream,
            "content": truncation.content,
            "lines": lines,
            "truncated": truncation.truncated,
            "truncated_by": truncation.truncated_by,
            "retained_start_offset": start_offset,
            "total_stream_bytes": total_bytes,
            "dropped_bytes": dropped_bytes,
            "cursor": {"stdout": session.stdout_total_bytes, "stderr": session.stderr_total_bytes},
            "ok": True,
        }

    def find_output(self, args: dict[str, Any]) -> dict[str, Any]:
        session = self._get_output_session(str(args.get("session_id", "")))
        session.refresh_status()
        query = str(args.get("query", ""))
        stream = str(args.get("stream", "both"))
        case_sensitive = bool(args.get("case_sensitive", False))
        use_regex = bool(args.get("regex", False))
        max_results = max(1, min(int(args.get("max_results", 100)), 1000))
        streams = [stream] if stream in {"stdout", "stderr"} else ["stdout", "stderr"]
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if use_regex else re.escape(query), flags)
        except re.error as exc:
            raise ToolFailure("INVALID_ARGUMENT", f"Invalid regex: {exc}", category="validation") from exc
        matches: list[dict[str, Any]] = []
        for stream_name in streams:
            data, _start, _total, _dropped = session.retained_stream_bytes(stream_name)
            text = data.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), 1):
                match = pattern.search(line)
                if not match:
                    continue
                matches.append(
                    {
                        "stream": stream_name,
                        "line": line_number,
                        "column": match.start() + 1,
                        "preview": truncate_line_chars(line, 500)[0],
                    }
                )
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break
        return {
            "session_id": session.session_id,
            "query": query,
            "matches": matches,
            "truncated": len(matches) >= max_results,
            "max_results": max_results,
            "ok": True,
        }

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
        if self.dangerously_skip_all_permissions:
            return {
                "ok": True,
                "status": "granted",
                "grant_id": "dangerously-skip-all-permissions",
                "expires_at": None,
                "constraints": {
                    "mode": "dangerously_skip_all_permissions",
                    "workspace": str(self.workspace.root),
                    "requested": args,
                },
                "warnings": [
                    "dangerously-skip-all-permissions is enabled; permission-gated operations are auto-granted"
                ],
            }
        tool_name = str(args.get("tool_name", ""))
        permission = str(args.get("permission", ""))
        reason = str(args.get("reason", ""))
        requested_arguments = args.get("arguments")
        if not isinstance(requested_arguments, dict):
            raise ToolFailure("INVALID_ARGUMENT", "arguments must be an object.", category="validation")
        scope = str(args.get("scope", "once"))
        ttl_seconds = int(args.get("ttl_seconds", 300))
        approval_timeout_seconds = int(args.get("approval_timeout_seconds", 75))
        approval = request_permission_approval(
            tool_name=tool_name,
            permission=permission,
            reason=reason,
            arguments=requested_arguments,
            scope=scope,
            ttl_seconds=ttl_seconds,
            timeout_seconds=approval_timeout_seconds,
        )
        if not bool(approval.get("granted")):
            return {
                "ok": False,
                "status": "denied",
                "grant_id": None,
                "expires_at": None,
                "error": {
                    "code": "PERMISSION_DENIED",
                    "message": "The signed-in user denied the permission request.",
                    "category": "permission",
                    "retryable": True,
                    "details": {"tool_name": tool_name, "permission": permission},
                },
            }
        grant_id = "grant_" + secrets.token_urlsafe(18)
        expires_at = time.time() + ttl_seconds
        grant = PermissionGrant(
            grant_id=grant_id,
            owner=self._permission_owner(),
            workspace=os.path.normcase(str(self.workspace.root)),
            tool_name=tool_name,
            permission=permission,
            arguments_digest=self._permission_arguments_digest(requested_arguments),
            scope=scope,
            expires_at=expires_at,
        )
        with self.execution_registry.state_lock:
            self.execution_registry.permission_grants[grant_id] = grant
        return {
            "ok": True,
            "status": "granted",
            "grant_id": grant_id,
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            "constraints": {
                "tool_name": tool_name,
                "permission": permission,
                "scope": scope,
                "workspace": str(self.workspace.root),
                "same_arguments_required": scope == "once",
                "os_privileges": "unchanged; this grant only relaxes an MCP policy gate",
                "privileged_executable_effect": (
                    "allows only the MCP setuid/setgid executable gate where applicable; it never grants Administrator, root, UAC, or ACL access"
                    if permission == "privileged_executable"
                    else None
                ),
            },
            "warnings": (["Session grant applies to this OAuth owner until expiry."] if scope == "session" else []),
        }

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


def require_git() -> str:
    git = cached_which("git")
    if not git:
        raise ToolFailure("GIT_ERROR", "git executable not found.", category="runtime")
    return git


def redact_for_trace(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SENSITIVE_ENV_RE.search(str(key)) else redact_for_trace(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_trace(item) for item in value[:50]]
    if isinstance(value, tuple):
        return [redact_for_trace(item) for item in value[:50]]
    if isinstance(value, str):
        if SENSITIVE_VALUE_RE.search(value):
            return "[REDACTED]"
        if len(value) > 240:
            return value[:240] + "...[truncated]"
        return value
    return value


def sanitize_activity_text(value: Any, *, max_chars: int = 1200) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = SENSITIVE_VALUE_RE.sub("[REDACTED]", text)
    text = ACTIVITY_INLINE_SECRET_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = ACTIVITY_BEARER_RE.sub("Bearer [REDACTED]", text)
    text = ACTIVITY_REQUEST_BASE64_RE.sub(lambda match: match.group(1) + "[REDACTED]", text)
    text = ACTIVITY_LONG_VALUE_RE.sub("[LONG_VALUE_REDACTED]", text)
    if len(text) > max_chars:
        return text[: max(0, max_chars - 14)] + "...[truncated]"
    return text


def _activity_tail(value: Any, *, max_lines: int = 10, max_chars: int = 1800) -> list[str]:
    text = sanitize_activity_text(value, max_chars=max_chars * 2)
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if len(lines) > max_lines:
        lines = ["... output truncated ...", *lines[-max_lines:]]
    result: list[str] = []
    used = 0
    for line in lines:
        clipped = line if len(line) <= 260 else line[:257] + "..."
        if used + len(clipped) > max_chars:
            result.append("... output truncated ...")
            break
        result.append(clipped)
        used += len(clipped)
    return result


def _activity_log_lines(
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any],
    duration_ms: int,
) -> list[str]:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    ok = bool(payload.get("ok", False))
    marker = "✓" if ok else "✗"
    lines: list[str] = []

    if name == "exec_command":
        context = str(args.get("execution_context") or "service")
        exit_code = payload.get("exit_code")
        status = f"exit {exit_code}" if exit_code is not None else str(payload.get("status") or "done")
        lines.append(f"[{stamp}] {marker} exec_command · {context} · {status} · {duration_ms} ms")
        stdout = payload.get("stdout")
        stderr = payload.get("stderr")
        if stdout:
            lines.extend("  " + item for item in _activity_tail(stdout))
        if stderr:
            lines.append("  [stderr]")
            lines.extend("  " + item for item in _activity_tail(stderr))
        if not stdout and not stderr and payload.get("preview"):
            lines.extend("  " + item for item in _activity_tail(payload.get("preview")))
        return lines

    if name == "apply_patch":
        lines.append(
            f"[{stamp}] {marker} apply_patch · {payload.get('additions', 0)} additions · {payload.get('removals', 0)} removals · {duration_ms} ms"
        )
        affected = payload.get("affected_files")
        if isinstance(affected, list):
            for item in affected[:12]:
                if not isinstance(item, dict):
                    continue
                operation = sanitize_activity_text(item.get("operation", "update"), max_chars=32)
                path = sanitize_activity_text(item.get("path", ""), max_chars=300)
                lines.append(f"  {operation}: {path}")
        return lines

    if name in {"browser_use", "computer_use"}:
        action = sanitize_activity_text(args.get("action", "inspect"), max_chars=80)
        surface = "Browser Use" if name == "browser_use" else "Computer Use"
        lines.append(f"[{stamp}] {marker} {surface} · {action} · {duration_ms} ms")
        window = payload.get("window")
        if isinstance(window, dict):
            title = sanitize_activity_text(window.get("title", ""), max_chars=300)
            if title:
                lines.append("  " + title)
        return lines

    if name == "read_file":
        path = sanitize_activity_text(args.get("path", ""), max_chars=360)
        lines.append(
            f"[{stamp}] {marker} read_file · {path} · lines {payload.get('start_line', '?')}-{payload.get('end_line', '?')} · {duration_ms} ms"
        )
        return lines

    if name == "search_text":
        query = sanitize_activity_text(args.get("query", ""), max_chars=240)
        path = sanitize_activity_text(args.get("path", "."), max_chars=280)
        lines.append(f"[{stamp}] {marker} search_text · {path} · {payload.get('total_matches', 0)} matches · {duration_ms} ms")
        if not ok:
            lines.append(f"  query: {query}")
        return lines

    if name in {"list_files", "list_dir"}:
        path = sanitize_activity_text(args.get("path", "."), max_chars=320)
        count = len(payload.get("files") or payload.get("entries") or [])
        lines.append(f"[{stamp}] {marker} {name} · {path} · {count} items · {duration_ms} ms")
        return lines

    if name.startswith("git_"):
        path = sanitize_activity_text(args.get("path", "."), max_chars=320)
        lines.append(f"[{stamp}] {marker} {name} · {path} · {duration_ms} ms")
        return lines

    if name == "human_help_me":
        reason = sanitize_activity_text(args.get("reason", ""), max_chars=120)
        lines.append(
            f"[{stamp}] {marker} HUMAN HELP · {reason} · {payload.get('status') or payload.get('outcome') or 'done'} · {duration_ms} ms"
        )
        return lines

    status = payload.get("status") or ("ok" if ok else "failed")
    lines.append(f"[{stamp}] {marker} {name} · {sanitize_activity_text(status, max_chars=120)} · {duration_ms} ms")
    return lines


def _activity_start_lines(name: str, args: dict[str, Any]) -> list[str]:
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    if name == "exec_command":
        context = sanitize_activity_text(args.get("execution_context") or "service", max_chars=40)
        return [
            f"[{stamp}] ▶ exec_command · {context}",
            "> " + sanitize_activity_text(args.get("cmd", ""), max_chars=700),
        ]
    if name == "apply_patch":
        return [f"[{stamp}] ▶ apply_patch"]
    if name in {"browser_use", "computer_use"}:
        surface = "Browser Use" if name == "browser_use" else "Computer Use"
        action = sanitize_activity_text(args.get("action", "inspect"), max_chars=80)
        return [f"[{stamp}] ▶ {surface} · {action}"]
    if name == "read_file":
        return [f"[{stamp}] ▶ read_file · {sanitize_activity_text(args.get('path', ''), max_chars=360)}"]
    if name == "search_text":
        return [
            f"[{stamp}] ▶ search_text · {sanitize_activity_text(args.get('path', '.'), max_chars=280)}",
            "> " + sanitize_activity_text(args.get("query", ""), max_chars=240),
        ]
    if name in {"list_files", "list_dir"} or name.startswith("git_"):
        return [f"[{stamp}] ▶ {name} · {sanitize_activity_text(args.get('path', '.'), max_chars=320)}"]
    if name == "human_help_me":
        return [f"[{stamp}] ▶ HUMAN HELP · {sanitize_activity_text(args.get('reason', ''), max_chars=120)}"]
    return [f"[{stamp}] ▶ {name}"]


def _prepare_activity_log_for_write() -> None:
    if ACTIVITY_LOG_PATH is None:
        return
    ACTIVITY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    if ACTIVITY_LOG_PATH.exists():
        try:
            last_write = datetime.fromtimestamp(ACTIVITY_LOG_PATH.stat().st_mtime).astimezone()
            if last_write.date() != now.date():
                archive = ACTIVITY_LOG_PATH.with_name(
                    f"{ACTIVITY_LOG_PATH.stem}-{last_write.date().isoformat()}{ACTIVITY_LOG_PATH.suffix}"
                )
                if archive.exists():
                    archive = ACTIVITY_LOG_PATH.with_name(
                        f"{ACTIVITY_LOG_PATH.stem}-{last_write.strftime('%Y-%m-%d-%H%M%S')}{ACTIVITY_LOG_PATH.suffix}"
                    )
                os.replace(ACTIVITY_LOG_PATH, archive)
        except OSError:
            pass

    cutoff = time.time() - ACTIVITY_LOG_RETENTION_DAYS * 24 * 60 * 60
    try:
        pattern = f"{ACTIVITY_LOG_PATH.stem}-*{ACTIVITY_LOG_PATH.suffix}"
        for archived in ACTIVITY_LOG_PATH.parent.glob(pattern):
            try:
                if archived.stat().st_mtime < cutoff:
                    archived.unlink()
            except OSError:
                pass
    except OSError:
        pass


def append_activity_start(name: str, args: dict[str, Any]) -> None:
    if ACTIVITY_LOG_PATH is None:
        return
    try:
        block = "\n".join(_activity_start_lines(name, args)) + "\n"
        with ACTIVITY_LOG_LOCK:
            _prepare_activity_log_for_write()
            with ACTIVITY_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(block)
    except Exception:
        return


def append_activity_log(
    name: str,
    args: dict[str, Any],
    payload: dict[str, Any],
    duration_ms: int,
) -> None:
    if ACTIVITY_LOG_PATH is None:
        return
    try:
        lines = _activity_log_lines(name, args, payload, duration_ms)
        block = "\n".join(lines) + "\n\n"
        with ACTIVITY_LOG_LOCK:
            _prepare_activity_log_for_write()
            with ACTIVITY_LOG_PATH.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(block)
    except Exception:
        # Activity logging must never break a real MCP operation.
        return


class LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int)]


def landlock_abi_version() -> int:
    if sys.platform != "linux":
        raise ToolFailure(
            "SANDBOX_UNAVAILABLE",
            "Linux Landlock filesystem confinement is unavailable on this platform.",
            category="security",
        )
    version = libc_syscall(SYS_LANDLOCK_CREATE_RULESET, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
    if version <= 0:
        err = ctypes.get_errno()
        raise ToolFailure(
            "SANDBOX_UNAVAILABLE",
            "Linux Landlock filesystem confinement is unavailable on this host.",
            category="security",
            details={"errno": err, "reason": os.strerror(err) if err else "unknown"},
        )
    return version


def landlock_handled_access(version: int) -> int:
    handled = (
        LANDLOCK_ACCESS_FS_EXECUTE
        | LANDLOCK_ACCESS_FS_WRITE_FILE
        | LANDLOCK_ACCESS_FS_READ_FILE
        | LANDLOCK_ACCESS_FS_READ_DIR
        | LANDLOCK_ACCESS_FS_REMOVE_DIR
        | LANDLOCK_ACCESS_FS_REMOVE_FILE
        | LANDLOCK_ACCESS_FS_MAKE_CHAR
        | LANDLOCK_ACCESS_FS_MAKE_DIR
        | LANDLOCK_ACCESS_FS_MAKE_REG
        | LANDLOCK_ACCESS_FS_MAKE_SOCK
        | LANDLOCK_ACCESS_FS_MAKE_FIFO
        | LANDLOCK_ACCESS_FS_MAKE_BLOCK
        | LANDLOCK_ACCESS_FS_MAKE_SYM
    )
    if version >= 2:
        handled |= LANDLOCK_ACCESS_FS_REFER
    if version >= 3:
        handled |= LANDLOCK_ACCESS_FS_TRUNCATE
    if version >= 5:
        handled |= LANDLOCK_ACCESS_FS_IOCTL_DEV
    return handled


def landlock_device_access(handled: int) -> int:
    readonly_file_access = handled & (LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE)
    return readonly_file_access | (
        handled
        & (
            LANDLOCK_ACCESS_FS_WRITE_FILE
            | LANDLOCK_ACCESS_FS_TRUNCATE
            | LANDLOCK_ACCESS_FS_IOCTL_DEV
        )
    )


def open_landlock_ruleset(workspace: Path, read_roots: list[str], *, write_roots: list[Path] | None = None) -> int:
    version = landlock_abi_version()
    handled = landlock_handled_access(version)
    ruleset_attr = LandlockRulesetAttr(handled)
    ruleset_fd = libc_syscall(
        SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        err = ctypes.get_errno()
        raise ToolFailure(
            "SANDBOX_UNAVAILABLE",
            "Failed to create Linux Landlock ruleset for exec_command.",
            category="security",
            details={"errno": err, "reason": os.strerror(err) if err else "unknown"},
        )
    try:
        workspace_access = handled
        readonly_access = handled & (
            LANDLOCK_ACCESS_FS_EXECUTE | LANDLOCK_ACCESS_FS_READ_FILE | LANDLOCK_ACCESS_FS_READ_DIR
        )
        device_access = landlock_device_access(handled)
        add_landlock_path(ruleset_fd, workspace, workspace_access)
        for write_root in write_roots or []:
            add_landlock_path(ruleset_fd, write_root, workspace_access, required=False)
        for read_root in read_roots:
            add_landlock_path(ruleset_fd, Path(read_root), readonly_access, required=False)
        for special in SPECIAL_DEVICE_PATHS:
            add_landlock_path(ruleset_fd, Path(special), device_access, required=False)
        for special_dir in ("/proc/self", "/proc/thread-self", "/dev/fd"):
            add_landlock_path(ruleset_fd, Path(special_dir), readonly_access, required=False)
    except Exception:
        os.close(ruleset_fd)
        raise
    return ruleset_fd


def add_landlock_path(ruleset_fd: int, path: Path, allowed_access: int, *, required: bool = True) -> None:
    try:
        fd = os.open(path, getattr(os, "O_PATH", os.O_RDONLY) | os.O_CLOEXEC)
    except OSError as exc:
        if required:
            raise ToolFailure(
                "SANDBOX_UNAVAILABLE",
                "Failed to open path while preparing Landlock sandbox.",
                category="security",
                details={"path": str(path), "errno": exc.errno, "reason": exc.strerror},
            ) from exc
        return
    try:
        path_attr = LandlockPathBeneathAttr(allowed_access & landlock_path_allowed_access(path), fd)
        rc = libc_syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(path_attr), 0)
        if rc < 0 and required:
            err = ctypes.get_errno()
            raise ToolFailure(
                "SANDBOX_UNAVAILABLE",
                "Failed to add path to Landlock sandbox.",
                category="security",
                details={"path": str(path), "errno": err, "reason": os.strerror(err) if err else "unknown"},
            )
    finally:
        os.close(fd)


def landlock_path_allowed_access(path: Path) -> int:
    try:
        mode = path.stat().st_mode
    except OSError:
        return ~0
    if stat.S_ISDIR(mode):
        return ~0
    return (
        LANDLOCK_ACCESS_FS_EXECUTE
        | LANDLOCK_ACCESS_FS_WRITE_FILE
        | LANDLOCK_ACCESS_FS_READ_FILE
        | LANDLOCK_ACCESS_FS_TRUNCATE
        | LANDLOCK_ACCESS_FS_IOCTL_DEV
    )


def landlock_exec_argv(ruleset_fd: int, cmd: str) -> list[str]:
    helper = Path(__file__).with_name("landlock_exec.py")
    return [sys.executable, str(helper), str(ruleset_fd), cmd]


def is_default_system_path_root(resolved: Path) -> bool:
    for prefix_path in _resolved_system_path_root_prefixes():
        if resolved == prefix_path or is_relative_to(resolved, prefix_path):
            return True
    return False


@functools.lru_cache(maxsize=1)
def _resolved_system_path_root_prefixes() -> tuple[Path, ...]:
    prefixes: list[Path] = []
    for prefix in SYSTEM_PATH_ROOT_PREFIXES:
        try:
            prefixes.append(Path(prefix).resolve())
        except OSError:
            prefixes.append(Path(prefix))
    return tuple(prefixes)


def guard_allow_roots() -> list[str]:
    # Keyed on the env vars the computation reads, so repeated exec_command
    # calls skip the dozens of Path.resolve()/is_dir() syscalls while env
    # changes still invalidate the cache.
    return list(
        _guard_allow_roots_cached(
            os.environ.get("JAVA_HOME", ""),
            os.environ.get("PATH", ""),
            os.environ.get(f"{ENV_PREFIX}_EXEC_ALLOW_ROOTS", ""),
        )
    )


@functools.lru_cache(maxsize=8)
def _guard_allow_roots_cached(java_home: str, path_env: str, extra_roots: str) -> tuple[str, ...]:
    roots = set(TOOLCHAIN_READ_ROOTS)
    roots.update(OS_METADATA_READ_FILES)
    roots.update(GIT_READ_ROOTS)
    roots.update(DNS_RESOLVER_READ_ROOTS)
    roots.update(
        {
            str(Path(sys.executable).resolve().parent),
            str(Path(sys.prefix).resolve()),
            str(Path(sys.base_prefix).resolve()),
        }
    )
    if java_home:
        try:
            resolved_java_home = Path(java_home).expanduser().resolve()
        except OSError:
            pass
        else:
            roots.add(str(resolved_java_home))
    for item in path_env.split(os.pathsep):
        if not item:
            continue
        try:
            resolved = Path(item).resolve()
        except OSError:
            continue
        if resolved.is_dir() and is_default_system_path_root(resolved):
            roots.add(str(resolved))
    for item in extra_roots.split(os.pathsep):
        if not item:
            continue
        try:
            resolved = Path(item).expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir():
            roots.add(str(resolved))
    return tuple(sorted(root for root in roots if root and Path(root).is_absolute()))


def _server_card_auth(runtime: Runtime, *, oauth_base_url: str | None = None) -> dict[str, Any]:
    if runtime.oauth_enabled():
        cfg = runtime.oauth_config
        assert cfg is not None
        base = (oauth_base_url or cfg.server_url or "").rstrip("/")
        return {
            "type": "oauth2",
            "scheme": "Bearer",
            "header": "Authorization",
            "authorizationUrl": f"{base}/oauth/authorize",
            "tokenUrl": f"{base}/oauth/token",
        }
    if runtime.auth_token is not None:
        return {"type": "bearer", "scheme": "Bearer", "header": "Authorization"}
    return {"type": "none", "scheme": None, "header": None}


def server_card_payload(runtime: Runtime, *, oauth_base_url: str | None = None) -> dict[str, Any]:
    names = runtime.exposed_tool_names()
    # Always the real annotations, never the tools/list override: this card is
    # what an operator fetches to find out what the endpoint actually does.
    annotations = {name: tool_annotations(name, fake_readonly=False) for name in names}
    read_only = [name for name in names if annotations[name].get("readOnlyHint") is True]
    mutating = [name for name in names if annotations[name].get("readOnlyHint") is not True]
    payload = {
        "protocolVersion": PROTOCOL_VERSION,
        "server": {
            "name": SERVER_NAME,
            "title": SERVER_TITLE,
            "version": runtime_version(),
        },
        "transport": {
            "type": "streamable_http",
            "endpoint": MCP_ENDPOINT_PATH,
            "methods": ["POST", "DELETE", "OPTIONS"],
        },
        "auth": _server_card_auth(runtime, oauth_base_url=oauth_base_url),
        "tools": {
            "count": len(names),
            "names": names,
            "readOnlyHintTrue": read_only,
            "readOnlyHintFalse": mutating,
            "annotationOverride": ("fake_readonly" if runtime.fake_readonly_annotations else None),
        },
        "capabilities": {
            "tools": {"listChanged": True},
        },
    }
    return payload


class MCPHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"CodingToolsMCP/{__version__}"
    protocol_version = "HTTP/1.1"
    timeout = 90

    @property
    def runtime(self) -> Runtime:
        return cast(Runtime, getattr(self, "_runtime", self.server.control_runtime))  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args, file=sys.stderr)

    def send_rpc_error(
        self,
        code: int,
        message: str,
        *,
        status: int = 400,
        request_id: str | int | None = None,
        data: Any = None,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        self.send_json(
            jsonrpc_error(request_id, code, message, data),
            status=status,
            extra_headers=extra_headers,
            head_only=head_only,
        )

    def do_GET(self) -> None:
        self.handle_metadata_request(head_only=False)

    def do_HEAD(self) -> None:
        self.handle_metadata_request(head_only=True)

    def do_DELETE(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if posixpath.normpath(request_path) != MCP_ENDPOINT_PATH:
            self.send_json({"error": "Unknown endpoint"}, status=404)
            return
        if not self.is_authorized():
            self.send_unauthorized()
            return
        session_id = self.headers.get("Mcp-Session-Id")
        if not session_id or not self.server.sessions.delete(session_id):  # type: ignore[attr-defined]
            self.send_rpc_error(-32001, "Unknown MCP session", status=404)
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_cors_headers()
        self.end_headers()

    def do_OPTIONS(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if posixpath.normpath(request_path) not in {
            MCP_ENDPOINT_PATH,
            "/.well-known/mcp.json",
            "/.well-known/mcp/server-card.json",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/oauth/authorize",
            "/oauth/token",
            "/oauth/register",
        }:
            self.send_json({"error": "Unknown endpoint"}, status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not is_allowed_origin(origin):
            self.send_json({"error": "Origin denied"}, status=403)
            return
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_cors_headers()
        self.end_headers()

    def _read_mcp_body(self) -> bytes | None:
        """Read an MCP request body with both fixed-length and chunked HTTP framing."""

        transfer_encoding = self.headers.get("Transfer-Encoding", "").strip().lower()
        raw_length = self.headers.get("Content-Length")
        if transfer_encoding:
            codings = [item.strip() for item in transfer_encoding.split(",") if item.strip()]
            if codings != ["chunked"] or raw_length is not None:
                self.close_connection = True
                self.send_rpc_error(-32600, "Only chunked Transfer-Encoding is supported", status=400)
                return None
            body = bytearray()
            while True:
                line = self.rfile.readline(8192)
                if not line or not line.endswith(b"\r\n"):
                    self.close_connection = True
                    self.send_rpc_error(-32700, "Malformed chunked request body")
                    return None
                size_text = line[:-2].split(b";", 1)[0].strip()
                try:
                    size = int(size_text, 16)
                except ValueError:
                    self.close_connection = True
                    self.send_rpc_error(-32700, "Malformed chunk size")
                    return None
                if size < 0 or len(body) + size > MAX_HTTP_REQUEST_BYTES:
                    self.close_connection = True
                    self.send_rpc_error(
                        -32600,
                        "Request body exceeds maximum size",
                        status=413,
                        data={"max_bytes": MAX_HTTP_REQUEST_BYTES},
                    )
                    return None
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(8192)
                        if not trailer:
                            self.close_connection = True
                            self.send_rpc_error(-32700, "Malformed chunked trailers")
                            return None
                        if trailer in {b"\r\n", b"\n"}:
                            return bytes(body)
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    self.close_connection = True
                    self.send_rpc_error(-32700, "Malformed chunked request body")
                    return None
                body.extend(chunk)

        if raw_length is None:
            self.close_connection = True
            self.send_rpc_error(-32600, "Content-Length or chunked Transfer-Encoding is required", status=411)
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self.close_connection = True
            self.send_rpc_error(-32600, "Content-Length must be a non-negative integer")
            return None
        if length < 0:
            self.close_connection = True
            self.send_rpc_error(-32600, "Content-Length must be a non-negative integer")
            return None
        if length > MAX_HTTP_REQUEST_BYTES:
            self.close_connection = True
            self.send_rpc_error(
                -32600,
                "Request body exceeds maximum size",
                status=413,
                data={"max_bytes": MAX_HTTP_REQUEST_BYTES},
            )
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            self.send_rpc_error(-32700, "Incomplete request body")
            return None
        return body

    def handle_metadata_request(self, *, head_only: bool) -> None:
        request_path = self.path.split("?", 1)[0]
        normalized = posixpath.normpath(request_path)
        if normalized == "/.well-known/oauth-authorization-server":
            self.handle_oauth_as_metadata(head_only=head_only)
            return
        if normalized == "/.well-known/oauth-protected-resource":
            self.handle_oauth_resource_metadata(head_only=head_only)
            return
        if normalized == "/oauth/authorize" and not head_only:
            self.handle_oauth_authorize_get()
            return
        if normalized == MCP_ENDPOINT_PATH:
            origin = self.headers.get("Origin")
            if origin and not is_allowed_origin(origin):
                self.send_json({"error": "Origin denied"}, status=403, head_only=head_only)
                return
            if not self.is_authorized():
                self.send_unauthorized(head_only=head_only)
                return
            if not head_only and "text/event-stream" in self.headers.get("Accept", "").lower():
                session_id = self.headers.get("Mcp-Session-Id")
                if not session_id or self.server.sessions.get(session_id) is None:  # type: ignore[attr-defined]
                    self.send_rpc_error(-32001, "Unknown MCP session", status=404)
                    return
                self.handle_tool_notification_stream(session_id)
                return
            self.send_rpc_error(
                -32000,
                "GET /mcp requires Accept: text/event-stream and a valid Mcp-Session-Id",
                status=405,
                extra_headers={"Allow": "POST, DELETE"},
                head_only=head_only,
            )
            return
        if normalized in {"/.well-known/mcp.json", "/.well-known/mcp/server-card.json"}:
            self.send_json(server_card_payload(self.runtime, oauth_base_url=self.oauth_base_url()), head_only=head_only)
            return
        self.send_json({"error": "Unknown endpoint"}, status=404, head_only=head_only)

    def handle_tool_notification_stream(self, session_id: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Mcp-Session-Id", session_id)
        self.send_cors_headers()
        self.end_headers()
        try:
            self.connection.settimeout(None)
        except OSError:
            pass

        generation = self.server.tool_list_generation  # type: ignore[attr-defined]
        try:
            while True:
                # Keep the HTTP session from expiring while the notification
                # stream is open, but do not count the stream as an in-flight
                # tool call.
                if self.server.sessions.get(session_id) is None:  # type: ignore[attr-defined]
                    return
                next_generation = self.server.wait_for_tool_list_change(generation, timeout=15.0)  # type: ignore[attr-defined]
                if next_generation != generation:
                    payload = json.dumps(
                        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}},
                        separators=(",", ":"),
                    ).encode("utf-8")
                    self.wfile.write(b"event: message\n")
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    generation = next_generation
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self) -> None:
        request_path = self.path.split("?", 1)[0]
        normalized = posixpath.normpath(request_path)
        if normalized == "/oauth/authorize":
            self.handle_oauth_authorize_post()
            return
        if normalized == "/oauth/token":
            self.handle_oauth_token()
            return
        if normalized == "/oauth/register":
            self.handle_oauth_register()
            return
        if normalized != MCP_ENDPOINT_PATH:
            self.send_rpc_error(-32601, "Unknown endpoint", status=404)
            return
        origin = self.headers.get("Origin")
        if origin and not is_allowed_origin(origin):
            self.close_connection = True
            self.send_rpc_error(-32600, "Origin denied", status=403)
            return
        if not self.is_authorized():
            self.close_connection = True
            self.send_unauthorized()
            return
        if self.headers.get_content_type().lower() != "application/json":
            self.close_connection = True
            self.send_rpc_error(-32600, "Content-Type must be application/json", status=415)
            return
        protocol_version = self.headers.get("MCP-Protocol-Version")
        if protocol_version and not protocol_version_is_supported(protocol_version):
            self.close_connection = True
            self.send_rpc_error(
                -32600,
                "Unsupported MCP protocol version",
                data={"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "received": protocol_version},
            )
            return
        body = self._read_mcp_body()
        if body is None:
            return
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_rpc_error(-32700, "Parse error")
            return
        if isinstance(request, list):
            self.send_rpc_error(-32600, "JSON-RPC batch requests are not supported by Streamable HTTP")
            return
        if not isinstance(request, dict):
            self.send_rpc_error(-32600, "Invalid Request")
            return
        try:
            validate_rpc_envelope(request)
        except JsonRpcError as exc:
            self.send_rpc_error(
                exc.code, exc.message, status=200, request_id=response_id(request), data=exc.data
            )
            return
        method = request.get("method")
        session_id = self.headers.get("Mcp-Session-Id")
        request_params = request.get("params") if isinstance(request.get("params"), dict) else {}
        request_meta = request_params.get("_meta") if isinstance(request_params.get("_meta"), dict) else {}
        stateless_request = (
            method == "server/discover"
            or protocol_version == STATELESS_PROTOCOL_VERSION
            or request_meta.get("io.modelcontextprotocol/protocolVersion") == STATELESS_PROTOCOL_VERSION
        )
        created_session = False
        leased_session_id: str | None = None
        if stateless_request:
            self._runtime = self.server.control_runtime  # type: ignore[attr-defined]
        elif method == "initialize":
            if session_id:
                self.send_rpc_error(
                    -32600, "initialize must not include Mcp-Session-Id", request_id=request.get("id")
                )
                return
            owner = self.session_owner() or f"ip:{self.client_address[0]}"
            if not self.server.rate_limiter.allow(
                f"mcp-initialize:{owner}", limit=30, window_seconds=60
            ):
                self.send_rpc_error(
                    -32000,
                    "Too many MCP initialize requests",
                    status=429,
                    request_id=request.get("id"),
                    extra_headers={"Retry-After": "10"},
                )
                return
            try:
                binding = self.server.sessions.create(  # type: ignore[attr-defined]
                    self.session_owner(), acquire=True
                )
                self._runtime = binding.runtime
                self._mcp_session_id = binding.session_id
                leased_session_id = binding.session_id
            except SessionCapacityError as exc:
                self.send_rpc_error(
                    -32000,
                    str(exc),
                    status=429,
                    request_id=request.get("id"),
                    extra_headers={"Retry-After": str(exc.retry_after)},
                )
                return
            except RuntimeError as exc:
                self.send_rpc_error(-32000, str(exc), status=503, request_id=request.get("id"))
                return
            self._send_session_header = True
            created_session = True
        elif session_id:
            binding = self.server.sessions.acquire(session_id)  # type: ignore[attr-defined]
            if binding is None:
                self.send_rpc_error(
                    -32001, "Unknown MCP session", status=404, request_id=response_id(request)
                )
                return
            self._runtime = binding.runtime
            self._mcp_session_id = binding.session_id
            leased_session_id = binding.session_id
            self._send_session_header = True
            if protocol_version and protocol_version != self.runtime.protocol_version:
                self.server.sessions.release(binding.session_id)  # type: ignore[attr-defined]
                leased_session_id = None
                self.send_rpc_error(
                    -32600,
                    "MCP-Protocol-Version does not match the initialized session",
                    request_id=request.get("id"),
                    data={"expected": self.runtime.protocol_version, "received": protocol_version},
                )
                return
        elif method == "ping":
            self._runtime = self.server.control_runtime  # type: ignore[attr-defined]
        else:
            self.send_rpc_error(-32002, "Server not initialized", request_id=request.get("id"))
            return
        try:
            response = self.handle_rpc(request)
            if created_session and response is not None and "error" in response:
                self.server.sessions.delete(self._mcp_session_id)  # type: ignore[attr-defined]
                self._send_session_header = False
                leased_session_id = None
            if response is None:
                self.send_response(202)
                if getattr(self, "_send_session_header", False):
                    self.send_header("Mcp-Session-Id", self._mcp_session_id)
                self.send_header("Content-Length", "0")
                self.send_cors_headers()
                self.end_headers()
                return
            self.send_json(response)
        finally:
            if leased_session_id is not None:
                self.server.sessions.release(leased_session_id)  # type: ignore[attr-defined]

    def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if request.get("method") == "server/discover":
                return {
                    "jsonrpc": "2.0",
                    "id": response_id(request),
                    "result": {
                        "resultType": "complete",
                        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": runtime_version(),
                        },
                        "instructions": self.runtime.project_context.server_instructions(),
                    },
                }
            return dispatch_rpc(self.runtime, request)
        except Exception as exc:  # noqa: BLE001 - HTTP must always answer with JSON-RPC
            error_message, error_leaves = summarize_exception(exc)
            return jsonrpc_error(
                response_id(request),
                -32603,
                error_message,
                {"exception_type": exc.__class__.__name__, "leaf_errors": error_leaves},
            )

    def is_authorized(self) -> bool:
        if not self.runtime.auth_enabled():
            return True
        header = self.headers.get("Authorization", "").strip()
        if self.runtime.auth_token is not None:
            if secrets.compare_digest(header, f"Bearer {self.runtime.auth_token}"):
                return True
        if self.runtime.oauth_config is not None and header.startswith("Bearer "):
            token = header[len("Bearer "):]
            base = self.oauth_base_url()
            for audience in self.oauth_resource_urls():
                if validate_access_token(
                    token,
                    self.runtime.oauth_config,
                    base,
                    audience=audience,
                ):
                    return True
        return False

    def session_owner(self) -> str | None:
        """Return a non-sensitive stable owner key for session quotas."""

        header = self.headers.get("Authorization", "").strip()
        if not header:
            return None
        if header.startswith("Bearer ") and self.runtime.oauth_config is not None:
            token = header[len("Bearer ") :]
            base = self.oauth_base_url()
            for audience in self.oauth_resource_urls():
                client_id = access_token_client_id(
                    token,
                    self.runtime.oauth_config,
                    base,
                    audience=audience,
                )
                if client_id:
                    return "oauth-client:" + hashlib.sha256(client_id.encode("utf-8")).hexdigest()
        return hashlib.sha256(header.encode("utf-8")).hexdigest()

    def oauth_base_url(self) -> str:
        cfg = self.runtime.oauth_config
        if cfg is not None and cfg.server_url:
            return cfg.server_url.rstrip("/")
        trust_proxy = truthy_env(os.environ.get(f"{ENV_PREFIX}_TRUST_PROXY_HEADERS"))
        proto = _first_header_value(self.headers.get("X-Forwarded-Proto")) if trust_proxy else ""
        if trust_proxy and not proto:
            proto = _forwarded_header_param(self.headers.get("Forwarded"), "proto")
        host = _safe_external_host(_first_header_value(self.headers.get("X-Forwarded-Host"))) if trust_proxy else ""
        if trust_proxy and not host:
            host = _safe_external_host(_forwarded_header_param(self.headers.get("Forwarded"), "host"))
        if not host:
            host = _safe_external_host(self.headers.get("Host", ""))
        if not host:
            server_address = cast(tuple[Any, ...], self.server.server_address)  # type: ignore[attr-defined]
            bind_host = server_address[0]
            bind_port = server_address[1]
            host = _http_base_for_bind_host(str(bind_host), int(bind_port)).removeprefix("http://")
        if proto not in {"http", "https"}:
            host_without_port = host.rsplit(":", 1)[0].strip("[]")
            proto = "http" if is_loopback_bind_host(host_without_port) else "https"
        return f"{proto}://{host}".rstrip("/")

    def oauth_resource_urls(self) -> tuple[str, str]:
        base = self.oauth_base_url()
        return base, f"{base}{MCP_ENDPOINT_PATH}"

    def normalize_oauth_resource(self, resource: str) -> str | None:
        normalized = resource.rstrip("/")
        return normalized if normalized in self.oauth_resource_urls() else None

    def send_unauthorized(self, *, head_only: bool = False) -> None:
        if self.runtime.oauth_config is not None:
            base = self.oauth_base_url()
            www_auth = f'Bearer realm="coding-tools-mcp", resource_metadata="{base}/.well-known/oauth-protected-resource"'
        else:
            www_auth = 'Bearer realm="coding-tools-mcp"'
        self.send_rpc_error(
            -32000,
            "Unauthorized",
            status=401,
            extra_headers={"WWW-Authenticate": www_auth},
            head_only=head_only,
        )

    def handle_oauth_as_metadata(self, *, head_only: bool = False) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404, head_only=head_only)
            return
        base = self.oauth_base_url()
        self.send_json(
            {
                "issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "registration_endpoint": f"{base}/oauth/register",
                "response_types_supported": list(OAUTH_RESPONSE_TYPES_SUPPORTED),
                "grant_types_supported": list(OAUTH_GRANT_TYPES_SUPPORTED),
                "scopes_supported": ["mcp", "offline_access"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": list(OAUTH_TOKEN_AUTH_METHODS),
            },
            head_only=head_only,
        )

    def handle_oauth_resource_metadata(self, *, head_only: bool = False) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404, head_only=head_only)
            return
        base = self.oauth_base_url()
        self.send_json(
            {
                "resource": f"{base}{MCP_ENDPOINT_PATH}",
                "authorization_servers": [base],
                "bearer_methods_supported": ["header"],
                "scopes_supported": ["mcp", "offline_access"],
            },
            head_only=head_only,
        )

    def _send_html(self, body: str, *, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        _write_http_body_safely(self, data)

    def _oauth_login_page(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        state: str,
        resource: str,
        error: str = "",
    ) -> str:
        def esc(v: str) -> str:
            return html.escape(v, quote=True)
        error_block = f'<p style="color:red">{html.escape(error)}</p>' if error else ""
        return (
            "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<title>Authorize MCP Server</title>"
            "<style>body{font-family:sans-serif;max-width:380px;margin:4rem auto;padding:1rem}"
            "input{width:100%;padding:.5rem;margin:.4rem 0;box-sizing:border-box}"
            "button{width:100%;padding:.7rem;background:#0066cc;color:#fff;border:none;cursor:pointer}</style>"
            "</head><body>"
            f"<h2>Authorize Coding Tools MCP</h2>"
            f"<p>Client: <strong>{esc(client_id)}</strong></p>"
            f"<p>Redirect URI: <code>{esc(redirect_uri)}</code></p>"
            f"{error_block}"
            "<form method='POST' action='/oauth/authorize'>"
            f"<input type='hidden' name='client_id' value='{esc(client_id)}'>"
            f"<input type='hidden' name='redirect_uri' value='{esc(redirect_uri)}'>"
            f"<input type='hidden' name='code_challenge' value='{esc(code_challenge)}'>"
            f"<input type='hidden' name='code_challenge_method' value='{esc(code_challenge_method)}'>"
            f"<input type='hidden' name='state' value='{esc(state)}'>"
            f"<input type='hidden' name='resource' value='{esc(resource)}'>"
            "<label>Password<input type='password' name='password' autocomplete='current-password' required></label>"
            "<button type='submit'>Authorize</button>"
            "</form></body></html>"
        )

    def _read_oauth_body(self) -> bytes | None:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self.send_json({"error": "Content-Length required"}, status=411)
            return None
        try:
            length = int(raw_len)
        except ValueError:
            self.send_json({"error": "Invalid Content-Length"}, status=400)
            return None
        if not (0 <= length <= OAUTH_MAX_BODY_BYTES):
            self.send_json({"error": "Request body too large"}, status=413)
            return None
        return self.rfile.read(length)

    def handle_oauth_authorize_get(self) -> None:
        if not self.server.rate_limiter.allow(
            f"oauth-authorize-get:{self.client_address[0]}", limit=30, window_seconds=60
        ):
            self._send_html("<h2>Too many requests</h2>", status=429)
            return
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404)
            return
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query, keep_blank_values=True)
        _p = functools.partial(_first_form_value, params)
        client_id = _p("client_id")
        redirect_uri = _p("redirect_uri")
        code_challenge = _p("code_challenge")
        code_challenge_method = _p("code_challenge_method")
        state = _p("state")
        resource = _p("resource")

        if _p("response_type") != "code":
            self._send_html("<h2>Error</h2><p>response_type must be 'code'</p>", status=400)
            return
        if cfg.registry.get(client_id) is None:
            self._send_html("<h2>Error</h2><p>Unknown client_id</p>", status=400)
            return
        if not cfg.registry.accepts_redirect(client_id, redirect_uri):
            self._send_html("<h2>Error</h2><p>redirect_uri is not registered for this client</p>", status=400)
            return
        if code_challenge_method != "S256" or not valid_pkce_challenge(code_challenge):
            self._send_html("<h2>Error</h2><p>code_challenge_method must be S256 and code_challenge is required</p>", status=400)
            return
        if self.normalize_oauth_resource(resource) is None:
            self._send_html("<h2>Error</h2><p>resource must identify this MCP server</p>", status=400)
            return

        self._send_html(self._oauth_login_page(
            client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
            code_challenge_method=code_challenge_method, state=state, resource=resource,
        ))

    def handle_oauth_authorize_post(self) -> None:
        if not self.server.rate_limiter.allow(
            f"oauth-authorize-post:{self.client_address[0]}", limit=10, window_seconds=60
        ):
            self._send_html("<h2>Too many requests</h2>", status=429)
            return
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404)
            return
        body = self._read_oauth_body()
        if body is None:
            return
        if self.headers.get_content_type().lower() != "application/x-www-form-urlencoded":
            self.send_json({"error": "invalid_request", "error_description": "Content-Type must be application/x-www-form-urlencoded"}, status=400)
            return
        params = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        _p = functools.partial(_first_form_value, params)
        client_id = _p("client_id")
        redirect_uri = _p("redirect_uri")
        code_challenge = _p("code_challenge")
        code_challenge_method = _p("code_challenge_method")
        state = _p("state")
        resource = _p("resource")
        password = _p("password")

        def fail(error: str, status: int = 400) -> None:
            self._send_html(self._oauth_login_page(
                client_id=client_id, redirect_uri=redirect_uri, code_challenge=code_challenge,
                code_challenge_method=code_challenge_method, state=state, resource=resource,
                error=error,
            ), status=status)

        if cfg.registry.get(client_id) is None or not cfg.registry.accepts_redirect(client_id, redirect_uri):
            fail("Invalid client or redirect URI")
            return
        if code_challenge_method != "S256" or not valid_pkce_challenge(code_challenge):
            fail("Invalid PKCE parameters")
            return
        normalized_resource = self.normalize_oauth_resource(resource)
        if normalized_resource is None:
            fail("Invalid resource")
            return
        if not secrets.compare_digest(password, cfg.password):
            fail("Invalid password", status=401)
            return

        code = secrets.token_urlsafe(32)
        now = time.time()
        cfg.put_pending_code(
            code,
            {
                "code_challenge": code_challenge,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "expires_at": now + OAUTH_CODE_TTL_SECONDS,
                "server_url": self.oauth_base_url(),
                "resource": normalized_resource,
            },
        )

        qs = urllib.parse.urlencode({"code": code, **({"state": state} if state else {})})
        sep = "&" if "?" in redirect_uri else "?"
        location = redirect_uri + sep + qs
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def handle_oauth_token(self) -> None:
        if not self.server.rate_limiter.allow(
            f"oauth-token:{self.client_address[0]}", limit=30, window_seconds=60
        ):
            self.send_json(
                {"error": "slow_down", "error_description": "Too many token requests."},
                status=429,
                extra_headers={"Retry-After": "10"},
            )
            return
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "unsupported_grant_type"}, status=400)
            return

        def _err(error: str, description: str) -> None:
            self.log_message("OAuth token error: %s - %s", error, description)
            self.send_json({"error": error, "error_description": description}, status=400)

        body = self._read_oauth_body()
        if body is None:
            return
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            _err("invalid_request", "Content-Type must be application/x-www-form-urlencoded")
            return
        params = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        _p = functools.partial(_first_form_value, params)
        grant_type = _p("grant_type")
        code = _p("code")
        refresh_token = _p("refresh_token")
        redirect_uri = _p("redirect_uri")
        code_verifier = _p("code_verifier")
        client_id = _p("client_id")
        client_secret = _p("client_secret")
        resource = _p("resource").rstrip("/")
        presented_auth_method = "client_secret_post" if client_secret else "none"

        # Also accept HTTP Basic auth for client credentials.
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Basic ") and (not client_id or not client_secret):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                basic_id, _, basic_secret = decoded.partition(":")
                if not client_id:
                    client_id = urllib.parse.unquote(basic_id)
                if not client_secret:
                    client_secret = urllib.parse.unquote(basic_secret)
                presented_auth_method = "client_secret_basic"
            except Exception:  # noqa: BLE001
                pass

        if grant_type not in {OAUTH_GRANT_TYPE_AUTHORIZATION_CODE, OAUTH_GRANT_TYPE_REFRESH_TOKEN}:
            _err("unsupported_grant_type", "Only authorization_code and refresh_token are supported")
            return
        if cfg.registry.get(client_id) is None:
            _err("invalid_client", "Unknown client_id")
            return
        if not cfg.registry.authenticates(client_id, client_secret, presented_auth_method):
            _err("invalid_client", "Invalid client_secret")
            return
        server_url = resource
        if grant_type == OAUTH_GRANT_TYPE_REFRESH_TOKEN:
            if not refresh_token or cfg.state_store is None:
                _err("invalid_grant", "refresh_token is not available")
                return
            normalized_resource = self.normalize_oauth_resource(resource)
            if normalized_resource is None:
                _err("invalid_target", "resource mismatch")
                return
            server_url = normalized_resource
            consumed, family_id = cfg.state_store.consume_refresh_token(refresh_token, client_id)
            if not consumed or family_id is None:
                _err("invalid_grant", "Unknown, expired, or already-used refresh token")
                return
            access_token = create_access_token(
                cfg,
                self.oauth_base_url(),
                client_id=client_id,
                audience=server_url,
            )
            next_refresh, refresh_expires_at, _ = cfg.state_store.issue_refresh_token(
                client_id, ttl=cfg.refresh_token_ttl, family_id=family_id
            )
            self.send_json(
                {
                    "access_token": access_token,
                    "token_type": "Bearer",
                    "expires_in": cfg.token_ttl,
                    "refresh_token": next_refresh,
                    "refresh_token_expires_in": max(0, int(refresh_expires_at - time.time())),
                }
            )
            return

        if not code:
            _err("invalid_grant", "code is required")
            return
        if not code_verifier or not (43 <= len(code_verifier) <= 128) or not re.fullmatch(r"[A-Za-z0-9\-._~]+", code_verifier):
            _err("invalid_grant", "Invalid code_verifier")
            return

        code_data = cfg.consume_pending_code(code)
        if code_data is None:
            _err("invalid_grant", "Unknown, expired, or already-used authorization code")
            return
        if not secrets.compare_digest(code_data["client_id"], client_id):
            _err("invalid_grant", "client_id mismatch")
            return
        if not secrets.compare_digest(code_data["redirect_uri"], redirect_uri):
            _err("invalid_grant", "redirect_uri mismatch")
            return
        if not resource or not secrets.compare_digest(str(code_data.get("resource") or ""), resource):
            _err("invalid_target", "resource mismatch")
            return
        if not verify_pkce(code_verifier, code_data["code_challenge"]):
            _err("invalid_grant", "PKCE verification failed")
            return

        access_token = create_access_token(
            cfg,
            self.oauth_base_url(),
            client_id=client_id,
            audience=server_url,
        )
        payload: dict[str, Any] = {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": cfg.token_ttl,
        }
        if cfg.state_store is not None:
            next_refresh, refresh_expires_at, _ = cfg.state_store.issue_refresh_token(
                client_id, ttl=cfg.refresh_token_ttl
            )
            payload["refresh_token"] = next_refresh
            payload["refresh_token_expires_in"] = max(0, int(refresh_expires_at - time.time()))
        self.send_json(payload)

    def handle_oauth_register(self) -> None:
        cfg = self.runtime.oauth_config
        if cfg is None:
            self.send_json({"error": "OAuth not configured"}, status=404)
            return
        if not truthy_env(os.environ.get(f"{ENV_PREFIX}_OAUTH_ALLOW_DYNAMIC_REGISTRATION")):
            self.send_json(
                {"error": "registration_disabled", "error_description": "This private MCP accepts only its pre-registered client."},
                status=403,
            )
            return
        body = self._read_oauth_body()
        if body is None:
            return
        if self.headers.get_content_type().lower() != "application/json":
            self.send_json({"error": "invalid_client_metadata", "error_description": "Content-Type must be application/json"}, status=400)
            return
        try:
            metadata = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json({"error": "invalid_client_metadata", "error_description": "Body must be valid JSON"}, status=400)
            return
        if not isinstance(metadata, dict):
            self.send_json({"error": "invalid_client_metadata", "error_description": "Metadata must be an object"}, status=400)
            return
        try:
            registered = cfg.registry.register(metadata)
        except ValueError as exc:
            self.send_json({"error": "invalid_client_metadata", "error_description": str(exc)}, status=400)
            return
        self.send_json(registered, status=201)

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and is_allowed_origin(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, DELETE, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Accept, Authorization, Content-Type, MCP-Protocol-Version, Mcp-Session-Id",
            )

    def send_json(
        self,
        payload: Any,
        *,
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
        head_only: bool = False,
    ) -> None:
        body = json_response_payload(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_send_session_header", False) and getattr(self, "_mcp_session_id", None):
            self.send_header("Mcp-Session-Id", self._mcp_session_id)
        self.send_cors_headers()
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            _write_http_body_safely(self, body)


class MCPHealthHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"CodingToolsMCPHealth/{__version__}"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] in {"/", "/index.html"}:
            body = (
                "<!doctype html><meta charset='utf-8'><title>Coding Tools MCP</title>"
                "<style>body{font:15px system-ui;margin:2rem;max-width:60rem}"
                "pre{background:#f4f4f4;padding:1rem;overflow:auto}</style>"
                "<h1>Coding Tools MCP</h1><pre id='status'>loading…</pre>"
                "<script>fetch('/healthz').then(r=>r.json()).then(v=>status.textContent="
                "JSON.stringify(v,null,2)).catch(e=>status.textContent=String(e))</script>"
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif self.path.split("?", 1)[0] == "/healthz":
            body = json.dumps(self.server.mcp_server.health_payload(), separators=(",", ":")).encode("utf-8")
            content_type = "application/json"
        else:
            body = b'{"error":"not_found"}'
            content_type = "application/json"
        status = 200 if self.path.split("?", 1)[0] in {"/", "/index.html", "/healthz"} else 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _write_http_body_safely(self, body)

    def do_POST(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if request_path == "/notify-tools-changed":
            generation = self.server.mcp_server.notify_tools_changed()
            body = json.dumps(
                {"status": "ok", "tool_list_generation": generation}, separators=(",", ":")
            ).encode("utf-8")
            self.send_response(200)
        elif request_path != "/prune":
            body = b'{"error":"not_found"}'
            self.send_response(404)
        else:
            self.server.mcp_server.sessions.prune()
            body = json.dumps(self.server.mcp_server.health_payload(), separators=(",", ":")).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _write_http_body_safely(self, body)


class RuntimeHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[MCPHandler],
        control_runtime: Runtime,
        runtime_factory: Any,
        *,
        tool_list_state: dict[str, Any] | None = None,
        enable_health: bool = True,
    ) -> None:
        super().__init__(address, handler)
        self.control_runtime = control_runtime
        self.sessions = HTTPSessionManager(runtime_factory)
        self.control_runtime.execution_registry.http_session_stats_provider = self.sessions.stats
        self.rate_limiter = SlidingWindowRateLimiter()
        self.started_at = time.time()
        self._tool_list_state = tool_list_state or {
            "condition": threading.Condition(),
            "generation": 0,
        }
        self.health_server: http.server.ThreadingHTTPServer | None = None
        self.health_thread: threading.Thread | None = None
        if not enable_health:
            return
        raw_health_port = (os.environ.get(f"{ENV_PREFIX}_HEALTH_PORT") or "8766").strip()
        try:
            health_port = int(raw_health_port)
        except ValueError:
            health_port = 8766
        if health_port > 0:
            try:
                self.health_server = http.server.ThreadingHTTPServer(
                    ("127.0.0.1", health_port), MCPHealthHandler
                )
                self.health_server.mcp_server = self  # type: ignore[attr-defined]
                self.health_server.daemon_threads = True
                self.health_thread = threading.Thread(
                    target=self.health_server.serve_forever,
                    name="coding-tools-mcp-health",
                    daemon=True,
                )
                self.health_thread.start()
            except OSError as exc:
                print(f"WARNING: local health endpoint disabled: {exc}", file=sys.stderr)
                self.health_server = None

    @property
    def tool_list_generation(self) -> int:
        return int(self._tool_list_state["generation"])

    def notify_tools_changed(self) -> int:
        condition = self._tool_list_state["condition"]
        with condition:
            self._tool_list_state["generation"] = int(self._tool_list_state["generation"]) + 1
            condition.notify_all()
            return int(self._tool_list_state["generation"])

    def wait_for_tool_list_change(self, generation: int, *, timeout: float) -> int:
        condition = self._tool_list_state["condition"]
        with condition:
            if int(self._tool_list_state["generation"]) == generation:
                condition.wait(timeout=max(0.1, timeout))
            return int(self._tool_list_state["generation"])

    def health_payload(self) -> dict[str, Any]:
        with self.control_runtime.sessions_lock:
            running_exec = len(self.control_runtime.sessions)
            retained_output = len(self.control_runtime.output_sessions)
        return {
            "status": "ok",
            "version": __version__,
            "display_version": runtime_version(),
            "build_identity": runtime_build_identity(),
            "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc).isoformat(),
            "permission_mode": self.control_runtime.permission_mode,
            "dangerously_skip_all_permissions": self.control_runtime.dangerously_skip_all_permissions,
            "permission_approval_transport": "local_windows_broker",
            "workspace": str(self.control_runtime.workspace.root),
            "workspace_allowlist": [
                {"name": entry.name, "path": str(entry.path)}
                for entry in workspace_catalog_from_env()
            ],
            "mcp": {"endpoint": MCP_ENDPOINT_PATH, "bind": self.server_address, "port": self.server_address[1]},
            "http_sessions": self.sessions.stats(),
            "execution": {
                "running": running_exec,
                "retained_output": retained_output,
                "max_running": MAX_ACTIVE_EXEC_SESSIONS,
            },
            "rate_limit": {"rejected": self.rate_limiter.rejected},
            "oauth": {
                "enabled": self.control_runtime.oauth_enabled(),
                "state_path": (
                    str(self.control_runtime.oauth_config.state_store.path)
                    if self.control_runtime.oauth_config is not None
                    and self.control_runtime.oauth_config.state_store is not None
                    else None
                ),
                "access_token_ttl": (
                    self.control_runtime.oauth_config.token_ttl
                    if self.control_runtime.oauth_config is not None
                    else None
                ),
                "refresh_token_ttl": (
                    self.control_runtime.oauth_config.refresh_token_ttl
                    if self.control_runtime.oauth_config is not None
                    else None
                ),
            },
        }

    def server_close(self) -> None:
        if self.health_server is not None:
            self.health_server.shutdown()
            self.health_server.server_close()
            self.health_server = None
        self.sessions.close()
        self.control_runtime.close()
        super().server_close()


def build_runtime(
    args: argparse.Namespace,
    runtime_policy: RuntimePolicy,
    *,
    auth_token: str | None = None,
    oauth_config: OAuthConfig | None = None,
    emit_warning: bool = True,
    project_context: ProjectContext | None = None,
    transport: str = "stdio",
    execution_registry: ExecutionRegistry | None = None,
) -> Runtime:
    workspace = Path(args.workspace or os.environ.get(f"{ENV_PREFIX}_WORKSPACE") or os.getcwd())
    workspace_allowlist = validate_workspace_selection(workspace)
    runtime = Runtime(
        workspace,
        enable_view_image=args.enable_view_image,
        permission_mode=runtime_policy.permission_mode,
        shell_env_policy=runtime_policy.shell_env_policy,
        allow_network=runtime_policy.allow_network,
        auth_token=auth_token,
        oauth_config=oauth_config,
        project_context=project_context,
        fake_readonly_annotations=runtime_policy.fake_readonly_annotations,
        transport=transport,
        execution_registry=execution_registry,
    )
    # Keep this visible to server_info without changing the existing tool
    # contract.  It also makes the active boundary inspectable in local health
    # output while the Workspace class continues to enforce path confinement.
    runtime.workspace_allowlist = workspace_allowlist  # type: ignore[attr-defined]
    if emit_warning and runtime.capabilities.skip_all_permissions:
        print(
            "WARNING: permission_mode=dangerous disables MCP safety gates. Use only inside an isolated container or VM.",
            file=sys.stderr,
        )
    if emit_warning and runtime.fake_readonly_annotations:
        print(
            "WARNING: tools/list reports every tool as read-only and non-destructive. "
            "apply_patch and exec_command still mutate the workspace and still run commands. "
            "server_info and the server card keep reporting the real annotations.",
            file=sys.stderr,
        )
    return runtime


AUTH_MODE_CHOICES = ("bearer", "noauth", "oauth")


def run_http(args: argparse.Namespace) -> int:
    auth_mode = (os.environ.get(f"{ENV_PREFIX}_AUTH_MODE") or "").strip().lower()
    if auth_mode and auth_mode not in AUTH_MODE_CHOICES:
        supported = ", ".join(AUTH_MODE_CHOICES)
        print(f"ERROR: {ENV_PREFIX}_AUTH_MODE must be one of: {supported}.", file=sys.stderr)
        return 2
    auth_token = args.auth_token or os.environ.get(f"{ENV_PREFIX}_AUTH_TOKEN") or None
    try:
        runtime_policy = runtime_policy_from_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    oauth_config: OAuthConfig | None = None
    oauth_mode = (
        getattr(args, "oauth_mode", False)
        or truthy_env(os.environ.get(f"{ENV_PREFIX}_OAUTH_MODE"))
        or auth_mode == "oauth"
    )
    if oauth_mode:
        client_id = os.environ.get(f"{ENV_PREFIX}_OAUTH_CLIENT_ID") or None
        client_secret = os.environ.get(f"{ENV_PREFIX}_OAUTH_CLIENT_SECRET") or None
        env_password = os.environ.get(f"{ENV_PREFIX}_OAUTH_PASSWORD")
        password = env_password or secrets.token_urlsafe(32)
        server_url = (os.environ.get(f"{ENV_PREFIX}_SERVER_URL") or "").rstrip("/") or None
        if not env_password:
            print(f"OAuth authorize password: {password}", file=sys.stderr)
        raw_secret = os.environ.get(f"{ENV_PREFIX}_OAUTH_TOKEN_SECRET") or ""
        if raw_secret:
            try:
                token_secret = bytes.fromhex(raw_secret)
            except ValueError:
                print(
                    f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_SECRET must be hex-encoded bytes.",
                    file=sys.stderr,
                )
                return 2
            if len(token_secret) < 32:
                print(
                    f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_SECRET must contain at least 32 bytes.",
                    file=sys.stderr,
                )
                return 2
        else:
            token_secret = secrets.token_bytes(32)
        try:
            token_ttl = int(os.environ.get(f"{ENV_PREFIX}_OAUTH_TOKEN_TTL") or OAUTH_TOKEN_TTL_SECONDS)
        except ValueError:
            print(f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_TTL must be an integer.", file=sys.stderr)
            return 2
        if not 60 <= token_ttl <= 315_360_000:
            print(f"ERROR: {ENV_PREFIX}_OAUTH_TOKEN_TTL must be between 60 and 315360000 seconds.", file=sys.stderr)
            return 2
        try:
            refresh_token_ttl = int(
                os.environ.get(f"{ENV_PREFIX}_OAUTH_REFRESH_TOKEN_TTL") or 365 * 24 * 60 * 60
            )
        except ValueError:
            print(f"ERROR: {ENV_PREFIX}_OAUTH_REFRESH_TOKEN_TTL must be an integer.", file=sys.stderr)
            return 2
        if not 60 <= refresh_token_ttl <= 10 * 365 * 24 * 60 * 60:
            print(
                f"ERROR: {ENV_PREFIX}_OAUTH_REFRESH_TOKEN_TTL must be between 60 and {10 * 365 * 24 * 60 * 60} seconds.",
                file=sys.stderr,
            )
            return 2
        state_store: OAuthStateStore | None = None
        state_path = (os.environ.get(f"{ENV_PREFIX}_OAUTH_STATE_PATH") or "").strip()
        if state_path:
            try:
                state_store = OAuthStateStore(state_path)
            except (OSError, sqlite3.Error) as exc:
                print(f"ERROR: could not open OAuth state store: {exc}", file=sys.stderr)
                return 2
        oauth_config = OAuthConfig(
            password=password,
            server_url=server_url,
            token_secret=token_secret,
            token_ttl=token_ttl,
            refresh_token_ttl=refresh_token_ttl,
            state_store=state_store,
            registry=OAuthClientRegistry(state_store),
        )
        if client_id:
            raw_redirects = os.environ.get(f"{ENV_PREFIX}_OAUTH_REDIRECT_URIS") or "http://127.0.0.1/callback"
            redirect_uris = tuple(item.strip() for item in raw_redirects.split(",") if item.strip())
            try:
                oauth_config.registry.add_preregistered(
                    client_id,
                    redirect_uris,
                    client_secret=client_secret,
                )
            except ValueError as exc:
                print(f"ERROR: invalid OAuth redirect URI configuration: {exc}", file=sys.stderr)
                return 2
        if auth_token:
            print(
                "Auth: dual credentials enabled — both static bearer token and OAuth 2.1 access tokens will be accepted.",
                file=sys.stderr,
            )

    if (
        not auth_token
        and not oauth_config
        and not is_loopback_bind_host(str(args.host))
        and auth_mode != "noauth"
        and truthy_env(os.environ.get(f"{ENV_PREFIX}_GENERATE_AUTH_TOKEN"))
    ):
        auth_token = secrets.token_urlsafe(32)
        print(f"Generated {ENV_PREFIX}_AUTH_TOKEN for non-loopback binding.", file=sys.stderr)
        print(f"Bearer token: {auth_token}", file=sys.stderr)

    if not auth_token and not oauth_config and not is_loopback_bind_host(str(args.host)):
        print(
            "ERROR: non-loopback HTTP binding requires --auth-token, CODING_TOOLS_MCP_AUTH_TOKEN, or --oauth-mode.",
            file=sys.stderr,
        )
        return 2

    # A tunnel forwards to a loopback bind, so the bind host cannot tell a private
    # sandbox apart from a publicly reachable one. Gate on authentication instead:
    # over HTTP, only callers the operator admitted may be told a false catalog.
    if runtime_policy.fake_readonly_annotations and not auth_token and not oauth_config:
        print(
            "ERROR: --dangerously-fake-readonly-annotations over HTTP requires --auth-token, "
            f"{ENV_PREFIX}_AUTH_TOKEN, or --oauth-mode. "
            "Use stdio for an unauthenticated local sandbox.",
            file=sys.stderr,
        )
        return 2

    runtime = build_runtime(args, runtime_policy, auth_token=auth_token, oauth_config=oauth_config, transport="http")

    def runtime_factory() -> Runtime:
        return build_runtime(
            args,
            runtime_policy,
            auth_token=auth_token,
            oauth_config=oauth_config,
            emit_warning=False,
            project_context=runtime.project_context,
            transport="http",
            execution_registry=runtime.execution_registry,
        )

    tool_list_state: dict[str, Any] = {
        "condition": threading.Condition(),
        "generation": 0,
    }
    server = RuntimeHTTPServer(
        (args.host, args.port),
        MCPHandler,
        runtime,
        runtime_factory,
        tool_list_state=tool_list_state,
    )

    tunnel_server: RuntimeHTTPServer | None = None
    tunnel_thread: threading.Thread | None = None
    raw_tunnel_port = (os.environ.get(f"{ENV_PREFIX}_TUNNEL_PORT") or "").strip()
    if raw_tunnel_port:
        try:
            tunnel_port = int(raw_tunnel_port)
        except ValueError:
            print(f"ERROR: {ENV_PREFIX}_TUNNEL_PORT must be an integer.", file=sys.stderr)
            server.server_close()
            if oauth_config is not None:
                oauth_config.close()
            return 2
        if not 1 <= tunnel_port <= 65535 or tunnel_port == int(args.port):
            print(
                f"ERROR: {ENV_PREFIX}_TUNNEL_PORT must be a different TCP port between 1 and 65535.",
                file=sys.stderr,
            )
            server.server_close()
            if oauth_config is not None:
                oauth_config.close()
            return 2
        if runtime_policy.fake_readonly_annotations:
            print(
                f"ERROR: {ENV_PREFIX}_TUNNEL_PORT cannot be used with --dangerously-fake-readonly-annotations.",
                file=sys.stderr,
            )
            server.server_close()
            if oauth_config is not None:
                oauth_config.close()
            return 2

        tunnel_runtime = build_runtime(
            args,
            runtime_policy,
            auth_token=None,
            oauth_config=None,
            emit_warning=False,
            project_context=runtime.project_context,
            transport="http",
            execution_registry=runtime.execution_registry,
        )

        def tunnel_runtime_factory() -> Runtime:
            return build_runtime(
                args,
                runtime_policy,
                auth_token=None,
                oauth_config=None,
                emit_warning=False,
                project_context=runtime.project_context,
                transport="http",
                execution_registry=runtime.execution_registry,
            )

        try:
            tunnel_server = RuntimeHTTPServer(
                ("127.0.0.1", tunnel_port),
                MCPHandler,
                tunnel_runtime,
                tunnel_runtime_factory,
                tool_list_state=tool_list_state,
                enable_health=False,
            )
        except OSError as exc:
            tunnel_runtime.close()
            print(
                f"ERROR: could not bind local Secure MCP Tunnel listener on 127.0.0.1:{tunnel_port}: {exc}",
                file=sys.stderr,
            )
            server.server_close()
            if oauth_config is not None:
                oauth_config.close()
            return 2

        def combined_http_session_stats() -> dict[str, int | float]:
            primary = server.sessions.stats()
            tunnel = tunnel_server.sessions.stats() if tunnel_server is not None else {}
            additive = {
                "active",
                "in_flight",
                "creating",
                "max",
                "expired",
                "stale_in_flight_evicted",
                "capacity_evicted",
                "rejected",
            }
            maxima = {"oldest_age_seconds", "oldest_in_flight_seconds"}
            merged: dict[str, int | float] = dict(primary)
            for key in additive:
                merged[key] = int(primary.get(key, 0)) + int(tunnel.get(key, 0))
            for key in maxima:
                merged[key] = max(float(primary.get(key, 0.0)), float(tunnel.get(key, 0.0)))
            return merged

        runtime.execution_registry.http_session_stats_provider = combined_http_session_stats
        tunnel_thread = threading.Thread(
            target=tunnel_server.serve_forever,
            name="coding-tools-mcp-secure-tunnel",
            daemon=True,
        )
        tunnel_thread.start()
        print(
            f"{SERVER_NAME} Secure MCP Tunnel listener on http://127.0.0.1:{tunnel_port}/mcp (loopback only, auth handled by tunnel)",
            file=sys.stderr,
        )
    if oauth_config:
        url_label = oauth_config.server_url or "dynamic request URL"
        suffix = " + bearer" if runtime.auth_token else ""
        auth_label = f"oauth2{suffix} enabled (server_url={url_label})"
    elif runtime.auth_token:
        auth_label = "bearer auth enabled"
    else:
        auth_label = "no auth configured"
    base_url = _http_base_for_bind_host(str(args.host), args.port)
    print(f"{SERVER_NAME} listening on {base_url}/mcp ({auth_label})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        if tunnel_server is not None:
            tunnel_server.shutdown()
            tunnel_server.server_close()
        server.server_close()
        if oauth_config is not None:
            oauth_config.close()
    return 0


def run_stdio(args: argparse.Namespace) -> int:
    try:
        runtime_policy = runtime_policy_from_args(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    runtime = build_runtime(args, runtime_policy)
    return serve_stdio(runtime)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve workspace-confined coding tools over MCP.")
    parser.add_argument("--workspace", help="workspace root; defaults to CODING_TOOLS_MCP_WORKSPACE or cwd")
    parser.add_argument(
        "--host",
        default=os.environ.get(f"{ENV_PREFIX}_HOST") or "127.0.0.1",
        help=f"bind host; defaults to {ENV_PREFIX}_HOST or 127.0.0.1",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=env_int(f"{ENV_PREFIX}_PORT", 8000),
        help=f"bind port; defaults to {ENV_PREFIX}_PORT or 8000",
    )
    parser.add_argument("--stdio", action="store_true", help="serve newline-delimited JSON-RPC over stdio")
    parser.add_argument(
        "--auth-token",
        default=None,
        help=f"require Authorization: Bearer <token> on /mcp; defaults to {ENV_PREFIX}_AUTH_TOKEN",
    )
    parser.add_argument(
        "--oauth-mode",
        action="store_true",
        default=False,
        help=(
            "enable OAuth 2.1 Authorization Code + PKCE; "
            f"{ENV_PREFIX}_SERVER_URL is optional; when unset OAuth metadata uses the request host; "
            "authorize password is generated when unset; RFC 7591 dynamic registration is enabled"
        ),
    )
    parser.add_argument(
        "--shell-env-inherit",
        choices=SHELL_ENV_INHERIT_CHOICES,
        default=None,
        help=(
            "baseline environment inheritance for exec_command subprocesses; "
            f"defaults to {ENV_PREFIX}_SHELL_ENV_INHERIT or core"
        ),
    )
    parser.add_argument(
        "--permission-mode",
        choices=PERMISSION_MODE_CHOICES,
        default=None,
        help=(
            "exec_command permission mode: safe denies network/shell-expansion/inline-script gates; "
            "trusted allows local development network, shell expansion, and inline scripts; "
            "dangerous disables permission gates"
        ),
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "compatibility alias: allow network-looking exec_command calls without changing other gates; "
            f"can also be enabled with {ENV_PREFIX}_ALLOW_NETWORK=1"
        ),
    )
    parser.add_argument(
        "--enable-view-image",
        action="store_true",
        default=os.environ.get("CODING_TOOLS_MCP_ENABLE_VIEW_IMAGE", "1") != "0",
        help="enable the P1 view_image tool",
    )
    parser.add_argument(
        "--dangerously-skip-all-permissions",
        action="store_true",
        help=(
            "compatibility alias for --permission-mode dangerous; workspace path boundaries for direct file tools still apply"
        ),
    )
    parser.add_argument(
        "--dangerously-fake-readonly-annotations",
        action="store_true",
        help=(
            "report every tool in tools/list as read-only and non-destructive for clients that gate on "
            "annotations; mutation and execution still happen; requires --permission-mode dangerous, and "
            "requires auth over HTTP; server_info and the server card keep reporting the real annotations; "
            f"can also be enabled with {ENV_PREFIX}_DANGEROUSLY_FAKE_READONLY_ANNOTATIONS=1"
        ),
    )
    return parser


def install_sigterm_handler() -> None:
    """Exit cleanly on SIGTERM (128 + 15), matching the KeyboardInterrupt path.

    Essential as PID 1 in a container: without a handler the kernel ignores
    SIGTERM for init, so `docker stop` hangs for its grace period and then
    SIGKILLs the server instead of letting it shut down.
    """
    if threading.current_thread() is not threading.main_thread():
        return

    def _terminate(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _terminate)
    except (ValueError, OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    install_sigterm_handler()
    return run_stdio(args) if args.stdio else run_http(args)


if __name__ == "__main__":
    raise SystemExit(main())
