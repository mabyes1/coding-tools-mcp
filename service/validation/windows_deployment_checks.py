from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


def run_windows_deployment_checks(
    package_parent: Path,
    action_contract: dict[str, Any],
    *,
    include_desktop_surfaces: bool = True,
) -> None:
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
    web_console_bridge_source = service_root / "WebConsoleBridge.cs"
    browser_extension_root = service_root.parent / "browser-extension"
    action_contract_source = package_parent / "coding_tools_mcp" / "computer-use-actions.json"
    elevated_install_text = (service_root / "install-elevated-broker.ps1").read_text(encoding="utf-8")
    interactive_install_text = (service_root / "install-interactive-broker.ps1").read_text(encoding="utf-8")
    internal_root = service_root / "internal"
    deployment_common_text = internal_root.joinpath("deployment-common.ps1").read_text(encoding="utf-8")
    deploy_text = internal_root.joinpath("deploy-coding-tools.ps1").read_text(encoding="utf-8")
    install_text = internal_root.joinpath("install-coding-tools.ps1").read_text(encoding="utf-8")
    repair_text = internal_root.joinpath("repair-coding-tools.ps1").read_text(encoding="utf-8")
    mcp_service_xml_text = (service_root / "WebGPTCodingToolsMCP.xml").read_text(encoding="utf-8")
    openai_tunnel_service_xml_text = (service_root / "OpenAITunnelClient.xml").read_text(encoding="utf-8")
    openai_tunnel_config_text = (service_root / "tunnel-client.yaml").read_text(encoding="utf-8")
    openai_tunnel_config = json.loads(openai_tunnel_config_text)
    gitignore_text = service_root.parent.joinpath(".gitignore").read_text(encoding="utf-8")
    bootstrap_text = (package_parent / "coding_tools_mcp" / "bootstrap.py").read_text(encoding="utf-8")
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

    interactive_e2e_start = deploy_text.find("function Test-InstalledInteractiveExecE2E")
    interactive_e2e_end = deploy_text.find("\nfunction ", interactive_e2e_start + 10)
    interactive_e2e_text = deploy_text[interactive_e2e_start:interactive_e2e_end if interactive_e2e_end >= 0 else None]
    if "AddSeconds(35)" not in interactive_e2e_text or "Start-Sleep -Milliseconds 500" not in interactive_e2e_text:
        raise RuntimeError("interactive exec deployment E2E must tolerate the broker heartbeat reconstruction window")
    if '& $serverPython -c $code' in deploy_text:
        raise RuntimeError("deployment Python smoke tests must not pass multiline code through Windows PowerShell native -c quoting")
    if "function New-DeploymentPythonSmokeScript" not in deploy_text:
        raise RuntimeError("deployment Python smoke tests lost their temp-script execution helper")
    if deploy_text.count("& $serverPython $scriptPath") < 2:
        raise RuntimeError("deployment Python smoke tests must execute temp .py files for both Computer Use and interactive exec")

    drain_index = deploy_text.find('Write-Host "Waiting $StartDelaySeconds second(s) before restarting MCP')
    busy_check_index = deploy_text.find("Assert-NoActiveMcpWork", deploy_text.find("if (-not $ValidateOnly"))
    if drain_index < 0 or busy_check_index < 0 or drain_index > busy_check_index:
        raise RuntimeError("deployment must drain the triggering MCP call before checking for active work")
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

    for retained_listener_contract in (
        "Get-NetTCPConnection -State Listen",
        "$reservedPorts = @(8765, 8766, 8767)",
        "Get-CimInstance Win32_Process",
        "Refusing to kill it automatically",
        "Stop-Process -Id $listenerPid -Force",
        "Timed out clearing retained private MCP listeners",
    ):
        if retained_listener_contract not in deployment_common_text:
            raise RuntimeError(
                "service stop lifecycle lost stale MCP listener cleanup contract: "
                + retained_listener_contract
            )

    for readiness_contract in (
        "Invoke-CodingToolsTunnelServerInfo",
        "Get-CodingToolsServerPackageVersion",
        "$ServerInfo.build_identity.package_version",
        "Set-CodingToolsMcpRecoveryPolicy",
        'method = "tools/call"',
        'name = "server_info"',
        '"MCP-Protocol-Version" = "2026-07-28"',
        "health/tunnel version mismatch",
        "health/tunnel workspace mismatch",
        "restart/5000/restart/10000/restart/30000",
        "failureflag WebGPTCodingToolsMCP 1",
    ):
        if readiness_contract not in deployment_common_text:
            raise RuntimeError(f"service readiness lost real Tunnel MCP handshake contract: {readiness_contract}")
    if deployment_common_text.count("Get-CodingToolsServerPackageVersion $serverInfo") < 2:
        raise RuntimeError(
            "service readiness must compare canonical package versions for both health matching and expected-version validation"
        )
    if "$serverInfo.version -ne $health.version" in deployment_common_text:
        raise RuntimeError("service readiness regressed to comparing display/build version against package health version")

    for tunnel_independence_contract in (
        "[switch]$IncludeLegacyCloudflare",
        "[switch]$RequireLegacyCloudflare",
        "Secure MCP Tunnel on 8767 is healthy and remains available",
    ):
        if tunnel_independence_contract not in deployment_common_text:
            raise RuntimeError(
                "Secure MCP Tunnel lifecycle regained a hard dependency on legacy Cloudflare: "
                + tunnel_independence_contract
            )
    if "Stop-CodingToolsPrivateServices 15 -IncludeLegacyCloudflare -IncludeSecureTunnel" not in install_text:
        raise RuntimeError("fresh install must explicitly stop both replaceable tunnel services before replacing them")
    if 'foreach ($serviceName in @("WebGPTCloudflareTunnel", "OpenAITunnelClient", "WebGPTCodingToolsMCP"))' not in install_text:
        raise RuntimeError("fresh install must delete the old OpenAITunnelClient SCM registration before replacing the service root")
    for tunnel_preservation_contract in (
        "web-gpt-openai-tunnel-",
        "$hadTunnelBackup = $false",
        'Join-Path $serviceRoot "tunnel"',
        "Copy-Item -LiteralPath $tunnelBackupRoot -Destination (Join-Path $serviceRoot \"tunnel\") -Recurse -Force",
        "Remove-Item -LiteralPath $tunnelBackupRoot -Recurse -Force -ErrorAction SilentlyContinue",
    ):
        if tunnel_preservation_contract not in install_text:
            raise RuntimeError(f"fresh install lost machine-level tunnel preservation contract: {tunnel_preservation_contract}")
    if "Start-CodingToolsPrivateServices -RequireLegacyCloudflare" in install_text:
        raise RuntimeError("fresh install must not let legacy Cloudflare failure block Secure Tunnel installation")
    if "RequireLegacyCloudflare" in deploy_text or "IncludeLegacyCloudflare" in deploy_text:
        raise RuntimeError("normal deploy/rollback must not make Secure MCP Tunnel health depend on legacy Cloudflare")
    if "IncludeSecureTunnel" in deploy_text:
        raise RuntimeError("normal deploy/rollback must leave a healthy OpenAITunnelClient running across the MCP restart")

    for secure_service_consumer, secure_service_text in (
        ("deploy", deploy_text),
        ("fresh install", install_text),
        ("repair", repair_text),
    ):
        if "Ensure-OpenAITunnelClientService" not in secure_service_text:
            raise RuntimeError(f"{secure_service_consumer} stopped ensuring the OpenAITunnelClient SCM service")

    for secure_tunnel_contract in (
        "Get-LegacyOpenAITunnelMigrationSource",
        "Initialize-OpenAITunnelClientFiles",
        "Ensure-OpenAITunnelClientService",
        "Set-OpenAITunnelClientRecoveryPolicy",
        "Wait-OpenAITunnelClientReady",
        'Get-ScheduledTask -TaskName "Tunnel-Coding"',
        'Stop-ScheduledTask -TaskName "Tunnel-Coding"',
        'Unregister-ScheduledTask -TaskName "Tunnel-Coding"',
        'obj= "NT AUTHORITY\\LocalService"',
        "depend= WebGPTCodingToolsMCP",
        '"${LocalServiceSid}:R"',
        '"${LocalServiceSid}:(OI)(CI)RX"',
        '"${LocalServiceSid}:(OI)(CI)M"',
        'http://127.0.0.1:8769',
        '"$baseUrl/readyz"',
        "--require-control-plane-poll",
        "restart/5000/restart/10000/restart/30000",
        "failureflag OpenAITunnelClient 1",
    ):
        if secure_tunnel_contract not in deployment_common_text:
            raise RuntimeError(f"OpenAITunnelClient lifecycle contract missing: {secure_tunnel_contract}")

    ready_index = deployment_common_text.find("Wait-OpenAITunnelClientReady $ServiceRoot")
    unregister_legacy_index = deployment_common_text.find('Unregister-ScheduledTask -TaskName "Tunnel-Coding"')
    if ready_index < 0 or unregister_legacy_index < 0 or ready_index > unregister_legacy_index:
        raise RuntimeError("legacy Tunnel-Coding task must remain available until the new SCM service is actually ready")

    for xml_contract in (
        "<id>OpenAITunnelClient</id>",
        "<executable>%BASE%\\tunnel\\tunnel-client.exe</executable>",
        '<arguments>run --config "%BASE%\\tunnel\\tunnel-client.yaml"</arguments>',
        "<startmode>Automatic</startmode>",
        "<depend>WebGPTCodingToolsMCP</depend>",
        '<onfailure action="restart" delay="5 sec" />',
        '<onfailure action="restart" delay="10 sec" />',
        '<onfailure action="restart" delay="30 sec" />',
        "<resetfailure>1 hour</resetfailure>",
    ):
        if xml_contract not in openai_tunnel_service_xml_text:
            raise RuntimeError(f"OpenAITunnelClient WinSW contract missing: {xml_contract}")
    if "--control-plane.api-key" in openai_tunnel_service_xml_text or "sk-" in openai_tunnel_service_xml_text:
        raise RuntimeError("OpenAITunnelClient must never place its runtime API key on the service command line")

    expected_secret_ref = r"file:C:\ProgramData\WebGPTCodingToolsMCPService\tunnel\runtime-api-key.txt"
    control_plane = openai_tunnel_config.get("control_plane", {})
    if control_plane.get("api_key") != expected_secret_ref:
        raise RuntimeError("machine-level tunnel config must use the ProgramData file: runtime API key reference")
    if control_plane.get("tunnel_id") != "tunnel_6a87d59143248191aa263b98ceb8d9d8":
        raise RuntimeError("machine-level tunnel config stopped reusing the established Secure Tunnel id")
    server_urls = openai_tunnel_config.get("mcp", {}).get("server_urls", [])
    if not server_urls or server_urls[0].get("url") != "http://127.0.0.1:8767/mcp":
        raise RuntimeError("machine-level tunnel config stopped targeting the dedicated 8767 MCP listener")
    health = openai_tunnel_config.get("health", {})
    if health.get("listen_addr") != "127.0.0.1:8769":
        raise RuntimeError("OpenAITunnelClient readiness endpoint must stay fixed on loopback port 8769")
    if "runtime-api-key.txt" not in gitignore_text:
        raise RuntimeError("runtime API key filename must remain ignored by Git")
    if (service_root / "runtime-api-key.txt").exists() or (service_root / "tunnel" / "runtime-api-key.txt").exists():
        raise RuntimeError("runtime API key material must never exist in the source tree")
    for user_profile_dependency in (r"C:\Users\ken\.codex-chatgpt-web", r"C:\Users\ken\.coding-tools-tunnel"):
        if user_profile_dependency in openai_tunnel_config_text or user_profile_dependency in openai_tunnel_service_xml_text:
            raise RuntimeError("installed OpenAITunnelClient config must not depend on Ken's user profile")

    if '<onfailure action="restart"' not in mcp_service_xml_text or "<resetfailure>" not in mcp_service_xml_text:
        raise RuntimeError("MCP Windows service lost automatic crash-restart policy")

    if "sc.exe failure WebGPTCodingToolsMCP reset= 0" in install_text:
        raise RuntimeError("fresh install must not clear the MCP recovery restart policy")

    for watchdog_contract in (
        "_probe_loopback_mcp",
        "_start_tunnel_watchdog",
        "TUNNEL_WATCHDOG_INTERVAL_SECONDS",
        "TUNNEL_WATCHDOG_FAILURES",
        "tunnel_watchdog_failed.is_set()",
    ):
        if watchdog_contract not in bootstrap_text:
            raise RuntimeError(f"Secure MCP Tunnel watchdog contract missing: {watchdog_contract}")

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
            web_console_bridge_source,
            browser_extension_root / "manifest.json",
            browser_extension_root / "background.js",
            browser_extension_root / "browser-agent.js",
            browser_extension_root / "content.js",
            browser_extension_root / "bridge-frame.html",
            browser_extension_root / "bridge-frame.js",
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
    web_console_bridge_text = web_console_bridge_source.read_text(encoding="utf-8")
    web_console_admin_text = (service_root / "manage-web-console-system.ps1").read_text(encoding="utf-8")
    extension_manifest = json.loads((browser_extension_root / "manifest.json").read_text(encoding="utf-8"))
    extension_background_text = (browser_extension_root / "background.js").read_text(encoding="utf-8")
    extension_agent_text = (browser_extension_root / "browser-agent.js").read_text(encoding="utf-8")
    extension_content_text = (browser_extension_root / "content.js").read_text(encoding="utf-8")
    extension_bridge_text = (browser_extension_root / "bridge-frame.js").read_text(encoding="utf-8")
    desktop_tool_text = (package_parent / "coding_tools_mcp" / "tools" / "desktop.py").read_text(encoding="utf-8")
    interactive_exec_text = (package_parent / "coding_tools_mcp" / "interactive_exec.py").read_text(encoding="utf-8")
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
    if "EscapeSendKeysText" not in helper_text or 'SendKeys.SendWait("^a")' not in helper_text:
        raise RuntimeError("type_text lost its keyboard fallback for writable controls without ValuePattern")
    if "computer-use-overlay-leases" not in helper_text:
        raise RuntimeError("Computer Use overlay must use per-operation leases")
    if "Try-HandleHumanHelpInWebConsole" not in interactive_broker_text:
        raise RuntimeError("HUMAN HELP stopped preferring the in-page Web Console")
    if '$delivery -ne "desktop_only"' not in interactive_broker_text:
        raise RuntimeError("HUMAN HELP desktop-only delivery no longer bypasses the Web Console")
    if '"delivery": str(delivery)' not in interactive_exec_text or "delivery=delivery" not in desktop_tool_text:
        raise RuntimeError("HUMAN HELP no longer forwards its delivery policy to the interactive broker")
    if "GetLastWriteTimeUtc($webConsoleHeartbeat)" not in interactive_broker_text:
        raise RuntimeError("Web Console liveness must use heartbeat metadata instead of contended content reads")
    for web_help_delivery_contract in ("HUMAN_HELP_WEB_SEEN", "HUMAN_HELP_WEB_NOT_SEEN", ".web-human-help.seen", ".web-human-help.activity", "HUMAN_HELP_WEB_ACTIVITY"):
        if web_help_delivery_contract not in interactive_broker_text:
            raise RuntimeError(f"HUMAN HELP web delivery handshake lost broker contract: {web_help_delivery_contract}")
    for caller_activity_contract in (
        'activity_path = queue / f"{request_id}.web-human-help.activity"',
        "activity_path.stat().st_mtime_ns",
        "deadline = time.monotonic() + timeout + 10.0",
        "if activity_mtime_ns > last_activity_mtime_ns:",
    ):
        if caller_activity_contract not in interactive_exec_text:
            raise RuntimeError(f"HUMAN HELP caller no longer extends its wait on Web input activity: {caller_activity_contract}")
    if ".web-human-help.seen" not in web_console_bridge_text:
        raise RuntimeError("Web Console bridge stopped acknowledging delivered HUMAN HELP prompts")
    if 'request.Path == "/v1/human-help/seen"' not in web_console_bridge_text:
        raise RuntimeError("Web Console bridge must require an explicit HUMAN HELP delivery acknowledgement")
    if 'request.Path == "/v1/human-help/activity"' not in web_console_bridge_text:
        raise RuntimeError("Web Console bridge must accept HUMAN HELP input activity heartbeats")
    if "ToUnixTimeMilliseconds" not in web_console_bridge_text:
        raise RuntimeError("Web Console HUMAN HELP activity must use monotonic-enough millisecond timestamps")
    if 'document.visibilityState === "visible"' not in extension_content_text:
        raise RuntimeError("background Web Console tabs must not suppress desktop HUMAN HELP fallback")
    if 'request("/v1/human-help/seen"' not in extension_content_text:
        raise RuntimeError("visible Web Console stopped acknowledging rendered HUMAN HELP prompts")
    if 'if (helpId && helpId !== lastPresentedHelpId && document.visibilityState === "visible")' not in extension_content_text:
        raise RuntimeError("visible Web Console must ACK HUMAN HELP independently of DND state")
    if 'if (!state.dnd) {' in extension_content_text:
        raise RuntimeError("Web Console DND must not suppress HUMAN HELP focus mode or automatic response-panel opening")
    if 'helpId !== lastPresentedHelpId && !state.dnd && document.visibilityState' in extension_content_text:
        raise RuntimeError("Web Console DND regressed to forcing HUMAN HELP desktop fallback")
    if ('activeTab = "help";' not in extension_content_text and 'selectTab("help")' not in extension_content_text) or "setOpen(true);" not in extension_content_text:
        raise RuntimeError("Web Console HUMAN HELP must force the response panel open even while DND is enabled")
    for input_activity_contract in (
        "HELP_ACTIVITY_THROTTLE_MS",
        "function noteHumanHelpActivity(requestId)",
        'request("/v1/human-help/activity"',
        '"keydown", "input", "paste", "compositionupdate"',
        "noteEscapeComposerActivity",
    ):
        if input_activity_contract not in extension_content_text:
            raise RuntimeError(f"Web Console HUMAN HELP input activity reset contract regressed: {input_activity_contract}")
    for human_help_reason_label in (
        'if (text === "permission_blocked") return "需要系統權限";',
        'if (text === "gui_required") return "需要你操作畫面";',
        'if (text === "faster_by_human") return "這一步你做比較快";',
        'if (text === "need_information") return "需要你提供資訊";',
        'if (text === "need_decision") return "需要你決定";',
    ):
        if human_help_reason_label not in extension_content_text:
            raise RuntimeError(f"Web Console HUMAN HELP reason label regressed: {human_help_reason_label}")
    web_help_attempt_index = interactive_broker_text.find("Try-HandleHumanHelpInWebConsole $RequestId")
    desktop_help_index = interactive_broker_text.find("Add-Type -AssemblyName System.Windows.Forms")
    if web_help_attempt_index < 0 or desktop_help_index < 0 or web_help_attempt_index > desktop_help_index:
        raise RuntimeError("HUMAN HELP must try the Web Console before creating desktop UI")
    if "activity-log-viewer.desktop" not in interactive_broker_text:
        raise RuntimeError("legacy desktop activity viewer must remain explicit opt-in")
    for bridge_contract in (
        "IPAddress.Loopback",
        "X-Coding-Tools-Console",
        "chrome-extension://",
        "Access-Control-Allow-Private-Network: true",
        ".web-human-help.response",
    ):
        if bridge_contract not in web_console_bridge_text:
            raise RuntimeError(f"Web Console bridge lost security/IPC contract: {bridge_contract}")
    if extension_manifest.get("manifest_version") != 3:
        raise RuntimeError("Web Console browser extension must use Manifest V3")
    if extension_manifest.get("host_permissions") != ["http://127.0.0.1:8768/*"]:
        raise RuntimeError("Web Console extension host permissions became broader than loopback bridge access")
    extension_permissions = set(extension_manifest.get("permissions") or [])
    for permission in ("storage", "tabs", "tabGroups", "debugger"):
        if permission not in extension_permissions:
            raise RuntimeError(f"Browser Use extension permission is missing: {permission}")
    if "scripting" in extension_permissions:
        raise RuntimeError("Browser Use must use the Chrome debugger channel instead of all-site script injection")
    if "http://127.0.0.1:8768" not in extension_background_text:
        raise RuntimeError("Web Console extension stopped using the loopback bridge")
    for browser_agent_contract in (
        'importScripts("browser-agent.js")',
        "startCodingToolsBrowserAgent()",
    ):
        if browser_agent_contract not in extension_background_text:
            raise RuntimeError(f"Browser Use background startup contract is missing: {browser_agent_contract}")
    for browser_agent_contract in (
        'chrome.tabs.create({ active: false, url: "about:blank" })',
        "BROWSER_AGENT_GROUP_TITLE",
        'chrome.tabs.update(tab.id, { url, active: false })',
        'chrome.debugger.attach(target, "1.3")',
        'send("Runtime.evaluate"',
        'send("Input.dispatchMouseEvent"',
        'send("Input.insertText"',
        'send("Page.captureScreenshot"',
        "__coding_tools_browser_cursor__",
    ):
        if browser_agent_contract not in extension_agent_text:
            raise RuntimeError(f"Browser Use extension contract is missing: {browser_agent_contract}")
    for browser_bridge_contract in (
        'request.Path == "/v1/browser/next"',
        'request.Path == "/v1/browser/respond"',
        ".browser-extension.request",
        ".browser-extension.response",
    ):
        if browser_bridge_contract not in web_console_bridge_text:
            raise RuntimeError(f"Browser Use loopback bridge contract is missing: {browser_bridge_contract}")
    for browser_broker_contract in (
        "Handle-BrowserExtensionRequest $RequestId $Request",
        ".browser-extension.request",
        "BROWSER_EXTENSION_UNAVAILABLE",
    ):
        if browser_broker_contract not in interactive_broker_text:
            raise RuntimeError(f"Browser Use broker routing contract is missing: {browser_broker_contract}")
    for timeout_contract in ("CONSOLE_REQUEST_TIMEOUT_MS", "AbortController", "console_request_timeout"):
        if timeout_contract not in extension_background_text:
            raise RuntimeError(f"Web Console background bridge lost timeout contract: {timeout_contract}")
    for timeout_contract in ("REQUEST_TIMEOUT_MS", "主控台請求逾時"):
        if timeout_contract not in extension_content_text:
            raise RuntimeError(f"Web Console content bridge lost timeout contract: {timeout_contract}")
    for ui_contract in ("attachShadow", "CODING MCP 主控台", "HUMAN_HELP", 'data-tab="settings"', "coding-tools-console-request"):
        if ui_contract not in extension_content_text:
            raise RuntimeError(f"Web Console drawer lost UI contract: {ui_contract}")
    for settings_contract in (
        'request("/v1/system/action"',
        '"restart_all"',
        '"restart_tunnel"',
        '"update"',
        '"rollback"',
        '"yolo"',
        "function renderSettings()",
        ':host([data-theme="dark"])',
        "function syncPageTheme()",
        "background:var(--glass-bg)",
    ):
        if settings_contract not in extension_content_text:
            raise RuntimeError(f"Web Console settings surface lost contract: {settings_contract}")
    for bridge_settings_contract in (
        'request.Path == "/v1/system/action"',
        "LaunchAdminAction(action)",
        '"start_all"',
        '"restart_all"',
        '"restart_tunnel"',
        '"safe"',
        '"trusted"',
        '"yolo"',
        '"services", ReadServiceStates()',
    ):
        if bridge_settings_contract not in web_console_bridge_text:
            raise RuntimeError(f"Web Console bridge lost settings contract: {bridge_settings_contract}")
    for web_console_admin_contract in (
        'Join-Path $PSHOME "powershell.exe"',
        '$ErrorActionPreference = "Continue"',
        '$childExitCode = $LASTEXITCODE',
        'Maintenance script exited with code',
    ):
        if web_console_admin_contract not in web_console_admin_text:
            raise RuntimeError(
                f"Web Console admin helper may regress to treating child stderr warnings as fatal: {web_console_admin_contract}"
            )
    for local_network_contract in ('targetAddressSpace: "loopback"', "coding-tools-bridge-frame", "127.0.0.1:8768"):
        if local_network_contract not in extension_bridge_text:
            raise RuntimeError(f"Web Console Local Network Access bootstrap lost contract: {local_network_contract}")
    if 'targetAddressSpace: "loopback"' not in extension_background_text:
        raise RuntimeError("Web Console background bridge must classify 127.0.0.1 as loopback")
    if '"X-Coding-Tools-Extension": "1"' not in extension_background_text:
        raise RuntimeError("Web Console extension transport lost its originless service-worker marker")
    for extension_originless_contract in (
        'Header(request, "X-Coding-Tools-Extension")',
        "originlessExtensionClient",
        "String.IsNullOrWhiteSpace(origin) && extensionClient",
    ):
        if extension_originless_contract not in web_console_bridge_text:
            raise RuntimeError(f"Web Console bridge lost originless extension authentication contract: {extension_originless_contract}")
    if "(!IsAllowedOrigin(origin) && !originlessExtensionClient) || !consoleClient" not in web_console_bridge_text:
        raise RuntimeError("Web Console bridge must preserve Origin checks except for marked extension service-worker requests")
    if "X-Coding-Tools-Console, X-Coding-Tools-Extension" not in web_console_bridge_text:
        raise RuntimeError("Web Console CORS allowlist lost the extension transport marker header")
    if "if (false)" in extension_content_text:
        raise RuntimeError("Web Console Local Network Access bootstrap must not be compile-time disabled")
    if "function extensionRequest" not in extension_content_text or 'chrome.runtime.sendMessage(' not in extension_content_text:
        raise RuntimeError("Web Console content script lost extension-background request transport")
    if "return await extensionRequest(path, options);" not in extension_content_text:
        raise RuntimeError("Web Console requests must prefer extension-background transport before page-context loopback")
    for focus_mode_contract in (
        ".focusMask",
        "backdrop-filter:blur(9px)",
        "function findEscapeComposer()",
        "function updateFocusMask()",
        "const enabled = Boolean(state && state.human_help)",
        'selectTab("help")',
        "setOpen(true);",
    ):
        if focus_mode_contract not in extension_content_text:
            raise RuntimeError(f"Web Console HUMAN HELP focus mode lost contract: {focus_mode_contract}")
    if 'helpId !== lastPresentedHelpId && document.visibilityState === "visible"' not in extension_content_text:
        raise RuntimeError("Web Console HUMAN HELP visible-page presentation contract disappeared")
    if "return await directRequest(path, options);" not in extension_content_text:
        raise RuntimeError("Web Console requests must retain direct loopback as a compatibility fallback")
    for content_bridge_contract in ('http://127.0.0.1:8768', 'targetAddressSpace: "loopback"', 'X-Coding-Tools-Console'):
        if content_bridge_contract not in extension_content_text:
            raise RuntimeError(f"Web Console content script lost direct loopback fallback contract: {content_bridge_contract}")
    for allowed_web_origin in ("https://chatgpt.com", "https://chat.openai.com"):
        if allowed_web_origin not in web_console_bridge_text:
            raise RuntimeError(f"Web Console bridge lost allowed web origin: {allowed_web_origin}")
    if '"web_qa"' not in desktop_tool_text or 'execution_context' not in desktop_tool_text:
        raise RuntimeError("HUMAN HELP public delivery must report Web Console responses as web_qa")

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
        if include_desktop_surfaces:
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

        web_console_output = Path(helper_temp) / "web-console-bridge.exe"
        web_console_compile = subprocess.run(
            [
                str(csc),
                "/nologo",
                "/target:winexe",
                "/optimize+",
                f"/out:{web_console_output}",
                f"/reference:{windows_root / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'System.Web.Extensions.dll'}",
                "/reference:System.ServiceProcess.dll",
                str(web_console_bridge_source),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if web_console_compile.returncode != 0 or not web_console_output.is_file():
            raise RuntimeError(
                "Web Console bridge failed to compile:\n" + web_console_compile.stdout[-8000:]
            )
