from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def run_windows_deployment_checks(package_parent: Path, action_contract: dict[str, Any]) -> None:
    if os.name != "nt":
        return

    service_root = Path(__file__).resolve().parents[1]
    windows_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
    csc = windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    automation_candidates = list(
        (windows_root / "Microsoft.Net" / "assembly" / "GAC_MSIL" / "System.Management.Automation").glob(
            "*/System.Management.Automation.dll"
        )
    )
    automation_ref = automation_candidates[0] if automation_candidates else Path("__missing_System.Management.Automation.dll")
    launcher_source = service_root / "ElevatedBrokerLauncher.cs"
    interactive_launcher_source = service_root / "InteractiveBrokerLauncher.cs"
    helper_source = service_root / "ComputerUseHelper.cs"
    overlay_source = service_root / "ComputerUseOverlay.cs"
    activity_viewer_source = service_root / "ActivityLogViewer.cs"
    action_contract_source = package_parent / "coding_tools_mcp" / "computer-use-actions.json"
    elevated_install_text = (service_root / "install-elevated-broker.ps1").read_text(encoding="utf-8")
    interactive_install_text = (service_root / "install-interactive-broker.ps1").read_text(encoding="utf-8")
    internal_root = service_root / "internal"
    deployment_common_text = internal_root.joinpath("deployment-common.ps1").read_text(encoding="utf-8")
    deploy_text = internal_root.joinpath("deploy-coding-tools.ps1").read_text(encoding="utf-8")
    install_text = internal_root.joinpath("install-coding-tools.ps1").read_text(encoding="utf-8")
    repair_text = internal_root.joinpath("repair-coding-tools.ps1").read_text(encoding="utf-8")
    for label, script_text in (
        ("elevated installer", elevated_install_text),
        ("deployment common", deployment_common_text),
    ):
        normalized_script_text = script_text.casefold()
        if '/remove:g "${currentaccount}"' not in normalized_script_text:
            raise RuntimeError(f"{label} stopped removing signed-in-user write access from the elevated queue")
        for acl_fragment in ("*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "${localServiceSid}:(OI)(CI)M"):
            if acl_fragment.casefold() not in normalized_script_text:
                raise RuntimeError(f"{label} elevated queue ACL contract lost {acl_fragment}")
    for acl_fragment in ("*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "${localServiceSid}:(OI)(CI)M"):
        if acl_fragment not in interactive_install_text:
            raise RuntimeError(f"interactive broker queue ACL contract lost {acl_fragment}")
    common_consumers = {
        "deploy": (deploy_text, ("New-CodingToolsBrokerArtifactStage", "Install-CodingToolsBrokerArtifacts", "Start-CodingToolsPrivateServices")),
        "fresh install": (install_text, ("New-CodingToolsBrokerArtifactStage", "Install-CodingToolsBrokerArtifacts", "Start-CodingToolsPrivateServices")),
        "repair": (repair_text, ("Start-CodingToolsPrivateServices",)),
    }
    for label, (script_text, required_calls) in common_consumers.items():
        if "deployment-common.ps1" not in script_text:
            raise RuntimeError(f"{label} stopped loading deployment-common.ps1")
        for function_name in required_calls:
            if function_name not in script_text:
                raise RuntimeError(f"{label} stopped using shared deployment primitive {function_name}")
    for duplicated_name in (
        "Build-BrokerArtifacts",
        "Stop-PrivateServices",
        "Start-PrivateServices",
        "Set-InstalledBrokerPermissions",
        "Install-StagedBrokerArtifacts",
    ):
        if f"function {duplicated_name}" in deploy_text or f"function {duplicated_name}" in install_text:
            raise RuntimeError(f"deployment policy regressed to a local duplicate: {duplicated_name}")

    helper_refs = [
        windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "System.Web.Extensions.dll",
        windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "WPF" / "UIAutomationClient.dll",
        windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "WPF" / "UIAutomationTypes.dll",
        windows_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "WPF" / "WindowsBase.dll",
    ]
    missing_helper_inputs = [
        path
        for path in [
            csc,
            automation_ref,
            launcher_source,
            interactive_launcher_source,
            helper_source,
            overlay_source,
            activity_viewer_source,
            action_contract_source,
            *helper_refs,
        ]
        if not path.is_file()
    ]
    if missing_helper_inputs:
        raise RuntimeError(
            "Computer Use helper build inputs are missing: "
            + ", ".join(str(path) for path in missing_helper_inputs)
        )

    # Regression contracts come from bugs we actually hit in production.
    helper_text = helper_source.read_text(encoding="utf-8")
    interactive_broker_text = (service_root / "interactive-broker.ps1").read_text(encoding="utf-8")
    if "System.Management.Automation.Language.Parser]::ParseInput" not in interactive_broker_text:
        raise RuntimeError("active_user exec must reject PowerShell syntax errors before launching the child shell")
    for action in sorted(set(action_contract["computer_use"]) | set(action_contract["browser_use"])):
        if f'action == "{action}"' not in helper_text:
            raise RuntimeError(f"Computer Use backend has no implementation branch for advertised action: {action}")
    if 'right_click is not supported' in helper_text:
        raise RuntimeError("right_click regressed to a schema-only action")
    if 'if (action == "inspect") ActivateWindow' in helper_text:
        raise RuntimeError("inspect must not foreground the target window")
    capture_start = helper_text.find("private static Tuple<byte[], int, int> Capture")
    capture_end = helper_text.find("private static", capture_start + 20)
    if capture_start < 0 or "ActivateWindow(" in helper_text[capture_start:capture_end]:
        raise RuntimeError("screenshot capture must not foreground the target window")
    if "try { element.SetFocus(); return; }" in helper_text:
        raise RuntimeError("click must not report success when it only focused the element")
    if "computer-use-overlay-leases" not in helper_text:
        raise RuntimeError("Computer Use overlay must use per-operation leases")

    with tempfile.TemporaryDirectory(prefix="coding-tools-computer-use-build-") as helper_temp:
        (Path(helper_temp) / "computer-use-actions.json").write_bytes(action_contract_source.read_bytes())
        launcher_output = Path(helper_temp) / "elevated-broker-launcher.exe"
        launcher_compile = subprocess.run(
            [
                str(csc),
                "/nologo",
                "/target:winexe",
                "/optimize+",
                f"/out:{launcher_output}",
                f"/reference:{automation_ref}",
                str(launcher_source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if launcher_compile.returncode != 0 or not launcher_output.is_file():
            raise RuntimeError(
                "Elevated broker launcher failed to compile:\n" + launcher_compile.stdout[-8000:]
            )
        launcher_self_test = subprocess.run(
            [str(launcher_output), "--self-test"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if launcher_self_test.returncode != 0:
            raise RuntimeError(
                "Elevated broker launcher runtime self-test failed:\n" + launcher_self_test.stdout[-8000:]
            )

        interactive_launcher_output = Path(helper_temp) / "interactive-broker-launcher.exe"
        interactive_launcher_compile = subprocess.run(
            [
                str(csc),
                "/nologo",
                "/target:winexe",
                "/optimize+",
                f"/out:{interactive_launcher_output}",
                f"/reference:{automation_ref}",
                str(interactive_launcher_source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if interactive_launcher_compile.returncode != 0 or not interactive_launcher_output.is_file():
            raise RuntimeError(
                "Interactive broker launcher failed to compile:\n"
                + interactive_launcher_compile.stdout[-8000:]
            )
        interactive_launcher_self_test = subprocess.run(
            [str(interactive_launcher_output), "--self-test"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if interactive_launcher_self_test.returncode != 0:
            raise RuntimeError(
                "Interactive broker launcher runtime self-test failed:\n"
                + interactive_launcher_self_test.stdout[-8000:]
            )

        helper_output = Path(helper_temp) / "computer-use-helper.exe"
        compile_result = subprocess.run(
            [
                str(csc),
                "/nologo",
                "/target:exe",
                "/optimize+",
                f"/out:{helper_output}",
                *(f"/reference:{path}" for path in helper_refs),
                "/reference:System.Drawing.dll",
                "/reference:System.Windows.Forms.dll",
                str(helper_source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if compile_result.returncode != 0 or not helper_output.is_file():
            raise RuntimeError(
                "Computer Use helper failed to compile:\n" + compile_result.stdout[-8000:]
            )
        request_json = json.dumps(
            {
                "action": "list_windows",
                "browser_only": False,
                "include_screenshot": False,
                "include_text": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        helper_smoke = subprocess.run(
            [str(helper_output), "--request-base64", base64.b64encode(request_json).decode("ascii")],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
        )
        if helper_smoke.returncode != 0:
            raise RuntimeError("Computer Use list_windows smoke test failed:\n" + helper_smoke.stdout[-8000:])
        try:
            smoke_payload = json.loads(helper_smoke.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Computer Use list_windows smoke test returned invalid JSON") from exc
        if not smoke_payload.get("ok") or smoke_payload.get("action") != "list_windows":
            raise RuntimeError("Computer Use list_windows smoke test returned an invalid payload")

        activity_viewer_output = Path(helper_temp) / "activity-log-viewer.exe"
        viewer_compile = subprocess.run(
            [
                str(csc),
                "/nologo",
                "/target:winexe",
                "/optimize+",
                f"/out:{activity_viewer_output}",
                "/reference:System.Drawing.dll",
                "/reference:System.Windows.Forms.dll",
                str(activity_viewer_source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if viewer_compile.returncode != 0 or not activity_viewer_output.is_file():
            raise RuntimeError(
                "Activity Log viewer failed to compile:\n" + viewer_compile.stdout[-8000:]
            )
