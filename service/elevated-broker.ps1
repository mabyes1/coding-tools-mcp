[CmdletBinding()]
param(
    [int]$PollMilliseconds = 250
)

$ErrorActionPreference = "Stop"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$queueRoot = Join-Path $serviceRoot "elevated-requests"
$pidPath = Join-Path $queueRoot "broker.pid"
$stopPath = Join-Path $queueRoot "broker.stop"
$logPath = Join-Path $queueRoot "broker.log"
$protocolVersion = 1
$requestTtlSeconds = 900

function Write-BrokerLog([string]$Message) {
    try {
        $timestamp = [DateTimeOffset]::Now.ToString("o")
        [IO.File]::AppendAllText($logPath, "$timestamp $Message`r`n", [Text.UTF8Encoding]::new($false))
    }
    catch { }
}

function Get-Sha256Hex([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "")
        }
        finally {
            $sha256.Dispose()
        }
    }
    finally {
        $stream.Dispose()
    }
}

# This is deliberately a fixed action map.  Do not turn this into a generic
# "run whatever PowerShell asks for" broker.
$allowedAction = [ordered]@{
    "install-vibedeck-update" = @{
        ScriptPath = "D:\coding-tools-mcp\phoneMonitor\scripts\build-and-install-windows.ps1"
        ExpectedSha256 = "A265E6C37C9E082033D2B499B4E3898CD61386403A7BA3DFDEA91FD32BA9010C"
        Description = "Build and install the validated VibeDeck update through the canonical Windows Setup path"
        Wait = $true
        Arguments = @("-SkipTests")
    }
    "repair-vibedeck-virtual-display" = @{
        ScriptPath = "D:\coding-tools-mcp\phoneMonitor\scripts\repair-virtual-display-device.ps1"
        ExpectedSha256 = "3EDF1E2A097B5E628FD33968B76AB68E11A21B1A0D55CD3DCB0ABFDE3BF2F7D6"
        Description = "Restart only the VibeDeck virtual display PnP device and rescan Windows display targets"
        Wait = $true
        Arguments = @()
    }
    "repair-vibedeck-autostart" = @{
        ExecutablePath = "C:\Program Files\VibeDeck\VibeDeck.Host.exe"
        Description = "Repair the signed-in user's VibeDeck Host autostart registration"
        Wait = $true
        Arguments = @("--register-autostart")
    }
    "sync-installed-webroot" = @{
        ScriptPath = "D:\coding-tools-mcp\phoneMonitor\scripts\sync-installed-webroot.ps1"
        ExpectedSha256 = "REPLACE_WITH_SYNC_WEBROOT_SHA256"
        Destination = "C:\Program Files\VibeDeck\wwwroot"
        Description = "Sync the approved VibeDeck web root from the private workspace"
        Wait = $true
        Arguments = @()
    }
    "update-private-mcp" = @{
        ScriptPath = "D:\coding-tools-mcp\coding-tools-mcp\service\update-private-mcp.ps1"
        ExpectedSha256 = "REPLACE_WITH_UPDATE_PRIVATE_MCP_SHA256"
        Description = "Deploy the validated private coding-tools MCP source and restart its services"
        Wait = $false
        Arguments = @("-StartDelaySeconds", "3")
    }
}

function Write-AtomicJson([string]$Path, [hashtable]$Payload) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, ($Payload | ConvertTo-Json -Compress -Depth 8), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Complete-Request([string]$RequestId, [hashtable]$Response) {
    $Response.protocol = $protocolVersion
    $Response.request_id = $RequestId
    Write-AtomicJson (Join-Path $queueRoot "$RequestId.response") $Response
}

