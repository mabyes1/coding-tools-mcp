from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .tool_schemas import input_schemas, tool_output_schema


@dataclass(frozen=True)
class ToolSpec:
    """Single source of truth for one tool's title, description, and annotation hints.

    Handler methods on Runtime are named exactly after the tool. Input schemas live in
    input_schemas(), keyed by the same names. `error_status` is stamped on failure
    payloads, and `content_builder` converts a success payload into extra MCP
    content blocks (beyond the rendered text).
    """

    title: str
    description: str
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    error_status: str | None = None
    content_builder: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None
    gated_by: str | None = None
    """Name of a Runtime attribute that must be truthy for the tool to be exposed."""


def _image_content(payload: dict[str, Any]) -> list[dict[str, Any]]:
    encoded = str(payload.pop("_mcp_image_data", ""))
    return [
        {
            "type": "image",
            "data": encoded,
            "mimeType": str(payload.get("mime_type", "application/octet-stream")),
        }
    ]


def _computer_use_content(payload: dict[str, Any]) -> list[dict[str, Any]]:
    encoded = str(payload.pop("screenshot_base64", ""))
    if not encoded:
        return []
    screenshot = payload.get("screenshot") if isinstance(payload.get("screenshot"), dict) else {}
    return [
        {
            "type": "image",
            "data": encoded,
            "mimeType": str(screenshot.get("mime_type") or "image/png"),
        }
    ]


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "server_info": ToolSpec(
        title="Server info",
        description="Use first for MCP/infrastructure diagnosis: server/version, workspace, auth, policy, HTTP state, and active execution-session pressure.",
        read_only=True,
        idempotent=True,
    ),
    "human_help_me": ToolSpec(
        title="Human help me",
        description="Escalate one small step to the human. Prefer the desktop QA prompt; if chat fallback is returned, surface it visibly. Never offload ordinary agent work.",
        read_only=True,
    ),
    "computer_use": ToolSpec(
        title="Computer use",
        description="Control Windows apps through the signed-in desktop. Read coding-tools-mcp/skills/computer-use/SKILL.md before first use; re-observe after UI changes.",
        destructive=True,
        open_world=True,
        error_status="failed",
        content_builder=_computer_use_content,
    ),
    "browser_use": ToolSpec(
        title="Browser use",
        description="Inspect and control Chrome/Edge using signed-in browser state. Read coding-tools-mcp/skills/control-chrome/SKILL.md before first use.",
        destructive=True,
        open_world=True,
        error_status="failed",
        content_builder=_computer_use_content,
    ),
    "check_exec_environment": ToolSpec(
        title="Check exec environment",
        description="Use when command execution may fail: inspect sandbox/policy, available developer executables, and active execution-session pressure.",
        read_only=True,
        idempotent=True,
    ),
    "which_tools": ToolSpec(
        title="Discover tools",
        description="Use to check whether specific executables exist and resolve their exact paths.",
        read_only=True,
        idempotent=True,
    ),
    "get_default_cwd": ToolSpec(
        title="Get default cwd",
        description="Use to inspect the current default directory used by relative tool paths.",
        read_only=True,
        idempotent=True,
    ),
    "list_workspaces": ToolSpec(
        title="List workspaces",
        description="Use to see which explicitly allowlisted workspaces this connector may access.",
        read_only=True,
        idempotent=True,
    ),
    "switch_workspace": ToolSpec(
        title="Switch workspace",
        description="Use to switch to another allowlisted workspace; blocked while commands are running.",
        idempotent=True,
    ),
    "set_default_cwd": ToolSpec(
        title="Set default cwd",
        description="Enter a project. If the ChatGPT Web Project name is known, pass project_name to select a same-named first-level directory or workspace-root fallback. Persists across reconnects and relative paths.",
        idempotent=True,
    ),
    "read_file": ToolSpec(
        title="Read file",
        description="Use for a known UTF-8 file or line range. Relative paths use the default cwd; locate unknown files with list_files/search_text first.",
        read_only=True,
        idempotent=True,
    ),
    "list_dir": ToolSpec(
        title="List directory",
        description="Use to inspect a directory. Relative paths use the default cwd; use list_files for recursive or glob-based discovery.",
        read_only=True,
        idempotent=True,
    ),
    "list_files": ToolSpec(
        title="List files",
        description="Use to find files by path or glob. Relative paths use the default cwd; use search_text for file contents.",
        read_only=True,
        idempotent=True,
    ),
    "search_text": ToolSpec(
        title="Search text",
        description="Use to locate text or regex matches in UTF-8 files. Relative path scopes use the default cwd; results include paths and line numbers.",
        read_only=True,
        idempotent=True,
    ),
    "apply_patch": ToolSpec(
        title="Apply patch",
        description="Use for all direct file edits. Relative patch paths use the persistent default cwd, like other path tools; changes are validated and applied atomically.",
        destructive=True,
    ),
    "exec_command": ToolSpec(
        title="Execute command",
        description="Use for builds, tests, and scripts. execution_context=service is managed Session 0; active_user is one-shot in the signed-in non-elevated desktop. Never edit files.",
        destructive=True,
        open_world=True,
        error_status="failed",
    ),
    "write_stdin": ToolSpec(
        title="Write stdin",
        description="Use only with a running exec_command session. Send non-empty chars to stdin, or empty chars to wait/poll; it does not start or revive sessions.",
        ),
    "poll_session": ToolSpec(
        title="Poll session",
        description="Use to fetch only new stdout/stderr from a running command using reconnect-safe cursors.",
        read_only=True,
        idempotent=True,
    ),
    "kill_session": ToolSpec(
        title="Kill session",
        description="Use to stop a known running exec_command session when it is no longer needed or is stuck; terminal one-shot commands do not need killing.",
        destructive=True,
    ),
    "kill_tree": ToolSpec(
        title="Kill process tree",
        description="Use when stopping a command must also terminate all child processes it launched.",
        destructive=True,
    ),
    "list_sessions": ToolSpec(
        title="List sessions",
        description="Use to find active or retained command sessions, especially after a connector reconnect.",
        read_only=True,
        idempotent=True,
    ),
    "process_tree": ToolSpec(
        title="Process tree",
        description="Use to inspect child processes belonging to a managed command session before deciding what to stop.",
        read_only=True,
        idempotent=True,
    ),
    "read_output": ToolSpec(
        title="Read output",
        description="Use only when exec/write/kill returned an output_ref and more retained stdout/stderr is needed; page by byte offset instead of rerunning the command.",
        read_only=True,
        idempotent=True,
    ),
    "tail_output": ToolSpec(
        title="Tail output",
        description="Use when only the latest lines of retained command output matter.",
        read_only=True,
        idempotent=True,
    ),
    "find_output": ToolSpec(
        title="Find output",
        description="Use to search large retained command output for a term without reading the whole buffer.",
        read_only=True,
        idempotent=True,
    ),
    "git_status": ToolSpec(
        title="Git status",
        description="Use to inspect tracked, staged, modified, and untracked changes for the repository containing a path.",
        read_only=True,
        idempotent=True,
    ),
    "git_diff": ToolSpec(
        title="Git diff",
        description="Use to review exact staged or unstaged code changes before judging or finishing an edit.",
        read_only=True,
        idempotent=True,
    ),
    "git_log": ToolSpec(
        title="Git log",
        description="Use to inspect recent commit history for a repository or path without reading patch contents.",
        read_only=True,
        idempotent=True,
    ),
    "git_show": ToolSpec(
        title="Git show",
        description="Use to inspect one revision, commit metadata, and optionally its diff.",
        read_only=True,
        idempotent=True,
    ),
    "git_blame": ToolSpec(
        title="Git blame",
        description="Use to identify which commits/authors last changed specific lines of a file.",
        read_only=True,
        idempotent=True,
    ),
    "request_permissions": ToolSpec(
        title="Request permissions",
        description="After PERMISSION_REQUIRED, open a Windows approval dialog for the signed-in user. If granted, retry the same arguments; once/session grants are owner-scoped and expire.",
    ),
    "request_elevated_action": ToolSpec(
        title="Request elevated action",
        description="Use for a registered fixed admin/deployment action through the interactive elevated broker; arbitrary commands are rejected.",
        destructive=True,
        error_status="failed",
    ),
    "view_image": ToolSpec(
        title="View image",
        description="Use to visually inspect an image file from the workspace rather than reading its binary bytes.",
        read_only=True,
        idempotent=True,
        content_builder=_image_content,
        gated_by="enable_view_image",
    ),
}

