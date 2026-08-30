from __future__ import annotations

"""Live, disposable Computer Use and Browser Use action tests for Windows."""

import argparse
import base64
import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


ASSETS = Path(__file__).with_name("test-assets")
UI_TITLE = "Coding Tools UI Action Harness"
BROWSER_START_TITLE = "Coding Tools Browser Start"
BROWSER_TITLE = "Coding Tools Browser Surface"


def log(status: str, name: str, detail: str = "") -> None:
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}", flush=True)


def require(condition: Any, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def find_csc() -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        windows / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    )
    for path in candidates:
        if path.is_file():
            return path
    raise RuntimeError(".NET Framework C# compiler was not found")


def find_chrome() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    resolved = shutil.which("chrome.exe")
    if resolved:
        return Path(resolved)
    raise RuntimeError("Google Chrome was not found")


def element_index(payload: dict[str, Any], *, name: str = "", automation_id: str = "", control_type: str = "") -> int:
    rows = payload.get("elements") if isinstance(payload.get("elements"), list) else []
    for row in rows:
        if not isinstance(row, dict) or row.get("offscreen") is True:
            continue
        if name and name.lower() not in str(row.get("name") or "").lower():
            continue
        if automation_id and automation_id.lower() != str(row.get("automation_id") or "").lower():
            continue
        if control_type and control_type.lower() != str(row.get("type") or "").lower():
            continue
        return int(row["index"])
    summary = [{key: row.get(key) for key in ("index", "type", "name", "automation_id", "offscreen")} for row in rows[:80] if isinstance(row, dict)]
    raise RuntimeError(f"UI element was not found: name={name!r} automation_id={automation_id!r} type={control_type!r}; elements={summary}")


def has_element_name(payload: dict[str, Any], expected: str) -> bool:
    return any(
        expected.lower() in str(row.get("name") or "").lower()
        for row in payload.get("elements", [])
        if isinstance(row, dict)
    )


