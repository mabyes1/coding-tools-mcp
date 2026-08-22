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
from .activity import (
    ACTIVITY_BEARER_RE,
    ACTIVITY_INLINE_SECRET_RE,
    ACTIVITY_LOG_LOCK,
    ACTIVITY_LOG_PATH,
    ACTIVITY_LOG_RETENTION_DAYS,
    ACTIVITY_LONG_VALUE_RE,
    ACTIVITY_REQUEST_BASE64_RE,
    _activity_log_lines,
    _activity_start_lines,
    _activity_tail,
    _prepare_activity_log_for_write,
    append_activity_log,
    append_activity_start,
    redact_for_trace,
    sanitize_activity_text,
)
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
from .errors import JsonRpcError, ToolFailure, summarize_exception
from .elevated_actions import ELEVATED_ACTIONS, request_elevated_action, request_permission_approval
from .execution import (
    ExecutionService,
    MAX_ACTIVE_EXEC_SESSIONS,
    add_exec_diagnostics,
    base_command_env,
    command_env,
    interactive_command_env,
)
from .http_server import MCPHandler, MCPHealthHandler, RuntimeHTTPServer, server_card_payload
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
from .oauth_http import OAUTH_TOKEN_AUTH_METHODS, OAuthHTTPMixin
from .patching import AtomicPatchCommitter
from .processes import (
    HARD_KILL_SIGNAL,
    SESSION_BUFFER_BYTES,
    ExecSession,
    process_group_popen_kwargs,
    process_tree_for_pid,
    truncate_output_bytes_tail,
)
from .permissions import PermissionGrant, PermissionService
from .session_store import (
    COMPLETED_SESSION_TTL_SECONDS,
    MAX_RETAINED_OUTPUT_SESSIONS,
    MAX_RUNTIME_OUTPUT_BYTES,
    ExecutionRegistry,
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
from .runtime_meta import SERVER_NAME, SERVER_TITLE, runtime_build_identity, runtime_version
from .runtime import Runtime
from .runtime_config import (
    ModeCapabilities,
    PERMISSION_MODE_CAPABILITIES,
    PERMISSION_MODE_CHOICES,
    RuntimePolicy,
    SHELL_ENV_INHERIT_CHOICES,
    ShellEnvPolicy,
    env_int,
    fake_readonly_annotations_from_args,
    parse_shell_env_set,
    permission_mode_from_args,
    runtime_policy_from_args,
    shell_env_policy_from_args,
    split_env_patterns,
)
from .runtime_support import (
    ECOSYSTEM_CACHE_ENV_NAMES,
    EXECUTABLE_ALLOWLIST_ENV,
    GIT_ENV_NAMES,
    KILL_SESSION_STATUSES,
    LITERAL_DIRECTORY_CHANGE_RE,
    PERMISSION_FAILURE_DIAGNOSTICS,
    POSIX_CORE_ENV_NAMES,
    RISKY_ENV_NAMES,
    RUNTIME_ROOT_DIR_NAME,
    SENSITIVE_ENV_RE,
    SENSITIVE_VALUE_RE,
    SPECIAL_DEVICE_PATHS,
    WINDOWS_CORE_ENV_NAMES,
    cached_which,
    configured_executable_allowlist,
    configured_runtime_root,
    configured_tool_path,
    diagnostic,
    env_pattern_matches,
    exec_output_diagnostics,
    fallback_runtime_dir_for_workspace,
    is_allowed_external_executable,
    is_core_command_env_name,
    is_filtered_env_var,
    is_risky_env_name,
    permission_failure_diagnostics,
    require_git,
    runtime_dir_for_workspace,
    runtime_parent_fallback_root,
    runtime_parent_root,
    structured_error_kind,
    truncate_evidence,
    workspace_runtime_hash,
)
from .sandbox import (
    DNS_RESOLVER_READ_ROOTS,
    GIT_READ_ROOTS,
    LANDLOCK_ACCESS_FS_EXECUTE,
    LANDLOCK_ACCESS_FS_IOCTL_DEV,
    LANDLOCK_ACCESS_FS_MAKE_BLOCK,
    LANDLOCK_ACCESS_FS_MAKE_CHAR,
    LANDLOCK_ACCESS_FS_MAKE_DIR,
    LANDLOCK_ACCESS_FS_MAKE_FIFO,
    LANDLOCK_ACCESS_FS_MAKE_REG,
    LANDLOCK_ACCESS_FS_MAKE_SOCK,
    LANDLOCK_ACCESS_FS_MAKE_SYM,
    LANDLOCK_ACCESS_FS_READ_DIR,
    LANDLOCK_ACCESS_FS_READ_FILE,
    LANDLOCK_ACCESS_FS_REFER,
    LANDLOCK_ACCESS_FS_REMOVE_DIR,
    LANDLOCK_ACCESS_FS_REMOVE_FILE,
    LANDLOCK_ACCESS_FS_TRUNCATE,
    LANDLOCK_ACCESS_FS_WRITE_FILE,
    LANDLOCK_CREATE_RULESET_VERSION,
    LANDLOCK_RULE_PATH_BENEATH,
    OS_METADATA_READ_FILES,
    SYS_LANDLOCK_ADD_RULE,
    SYS_LANDLOCK_CREATE_RULESET,
    SYSTEM_PATH_ROOT_PREFIXES,
    TOOLCHAIN_READ_ROOTS,
    LandlockPathBeneathAttr,
    LandlockRulesetAttr,
    _guard_allow_roots_cached,
    _resolved_system_path_root_prefixes,
    add_landlock_path,
    guard_allow_roots,
    is_default_system_path_root,
    landlock_abi_version,
    landlock_device_access,
    landlock_exec_argv,
    landlock_handled_access,
    landlock_path_allowed_access,
    landlock_status_payload,
    landlock_unavailable_warning,
    open_landlock_ruleset,
)
from .telemetry import SessionTelemetry
from .textutils import TextTruncation
from .tools.diagnostics import (
    discover_tools,
    exec_environment_summary,
    execution_session_summary,
    landlock_enforced,
    server_info_payload as build_server_info_payload,
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
from .transport_http import (
    MAX_HTTP_REQUEST_BYTES,
    MCP_ENDPOINT_PATH,
    HTTPSessionManager,
    SessionCapacityError,
    SlidingWindowRateLimiter,
    first_form_value as _first_form_value,
    first_header_value as _first_header_value,
    forwarded_header_param as _forwarded_header_param,
    http_base_for_bind_host as _http_base_for_bind_host,
    is_allowed_origin,
    is_loopback_bind_host,
    json_response_payload,
    safe_external_host as _safe_external_host,
    write_http_body_safely as _write_http_body_safely,
)
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


from .bootstrap import (
    AUTH_MODE_CHOICES,
    build_parser,
    build_runtime,
    install_sigterm_handler,
    main,
    run_http,
    run_stdio,
)


if __name__ == "__main__":
    raise SystemExit(main())
