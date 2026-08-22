from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


def run_http_lifecycle_checks(server: Any) -> None:
    with tempfile.TemporaryDirectory(prefix="coding-tools-http-lifecycle-") as temporary:
        http_workspace = Path(temporary)
        control_runtime = server.Runtime(http_workspace, enable_view_image=False)
        registry = control_runtime.execution_registry

        def http_runtime_factory() -> Any:
            return server.Runtime(
                http_workspace,
                enable_view_image=False,
                project_context=control_runtime.project_context,
                execution_registry=registry,
                transport="http",
            )

        http_server = server.RuntimeHTTPServer(
            ("127.0.0.1", 0),
            server.MCPHandler,
            control_runtime,
            http_runtime_factory,
            enable_health=False,
        )
        reconnect_binding = http_server.sessions.create("http-lifecycle-owner")
        reconnect_runtime = reconnect_binding.runtime
        if reconnect_runtime.execution_registry is not registry:
            raise RuntimeError("HTTP reconnect Runtime did not share the control ExecutionRegistry")
        if reconnect_runtime._owns_execution_registry:
            raise RuntimeError("HTTP reconnect Runtime incorrectly owns the shared ExecutionRegistry")
        if registry.closed:
            raise RuntimeError("HTTP lifecycle setup unexpectedly closed the ExecutionRegistry")
        http_server.server_close()
        if not registry.closed:
            raise RuntimeError("RuntimeHTTPServer.server_close did not close the control ExecutionRegistry")
        if not control_runtime._closed:
            raise RuntimeError("RuntimeHTTPServer.server_close did not close its control Runtime")
        if not reconnect_runtime._closed:
            raise RuntimeError("RuntimeHTTPServer.server_close did not close reconnect session Runtimes")
