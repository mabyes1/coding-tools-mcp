from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ToolFailure


@dataclass(frozen=True)
class PermissionGrant:
    grant_id: str
    owner: str
    workspace: str
    tool_name: str
    permission: str
    arguments_digest: str
    scope: str
    expires_at: float


class PermissionStore:
    """Reconnect-shared grant storage, independent from process/session state."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.grants: dict[str, PermissionGrant] = {}


class PermissionService:
    """Owner/workspace/argument-bound MCP permission grants."""

    def __init__(
        self,
        *,
        store: PermissionStore,
        workspace_root: Callable[[], Path],
        owner: Callable[[], str],
        dangerously_skip_all_permissions: bool,
        request_context: threading.local,
        request_approval: Callable[..., dict[str, Any]],
    ) -> None:
        self.store = store
        self.workspace_root = workspace_root
        self.owner = owner
        self.dangerously_skip_all_permissions = dangerously_skip_all_permissions
        self.request_context = request_context
        self.request_approval = request_approval

    @staticmethod
    def arguments_digest(arguments: dict[str, Any]) -> str:
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def granted(self, permission: str) -> bool:
        if self.dangerously_skip_all_permissions:
            return True
        claimed = getattr(self.request_context, "claimed_permission_grants", None)
        if isinstance(claimed, set) and permission in claimed:
            return True
        tool_name = str(getattr(self.request_context, "tool_name", ""))
        arguments = getattr(self.request_context, "arguments", None)
        if not tool_name or not isinstance(arguments, dict):
            return False
        owner = self.owner()
        workspace = os.path.normcase(str(self.workspace_root()))
        digest = self.arguments_digest(arguments)
        now = time.time()
        matched: PermissionGrant | None = None
        matched_id: str | None = None
        with self.store.lock:
            expired = [grant_id for grant_id, grant in self.store.grants.items() if grant.expires_at <= now]
            for grant_id in expired:
                self.store.grants.pop(grant_id, None)
            for grant_id, grant in self.store.grants.items():
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
                self.store.grants.pop(matched_id, None)
        if matched is None:
            return False
        if matched.scope == "once":
            claimed = getattr(self.request_context, "claimed_permission_grants", None)
            if not isinstance(claimed, set):
                claimed = set()
                self.request_context.claimed_permission_grants = claimed
            claimed.add(permission)
        return True

    def finish_request(self) -> None:
        self.request_context.claimed_permission_grants = set()

    def request(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.dangerously_skip_all_permissions:
            return {
                "ok": True,
                "status": "granted",
                "grant_id": "dangerously-skip-all-permissions",
                "expires_at": None,
                "constraints": {
                    "mode": "dangerously_skip_all_permissions",
                    "workspace": str(self.workspace_root()),
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
        approval = self.request_approval(
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
            owner=self.owner(),
            workspace=os.path.normcase(str(self.workspace_root())),
            tool_name=tool_name,
            permission=permission,
            arguments_digest=self.arguments_digest(requested_arguments),
            scope=scope,
            expires_at=expires_at,
        )
        with self.store.lock:
            self.store.grants[grant_id] = grant
        return {
            "ok": True,
            "status": "granted",
            "grant_id": grant_id,
            "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
            "constraints": {
                "tool_name": tool_name,
                "permission": permission,
                "scope": scope,
                "workspace": str(self.workspace_root()),
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


__all__ = ["PermissionGrant", "PermissionService", "PermissionStore"]
