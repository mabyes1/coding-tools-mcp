from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any


def run_windows_runtime_checks(
    server: Any,
    elevated_actions: Any,
    workspace: Path,
    project_context: Any,
) -> None:
    if os.name != "nt":
        return

    with tempfile.TemporaryDirectory(prefix="coding-tools-broker-check-") as temporary:
        queue = Path(temporary)
        (queue / "broker.pid").write_text(str(os.getpid()), encoding="ascii")
        (queue / "broker.heartbeat").write_text(str(time.time()), encoding="ascii")
        alive, reported_pid = elevated_actions._broker_is_alive(queue)
        if not alive or reported_pid != os.getpid():
            raise RuntimeError("Windows broker process liveness probe rejected a live PID")

    runtime = server.Runtime(workspace, enable_view_image=False, project_context=project_context)
    try:
        command_env = {key.upper(): value for key, value in runtime._command_env({}).items()}
        required_windows_env = {
            "PROGRAMFILES",
            "PROGRAMFILES(X86)",
            "PROGRAMW6432",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "HOMEDRIVE",
            "HOMEPATH",
            "DOTNET_CLI_HOME",
            "NUGET_PACKAGES",
        }
        missing_windows_env = sorted(required_windows_env.difference(command_env))
        if missing_windows_env:
            raise RuntimeError(
                "Windows command environment is missing developer-tool profile variables: "
                + ", ".join(missing_windows_env)
            )

        _, interactive_policy = runtime._interactive_command_env({})
        interactive_core = {str(name).upper() for name in interactive_policy.get("core_names", [])}
        required_interactive_windows_env = {"SYSTEMDRIVE", "PROGRAMDATA", "ALLUSERSPROFILE"}
        missing_interactive_env = sorted(required_interactive_windows_env.difference(interactive_core))
        if missing_interactive_env:
            raise RuntimeError(
                "Interactive-user core environment is missing Windows known-folder variables: "
                + ", ".join(missing_interactive_env)
            )
        runtime_prefix = str(runtime.runtime_dir).rstrip("\\/").casefold() + "\\"
        for name in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "DOTNET_CLI_HOME", "NUGET_PACKAGES"):
            value = str(command_env[name]).casefold()
            if not value.startswith(runtime_prefix):
                raise RuntimeError(f"{name} must remain isolated inside the MCP runtime directory")
    finally:
        runtime.close()
