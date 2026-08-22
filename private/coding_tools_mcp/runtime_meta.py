from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from . import __version__


SERVER_NAME = "coding-tools-mcp"
SERVER_TITLE = "Coding Tools MCP"


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


__all__ = ["SERVER_NAME", "SERVER_TITLE", "runtime_build_identity", "runtime_version"]
