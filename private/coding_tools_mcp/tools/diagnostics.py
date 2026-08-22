from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..envutils import ENV_PREFIX


ConfiguredToolPath = Callable[[str], str | None]


def exec_environment_summary(
    *,
    workspace_root: Path,
    permission_mode: str,
    network_allowed: bool,
    runtime_dir: Path,
    home_dir: Path,
    tmp_dir: Path,
    cache_dir: Path,
) -> dict[str, Any]:
    return {
        "workspace": str(workspace_root),
        "permission_mode": permission_mode,
        "network_allowed": network_allowed,
        "runtime_dir": str(runtime_dir),
        "home": str(home_dir),
        "tmpdir": str(tmp_dir),
        "cache_dir": str(cache_dir),
    }


def execution_session_summary(
    *,
    running: int,
    starting: int,
    retained_output: int,
    max_running: int,
) -> dict[str, Any]:
    return {
        "running": running,
        "starting": starting,
        "retained_output": retained_output,
        "max_running": max_running,
        "available_slots": max(0, max_running - running - starting),
    }


def landlock_enforced(landlock: dict[str, Any], *, enabled: bool) -> bool:
    return bool(landlock.get("available")) and enabled


def skill_catalog(workspace_root: Path) -> list[dict[str, str]]:
    """Discover workspace-bundled SKILL.md files without executing them."""
    roots = [
        workspace_root / "coding-tools-mcp" / "skills",
        workspace_root / "skills",
    ]
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    skill_files: list[Path] = []
    for root in roots:
        if root.is_dir():
            skill_files.extend(sorted(root.glob("*/SKILL.md")))
    for skill_file in skill_files:
        try:
            text = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        name = skill_file.parent.name
        description = ""
        if text.startswith("---"):
            header_end = text.find("\n---", 3)
            if header_end >= 0:
                header = text[3:header_end]
                for line in header.splitlines():
                    key, sep, value = line.partition(":")
                    if not sep:
                        continue
                    if key.strip() == "name" and value.strip():
                        name = value.strip().strip('"\'')
                    elif key.strip() == "description" and value.strip():
                        description = value.strip().strip('"\'')
        if name in seen:
            continue
        seen.add(name)
        items.append(
            {
                "name": name,
                "description": description,
                "path": skill_file.relative_to(workspace_root).as_posix(),
            }
        )
    return items


def discover_tools(
    names: list[str],
    *,
    configured_tool_path: ConfiguredToolPath,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    configured_pwsh = (os.environ.get(f"{ENV_PREFIX}_PWSH_PATH") or "").strip()
    for raw_name in names[:64]:
        name = str(raw_name).strip()
        if not name:
            continue
        candidates = [name]
        configured = configured_tool_path(name)
        if configured:
            candidates.insert(0, configured)
        if name.casefold() in {"pwsh", "powershell"} and configured_pwsh:
            candidates.insert(0, configured_pwsh)
        resolved = next((path for path in candidates if Path(path).is_file() or shutil.which(path)), None)
        results.append({"name": name, "available": bool(resolved), "path": resolved})
    return results


def server_info_payload(
    *,
    server: str,
    title: str,
    version: str,
    build_identity: dict[str, Any],
    protocol_version: str,
    exec_environment: dict[str, Any],
    workspace_allowlist: list[dict[str, str]],
    default_cwd: str,
    default_cwd_scope: str,
    auth_enabled: bool,
    oauth: dict[str, Any],
    dangerously_skip_all_permissions: bool,
    annotation_override: str | None,
    landlock: dict[str, Any],
    exec_policy: dict[str, str],
    shell_env_inherit: str,
    shell_env_include_only: list[str],
    shell_env_exclude: list[str],
    endpoint_path: str,
    project_context: dict[str, Any],
    skills: list[dict[str, str]],
    http_sessions: dict[str, int | float] | None,
    execution: dict[str, Any],
    tools: list[str],
) -> dict[str, Any]:
    """Build the stable server-info document from already-resolved domain state."""
    return {
        "server": server,
        "title": title,
        "version": version,
        "build_identity": build_identity,
        "protocol_version": protocol_version,
        **exec_environment,
        "workspace_allowlist": workspace_allowlist,
        "default_cwd": default_cwd,
        "default_cwd_scope": default_cwd_scope,
        "auth_enabled": auth_enabled,
        "oauth": oauth,
        "dangerously_skip_all_permissions": dangerously_skip_all_permissions,
        "annotation_override": annotation_override,
        "landlock": landlock,
        "exec_policy": exec_policy,
        "permission_elicitation_supported": True,
        "permission_approval_transport": "local_windows_broker",
        "shell_env_inherit": shell_env_inherit,
        "shell_env_include_only": shell_env_include_only,
        "shell_env_exclude": shell_env_exclude,
        "endpoint_path": endpoint_path,
        "project_context": project_context,
        "skills": skills,
        "http_sessions": http_sessions,
        "execution": execution,
        "tools": tools,
        "tool_count": len(tools),
    }


__all__ = [
    "discover_tools",
    "exec_environment_summary",
    "execution_session_summary",
    "landlock_enforced",
    "server_info_payload",
    "skill_catalog",
]
