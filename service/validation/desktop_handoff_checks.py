from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any


def run_desktop_handoff_checks(server: Any, runtime_module: Any, workspace: Path) -> None:
    human_runtime = server.Runtime(workspace, enable_view_image=False)
    try:
        human_runtime.initialized = True
        unknown_tool_response = server.dispatch_rpc(
            human_runtime,
            {
                "jsonrpc": "2.0",
                "id": "unknown-tool-regression",
                "method": "tools/call",
                "params": {"name": "definitely_removed_tool", "arguments": {}},
            },
        )
        unknown_error = (unknown_tool_response or {}).get("error", {})
        if unknown_error.get("code") != -32602 or "Unknown tool" not in str(unknown_error.get("message") or ""):
            raise RuntimeError("removed tools no longer fail cleanly with an Unknown tool JSON-RPC error")

        handoff_result = human_runtime.call_tool(
            "human_help_me",
            {
                "reason": "faster_by_human",
                "request": "Run one diagnostic command.",
                "expected_result": "Command output is visible.",
                "return_to_agent": "Paste the output.",
                "delivery": "chat_only",
            },
        )
        handoff = handoff_result.get("structuredContent", {})
        if handoff.get("status") != "human_action_required" or handoff.get("visibility") != "must_surface_to_user":
            raise RuntimeError("human_help_me did not produce a blocking handoff status")
        if handoff.get("request") != "Run one diagnostic command.":
            raise RuntimeError("human_help_me did not preserve the requested human action")
        rendered = "\n".join(
            str(item.get("text") or "")
            for item in handoff_result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        )
        if "HUMAN HELP NEEDED" not in rendered or "surface it in your next visible reply" not in rendered:
            raise RuntimeError("human_help_me model-facing handoff text is incomplete")
    finally:
        human_runtime.close()

    with tempfile.TemporaryDirectory(prefix="coding-tools-desktop-contract-") as temporary:
        desktop_runtime = server.Runtime(Path(temporary), enable_view_image=False)
        original_computer_use = runtime_module.request_computer_use
        original_human_help = runtime_module.request_human_help
        desktop_calls: list[dict[str, object]] = []
        human_help_calls: list[dict[str, object]] = []

        def fake_computer_use(**kwargs: object) -> dict[str, object]:
            desktop_calls.append(dict(kwargs))
            return {"ok": True, "action": kwargs.get("action")}

        def fake_human_help(**kwargs: object) -> dict[str, object]:
            human_help_calls.append(dict(kwargs))
            return {
                "ok": True,
                "outcome": "done",
                "answer": "",
                "execution_context": "active_user" if kwargs.get("delivery") == "desktop_only" else "web_console",
            }

        runtime_module.request_computer_use = fake_computer_use
        runtime_module.request_human_help = fake_human_help
        try:
            desktop_help = desktop_runtime.human_help_me(
                {
                    "reason": "gui_required",
                    "request": "Confirm the desktop-only prompt.",
                    "delivery": "desktop_only",
                }
            )
            auto_help = desktop_runtime.human_help_me(
                {
                    "reason": "gui_required",
                    "request": "Confirm the automatic prompt.",
                    "delivery": "auto",
                }
            )
            if [call.get("delivery") for call in human_help_calls] != ["desktop_only", "auto"]:
                raise RuntimeError("human_help_me no longer forwards the requested delivery policy")
            if desktop_help.get("delivery") != "desktop_qa":
                raise RuntimeError("desktop_only human help no longer reports desktop QA delivery")
            if auto_help.get("delivery") != "web_qa":
                raise RuntimeError("auto human help no longer preserves Web Console delivery")

            windows_result = desktop_runtime.computer_use(
                {
                    "action": "click",
                    "window_id": 42,
                    "x": 10,
                    "y": 20,
                    "include_screenshot": False,
                    "include_text": False,
                    "timeout_seconds": 12,
                }
            )
            browser_result = desktop_runtime.browser_use(
                {
                    "action": "navigate",
                    "url": "https://example.invalid/path",
                    "process_name": "chrome",
                }
            )
            if windows_result.get("surface") != "windows" or not str(windows_result.get("skill") or "").endswith("computer-use/SKILL.md"):
                raise RuntimeError("computer_use facade surface/skill metadata drifted")
            if browser_result.get("surface") != "browser" or not str(browser_result.get("skill") or "").endswith("control-chrome/SKILL.md"):
                raise RuntimeError("browser_use facade surface/skill metadata drifted")
            if len(desktop_calls) != 2:
                raise RuntimeError("desktop facade did not delegate exactly one broker call per tool call")
            windows_call, browser_call = desktop_calls
            if windows_call.get("browser_only") is not False or windows_call.get("window_id") != 42:
                raise RuntimeError("computer_use broker argument mapping drifted")
            if windows_call.get("x") != 10 or windows_call.get("y") != 20 or windows_call.get("timeout_seconds") != 12.0:
                raise RuntimeError("computer_use coordinate/timeout mapping drifted")
            if windows_call.get("include_screenshot") is not False or windows_call.get("include_text") is not False:
                raise RuntimeError("computer_use include flags drifted")
            if browser_call.get("browser_only") is not True or browser_call.get("action") != "navigate":
                raise RuntimeError("browser_use broker mode/action mapping drifted")
            if browser_call.get("text") != "https://example.invalid/path" or browser_call.get("process_name") != "chrome":
                raise RuntimeError("browser_use navigate URL mapping drifted")
        finally:
            runtime_module.request_computer_use = original_computer_use
            runtime_module.request_human_help = original_human_help
            desktop_runtime.close()