function Handle-PermissionRequest([string]$RequestId, $Request) {
    $allowedTools = @("exec_command", "apply_patch")
    $allowedPermissions = @(
        "network", "destructive_command", "long_timeout", "sensitive_env",
        "shell_expansion", "inline_script", "privileged_executable",
        "filesystem_escape", "write_generated_or_ignored"
    )
    $toolName = [string]$Request.tool_name
    $permission = [string]$Request.permission
    $scope = [string]$Request.scope
    $ttlSeconds = [int]$Request.ttl_seconds
    if ($toolName -notin $allowedTools -or $permission -notin $allowedPermissions -or $scope -notin @("once", "session") -or $ttlSeconds -lt 1 -or $ttlSeconds -gt 3600) {
        Complete-Request $RequestId @{ ok = $false; error = "PERMISSION_REQUEST_INVALID"; message = "Invalid MCP permission request."; retryable = $false }
        return
    }

    $argumentPreview = try { $Request.arguments | ConvertTo-Json -Compress -Depth 6 } catch { "<unavailable>" }
    if ($argumentPreview.Length -gt 1200) { $argumentPreview = $argumentPreview.Substring(0, 1200) + "..." }
    # Keep this broker source ASCII-only. Windows PowerShell 5.1 treats UTF-8
    # without a BOM as ANSI when older installers read and rewrite the file.
    $scopeText = if ($scope -eq "once") { "once (same arguments only)" } else { "session (up to $ttlSeconds seconds)" }
    $message = @"
WebGPT MCP is requesting additional permission.

Tool: $toolName
Permission: $permission
Scope: $scopeText
Reason: $([string]$Request.reason)

Arguments:
$argumentPreview

Allow this request? Choose No to deny it. A new request can be made later.
"@
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $choice = [System.Windows.Forms.MessageBox]::Show(
            $message,
            "WebGPT MCP Permission Request",
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning,
            [System.Windows.Forms.MessageBoxDefaultButton]::Button2
        )
        $granted = $choice -eq [System.Windows.Forms.DialogResult]::Yes
        Complete-Request $RequestId @{
            ok = $true
            granted = $granted
            permission = $permission
            tool_name = $toolName
            scope = $scope
            ttl_seconds = $ttlSeconds
            message = if ($granted) { "Permission approved by the signed-in user." } else { "Permission denied by the signed-in user." }
            retryable = -not $granted
        }
    }
    catch {
        Complete-Request $RequestId @{ ok = $false; error = "PERMISSION_DIALOG_FAILED"; message = $_.Exception.Message; retryable = $true }
    }
}

