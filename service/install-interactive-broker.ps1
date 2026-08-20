[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$queueRoot = Join-Path $serviceRoot "interactive-requests"
$sourceRoot = $PSScriptRoot
$localServiceSid = "*S-1-5-19"
$currentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$currentSession = [Diagnostics.Process]::GetCurrentProcess().SessionId
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdmin -or $currentSession -le 0) {
    throw "Interactive broker installation must run elevated in the signed-in desktop session."
}
if (-not (Test-Path -LiteralPath $serviceRoot -PathType Container)) {
    throw "MCP service root is missing: $serviceRoot"
}

New-Item -ItemType Directory -Path $queueRoot -Force | Out-Null
foreach ($file in @("interactive-broker.ps1", "manage-interactive-broker.ps1")) {
    $source = Join-Path $sourceRoot $file
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Interactive broker source is missing: $source"
    }
    Copy-Item -LiteralPath $source -Destination (Join-Path $serviceRoot $file) -Force
}

$manager = Join-Path $serviceRoot "manage-interactive-broker.ps1"
$windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
if (Test-Path -LiteralPath $manager -PathType Leaf) {
    & $windowsPowerShell -NoLogo -NoProfile -ExecutionPolicy Bypass -File $manager -Action Stop 2>$null | Out-Null
}
Stop-ScheduledTask -TaskName "WebGPT-Interactive-Broker" -ErrorAction SilentlyContinue
Get-Process -Name "interactive-broker-launcher" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue

$launcherSource = Join-Path $sourceRoot "InteractiveBrokerLauncher.cs"
if (-not (Test-Path -LiteralPath $launcherSource -PathType Leaf)) { throw "Interactive broker launcher source is missing: $launcherSource" }
$launcherExe = Join-Path $serviceRoot "interactive-broker-launcher.exe"
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$automationRef = (& $windowsPowerShell -NoLogo -NoProfile -NonInteractive -Command "[System.Management.Automation.PowerShell].Assembly.Location").Trim()
& $csc /nologo /target:winexe /optimize+ /out:$launcherExe `
    /reference:$automationRef `
    $launcherSource
if ($LASTEXITCODE -ne 0) { throw "Could not build the windowless interactive broker launcher." }

$activityLogViewerPidPath = Join-Path $queueRoot "activity-log-viewer.pid"
if (Test-Path -LiteralPath $activityLogViewerPidPath -PathType Leaf) {
    try {
        $viewerPid = [int]([IO.File]::ReadAllText($activityLogViewerPidPath).Trim())
        if ($viewerPid -gt 0) { Stop-Process -Id $viewerPid -Force -ErrorAction SilentlyContinue }
    }
    catch { }
    Remove-Item -LiteralPath $activityLogViewerPidPath -Force -ErrorAction SilentlyContinue
}
$viewerSource = Join-Path $sourceRoot "ActivityLogViewer.cs"
if (-not (Test-Path -LiteralPath $viewerSource -PathType Leaf)) { throw "Activity Log viewer source is missing: $viewerSource" }
$viewerExe = Join-Path $serviceRoot "activity-log-viewer.exe"
& $csc /nologo /target:winexe /optimize+ /out:$viewerExe `
    /reference:System.Drawing.dll /reference:System.Windows.Forms.dll $viewerSource
if ($LASTEXITCODE -ne 0) { throw "Could not build Activity Log viewer." }

& icacls.exe $queueRoot /inheritance:r /grant:r `
    "*S-1-5-18:(OI)(CI)F" `
    "*S-1-5-32-544:(OI)(CI)F" `
    "${currentAccount}:(OI)(CI)M" `
    "${localServiceSid}:(OI)(CI)M" /C | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Could not secure the interactive broker queue." }

foreach ($file in @("interactive-broker.ps1", "manage-interactive-broker.ps1")) {
    $installed = Join-Path $serviceRoot $file
    & icacls.exe $installed /grant:r "${currentAccount}:RX" "${localServiceSid}:RX" /C | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not grant broker file access: $installed" }
}

& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Install
if ($LASTEXITCODE -ne 0) { throw "Could not install the non-elevated interactive broker task." }
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Start
if ($LASTEXITCODE -ne 0) { throw "Could not start the non-elevated interactive broker." }

Write-Host "INTERACTIVE_BROKER_INSTALL_OK user=$currentAccount session=$currentSession"