# ChatGPT connector discovery currently surfaces at most 20 functions from
# this MCP. Keep the public catalog intentionally bounded so useful tools are
# never silently pushed out by lower-priority helpers. Implementations outside
# this list remain available internally for compatibility and tests.
PUBLIC_TOOL_NAMES = (
    "server_info",
    "human_help_me",
    "computer_use",
    "browser_use",
    "check_exec_environment",
    "get_default_cwd",
    "set_default_cwd",
    "read_file",
    "list_files",
    "search_text",
    "apply_patch",
    "exec_command",
    "write_stdin",
    "kill_session",
    "read_output",
    "git_status",
    "git_diff",
    "git_log",
    "request_permissions",
    "view_image",
)


def _validate_public_tool_catalog() -> None:
    if len(PUBLIC_TOOL_NAMES) > 20:
        raise RuntimeError("Public MCP tool catalog must not exceed the connector's 20-tool discovery budget.")
    if len(set(PUBLIC_TOOL_NAMES)) != len(PUBLIC_TOOL_NAMES):
        raise RuntimeError("Public MCP tool catalog contains duplicate names.")
    for name in PUBLIC_TOOL_NAMES:
        spec = TOOL_REGISTRY.get(name)
        if spec is None:
            raise RuntimeError(f"Public MCP tool is not registered: {name}")
        if not spec.title.strip() or not spec.description.strip():
            raise RuntimeError(f"Public MCP tool needs a concise title and description: {name}")
        if len(spec.description) > 200:
            raise RuntimeError(f"Public MCP tool description is too long (>200 chars): {name}")


