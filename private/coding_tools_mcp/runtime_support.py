from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .envutils import ENV_PREFIX
from .errors import ToolFailure
from .tool_schemas import INLINE_SCRIPT_PERMISSION
from .workspace import is_relative_to

SENSITIVE_ENV_RE = re.compile(r"(token|secret|credential|api[_-]?key|password|passwd|private)", re.I)

SENSITIVE_VALUE_RE = re.compile(
    r"(COMPLIANCE_SHOULD_NOT_LEAK|-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16})"
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

EXECUTABLE_ALLOWLIST_ENV = f"{ENV_PREFIX}_EXECUTABLE_ALLOWLIST"

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

KILL_SESSION_STATUSES = ("terminated", "killed", "exited", "terminating", "not_found")

POSIX_CORE_ENV_NAMES = {"PATH", "LANG", "LC_ALL", "TERM"}

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

RUNTIME_ROOT_DIR_NAME = "coding-tools-mcp"

SPECIAL_DEVICE_PATHS = ("/dev/null", "/dev/zero", "/dev/random", "/dev/urandom")

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

def require_git() -> str:
    git = cached_which("git")
    if not git:
        raise ToolFailure("GIT_ERROR", "git executable not found.", category="runtime")
    return git