function Handle-Request([string]$ProcessingPath) {
    $requestId = [IO.Path]::GetFileNameWithoutExtension($ProcessingPath)
    try {
        $request = [IO.File]::ReadAllText($ProcessingPath, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
        if ($request.protocol -ne $protocolVersion -or $request.request_id -ne $requestId) {
            Complete-Request $requestId @{ ok = $false; error = "ELEVATION_REQUEST_INVALID"; message = "Invalid broker request envelope." }
            return
        }
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$request.created_at
        if ($age -lt -30 -or $age -gt $requestTtlSeconds) {
            Complete-Request $requestId @{ ok = $false; error = "ELEVATION_REQUEST_EXPIRED"; message = "The elevation request expired before user approval."; retryable = $true }
            return
        }
        if ([string]$request.kind -eq "mcp_permission") {
            Handle-PermissionRequest $requestId $request
            return
        }
        $actionName = [string]$request.action
        if (-not $allowedAction.Contains($actionName)) {
            Complete-Request $requestId @{ ok = $false; error = "ELEVATED_ACTION_NOT_ALLOWED"; message = "This action is not registered in the broker." }
            return
        }
        $action = $allowedAction[$actionName]
        if ($action.ExecutablePath) {
            if (-not (Test-Path -LiteralPath $action.ExecutablePath -PathType Leaf)) {
                Complete-Request $requestId @{ ok = $false; error = "ELEVATED_EXECUTABLE_NOT_FOUND"; message = "The approved action executable is missing."; retryable = $false }
                return
            }
            $process = Start-Process -FilePath $action.ExecutablePath -WorkingDirectory (Split-Path -Parent $action.ExecutablePath) -PassThru -ArgumentList @($action.Arguments)
        }
        else {
            if (-not (Test-Path -LiteralPath $action.ScriptPath -PathType Leaf)) {
                Complete-Request $requestId @{ ok = $false; error = "ELEVATED_SCRIPT_NOT_FOUND"; message = "The approved action script is missing."; retryable = $false }
                return
            }
            $actualHash = (Get-Sha256Hex $action.ScriptPath).ToUpperInvariant()
            if ($action.ExpectedSha256 -notmatch '^[A-Fa-f0-9]{64}$' -or $actualHash -ne $action.ExpectedSha256.ToUpperInvariant()) {
                Complete-Request $requestId @{ ok = $false; error = "ELEVATED_SCRIPT_HASH_MISMATCH"; message = "The approved action script hash does not match the broker allowlist."; retryable = $false }
                return
            }
            $shell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
            $argumentList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $action.ScriptPath)
            if ($action.Arguments) { $argumentList += @($action.Arguments) }
            $process = Start-Process -FilePath $shell -PassThru -ArgumentList $argumentList
        }
        if ($action.Wait) { $process.WaitForExit() }
        $ok = if ($action.Wait) { $process.ExitCode -eq 0 } else { $true }
        $verifiedMessage = $null
        if ($ok -and $actionName -eq "repair-vibedeck-autostart") {
            $expectedAutostart = '"C:\Program Files\VibeDeck\VibeDeck.Host.exe"'
            $actualAutostart = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "VibeDeckHost" -ErrorAction SilentlyContinue).VibeDeckHost
            if ($actualAutostart -ne $expectedAutostart) {
                $ok = $false
                $verifiedMessage = "VibeDeck Host exited successfully, but the signed-in user's autostart registry value was not registered as expected."
            }
            else {
                $verifiedMessage = "VibeDeck autostart repaired and verified for the signed-in user."
            }
        }
        Complete-Request $requestId @{
            ok = $ok
            exit_code = if ($action.Wait) { $process.ExitCode } else { 0 }
            message = if ($verifiedMessage) { $verifiedMessage } elseif (-not $action.Wait) { "Elevated action accepted and launched." } elseif ($ok) { "Elevated action completed." } else { "Elevated action exited with code $($process.ExitCode)." }
            error = if ($ok) { $null } else { "ELEVATED_ACTION_FAILED" }
            retryable = $false
        }
    }
    catch {
        Complete-Request $requestId @{ ok = $false; error = "ELEVATION_BROKER_ERROR"; message = $_.Exception.Message; retryable = $true }
    }
    finally {
        Remove-Item -LiteralPath $ProcessingPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $queueRoot -PathType Container)) {
    throw "Elevated broker queue is missing: $queueRoot"
}
$brokerIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$brokerPrincipal = [Security.Principal.WindowsPrincipal]::new($brokerIdentity)
$brokerSessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
if ($brokerIdentity.User.Value -eq "S-1-5-19" -or $brokerSessionId -le 0) {
    throw "The elevated broker must run in an interactive signed-in user session, not LocalService or Session 0."
}
if (-not $brokerPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "The elevated broker must run elevated. Repair the WebGPT-Elevated-Broker scheduled task with RunLevel Highest."
}

Set-Content -LiteralPath $pidPath -Value ([Diagnostics.Process]::GetCurrentProcess().Id) -Encoding ASCII
Write-BrokerLog "START pid=$([Diagnostics.Process]::GetCurrentProcess().Id) session=$([Diagnostics.Process]::GetCurrentProcess().SessionId) user=$([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
try {
    while ($true) {
        if (Test-Path -LiteralPath $stopPath) { break }
        foreach ($requestPath in @(Get-ChildItem -LiteralPath $queueRoot -Filter "*.request" -File -ErrorAction SilentlyContinue | Sort-Object CreationTime)) {
            $processingPath = Join-Path $queueRoot "$([IO.Path]::GetFileNameWithoutExtension($requestPath.Name)).processing"
            try {
                Move-Item -LiteralPath $requestPath.FullName -Destination $processingPath -Force -ErrorAction Stop
            }
            catch {
                continue
            }
            Handle-Request $processingPath
        }
        Start-Sleep -Milliseconds ([Math]::Max(100, [Math]::Min($PollMilliseconds, 2000)))
    }
}
catch {
    Write-BrokerLog "FATAL $($_.Exception.GetType().FullName): $($_.Exception.Message)"
    throw
}
finally {
    Write-BrokerLog "STOP pid=$([Diagnostics.Process]::GetCurrentProcess().Id)"
    Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}