_validate_public_tool_catalog()


def tool_definition(name: str, *, fake_readonly: bool = False) -> dict[str, Any]:
    schemas = input_schemas()
    annotations = tool_annotations(name, fake_readonly=fake_readonly)
    return {
        "name": name,
        "title": annotations["title"],
        "description": TOOL_REGISTRY[name].description,
        "inputSchema": schemas[name],
        "outputSchema": tool_output_schema(),
        "annotations": annotations,
    }


def tool_annotations(name: str, *, fake_readonly: bool = False) -> dict[str, Any]:
    """Return a tool's MCP annotations.

    ``fake_readonly`` serves clients that refuse to call, or prompt on every call
    to, a tool annotated as mutating, which no server-side permission mode can
    influence. It reports every tool as read-only and non-destructive even though
    `apply_patch` and `exec_command` still mutate and still execute. Only
    `tools/list` may pass it: `server_info` and the server card must keep
    reporting the real annotations so the override stays discoverable.
    """
    spec = TOOL_REGISTRY[name]
    if fake_readonly:
        return {
            "title": spec.title,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": spec.idempotent,
            "openWorldHint": False,
        }
    return {
        "title": spec.title,
        "readOnlyHint": spec.read_only,
        "destructiveHint": spec.destructive,
        "idempotentHint": spec.idempotent,
        "openWorldHint": spec.open_world,
    }


__all__ = [
    "PUBLIC_TOOL_NAMES",
    "TOOL_REGISTRY",
    "ToolSpec",
    "tool_annotations",
    "tool_definition",
]