def wait_window(request: Any, title: str, *, browser_only: bool, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = request(
            action="list_windows", browser_only=browser_only,
            include_screenshot=False, include_text=False, timeout_seconds=20,
        )
        matches = [row for row in last.get("windows", []) if title.lower() in str(row.get("title") or "").lower()]
        if len(matches) == 1:
            return matches[0]
        time.sleep(0.25)
    raise RuntimeError(f"window did not appear uniquely: {title!r}; last={last}")


def call(request: Any, name: str, **kwargs: Any) -> dict[str, Any]:
    payload = request(timeout_seconds=30, **kwargs)
    require(payload.get("ok") is True, f"{name}: {payload}")
    log("PASS", name)
    return payload


def verify_status(request: Any, *, window_id: int, browser_only: bool, expected: str) -> dict[str, Any]:
    payload = request(
        action="inspect", window_id=window_id, browser_only=browser_only,
        include_screenshot=False, include_text=True, timeout_seconds=30,
    )
    require(has_element_name(payload, expected), f"observable status {expected!r} was not found")
    return payload


def run_surface(request: Any, *, window: dict[str, Any], browser_only: bool) -> None:
    prefix = "browser_use" if browser_only else "computer_use"
    window_id = int(window["id"])
    call(request, f"{prefix}.list_windows", action="list_windows", browser_only=browser_only, include_screenshot=False, include_text=False)
    observed = call(request, f"{prefix}.inspect", action="inspect", window_id=window_id, browser_only=browser_only, include_screenshot=False, include_text=True)
    shot = call(request, f"{prefix}.screenshot", action="screenshot", window_id=window_id, browser_only=browser_only, include_screenshot=True, include_text=False)
    require(len(base64.b64decode(str(shot.get("screenshot_base64") or ""))) > 1000, f"{prefix} screenshot was empty")
    call(request, f"{prefix}.activate", action="activate", window_id=window_id, browser_only=browser_only, include_screenshot=False, include_text=False)

    button = element_index(observed, name="Click test button", control_type="Button")
    call(request, f"{prefix}.click", action="click", window_id=window_id, browser_only=browser_only, element_index=button, include_screenshot=False, include_text=True)
    observed = verify_status(request, window_id=window_id, browser_only=browser_only, expected="STATUS: CLICKED")

    right_area = element_index(observed, name="Right click test area")
    call(request, f"{prefix}.right_click", action="right_click", window_id=window_id, browser_only=browser_only, element_index=right_area, include_screenshot=False, include_text=True)
    observed = verify_status(request, window_id=window_id, browser_only=browser_only, expected="STATUS: RIGHT_CLICKED")
    call(request, f"{prefix}.dismiss_context_menu", action="press_key", window_id=window_id, browser_only=browser_only, key="ESC", include_screenshot=False, include_text=False)

    if browser_only:
        input_index = element_index(observed, name="Test input", control_type="Edit")
    else:
        input_index = element_index(observed, automation_id="TestInput", control_type="Edit")
    call(request, f"{prefix}.focus_input", action="click", window_id=window_id, browser_only=browser_only, element_index=input_index, include_screenshot=False, include_text=False)
    call(request, f"{prefix}.type_text", action="type_text", window_id=window_id, browser_only=browser_only, element_index=input_index, text="CODING_TOOLS_TYPED", include_screenshot=False, include_text=True)
    observed = verify_status(request, window_id=window_id, browser_only=browser_only, expected="STATUS: TEXT=CODING_TOOLS_TYPED")
    call(request, f"{prefix}.press_key", action="press_key", window_id=window_id, browser_only=browser_only, key="F2", include_screenshot=False, include_text=True)
    observed = verify_status(request, window_id=window_id, browser_only=browser_only, expected="STATUS: KEY=F2")

    scroll_name = "Scrollable test area" if browser_only else "Scrollable test list"
    scroll_index = element_index(observed, name=scroll_name)
    call(request, f"{prefix}.scroll", action="scroll", window_id=window_id, browser_only=browser_only, element_index=scroll_index, scroll_y=600, include_screenshot=False, include_text=True)


def terminate_tree(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill.exe", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        process.terminate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package-parent", type=Path, default=Path(__file__).parents[1] / "private")
    args = parser.parse_args()
    sys.path.insert(0, str(args.package_parent.resolve()))
    from coding_tools_mcp.interactive_exec import request_computer_use

    failures: list[str] = []
    harness_process: subprocess.Popen[Any] | None = None
    chrome_process: subprocess.Popen[Any] | None = None
    server: http.server.ThreadingHTTPServer | None = None
    with tempfile.TemporaryDirectory(prefix="coding-tools-interactive-") as temporary:
        root = Path(temporary)
        try:
            harness = root / "interactive-surface-harness.exe"
            compile_result = subprocess.run(
                [str(find_csc()), "/nologo", "/target:winexe", f"/out:{harness}", "/reference:System.Drawing.dll", "/reference:System.Windows.Forms.dll", str(ASSETS / "InteractiveSurfaceHarness.cs")],
                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
            )
            require(compile_result.returncode == 0 and harness.is_file(), f"harness compile failed: {compile_result.stdout}")
            harness_process = subprocess.Popen([str(harness)])
            ui_window = wait_window(request_computer_use, UI_TITLE, browser_only=False)
            try:
                run_surface(request_computer_use, window=ui_window, browser_only=False)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"computer_use: {exc}")
                log("FAIL", "computer_use", str(exc))

            handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ASSETS))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            port = int(server.server_address[1])
            profile = root / "chrome-profile"
            chrome_process = subprocess.Popen([
                str(find_chrome()), f"--user-data-dir={profile}", "--new-window", "--no-first-run",
                "--disable-default-apps", "--force-renderer-accessibility",
                f"http://127.0.0.1:{port}/browser-surface-start.html",
            ])
            browser_window = wait_window(request_computer_use, BROWSER_START_TITLE, browser_only=True, timeout=30)
            call(
                request_computer_use, "browser_use.navigate", action="navigate", window_id=int(browser_window["id"]),
                browser_only=True, text=f"http://127.0.0.1:{port}/browser-surface.html",
                include_screenshot=False, include_text=True,
            )
            browser_window = wait_window(request_computer_use, BROWSER_TITLE, browser_only=True, timeout=20)
            try:
                run_surface(request_computer_use, window=browser_window, browser_only=True)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"browser_use: {exc}")
                log("FAIL", "browser_use", str(exc))
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            terminate_tree(chrome_process)
            terminate_tree(harness_process)

    counts = {"PASS": 2 - len(failures), "FAIL": len(failures)}
    print("INTERACTIVE_TEST_SUMMARY " + json.dumps({"counts": counts, "failures": failures}, ensure_ascii=False), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
