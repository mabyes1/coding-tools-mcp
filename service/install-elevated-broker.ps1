$ErrorActionPreference = "Stop"

function Get-Sha256Hex([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "") }
        finally { $sha256.Dispose() }
    }
    finally { $stream.Dispose() }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) { throw "Run this broker installation as Administrator." }

$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$queueRoot = Join-Path $serviceRoot "elevated-requests"
$sourceRoot = $PSScriptRoot
$workspaceRoot = "D:\coding-tools-mcp"
$localServiceSid = "*S-1-5-19"
$currentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $serviceRoot -PathType Container)) { throw "MCP service root is missing: $serviceRoot" }
New-Item -ItemType Directory -Path $queueRoot -Force | Out-Null
foreach ($file in @("elevated-broker.ps1", "request-elevated-action.ps1", "manage-elevated-broker.ps1")) {
    $source = Join-Path $sourceRoot $file
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Broker source is missing: $source" }
    Copy-Item -LiteralPath $source -Destination (Join-Path $serviceRoot $file) -Force
}
$launcherSource = Join-Path $sourceRoot "ElevatedBrokerLauncher.cs"
if (-not (Test-Path -LiteralPath $launcherSource -PathType Leaf)) { throw "Broker launcher source is missing: $launcherSource" }
$launcherExe = Join-Path $serviceRoot "elevated-broker-launcher.exe"
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
$windowsPowerShellForBuild = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$automationRef = (& $windowsPowerShellForBuild -NoLogo -NoProfile -NonInteractive -Command "[System.Management.Automation.PowerShell].Assembly.Location").Trim()

# Reinstall/repair may run while the existing scheduled launcher is alive.
# Shut it down first so csc can atomically replace the executable.
$installedBrokerManager = Join-Path $serviceRoot "manage-elevated-broker.ps1"
if (Test-Path -LiteralPath $installedBrokerManager -PathType Leaf) {
    & $windowsPowerShellForBuild -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $installedBrokerManager -Action Stop 2>$null | Out-Null
}
Stop-ScheduledTask -TaskName "WebGPT-Elevated-Broker" -ErrorAction SilentlyContinue
Get-Process -Name "elevated-broker-launcher" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
$unlockDeadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
while ((Get-Process -Name "elevated-broker-launcher" -ErrorAction SilentlyContinue) -and
       [DateTimeOffset]::UtcNow -lt $unlockDeadline) {
    Start-Sleep -Milliseconds 100
}
if (Get-Process -Name "elevated-broker-launcher" -ErrorAction SilentlyContinue) {
    throw "Could not stop the existing elevated broker launcher before install."
}

& $csc /nologo /target:winexe /optimize+ /out:$launcherExe `
    /reference:$automationRef `
    $launcherSource
if ($LASTEXITCODE -ne 0) { throw "Could not build the windowless elevated broker launcher." }
$webrootSource = Join-Path $workspaceRoot "phoneMonitor\scripts\sync-installed-webroot.ps1"
$brokerPath = Join-Path $serviceRoot "elevated-broker.ps1"
if (-not (Test-Path -LiteralPath $webrootSource -PathType Leaf)) { throw "Approved webroot script is missing: $webrootSource" }
$hash = Get-Sha256Hex $webrootSource
$brokerText = [IO.File]::ReadAllText($brokerPath, [Text.UTF8Encoding]::new($false))
$brokerText = [regex]::Replace($brokerText, '(ExpectedSha256\s*=\s*")[A-Fa-f0-9]{64}("?)', { param($match) $match.Groups[1].Value + $hash + $match.Groups[2].Value })
$brokerText = $brokerText.Replace("REPLACE_WITH_SYNC_WEBROOT_SHA256", $hash)
[IO.File]::WriteAllText($brokerPath, $brokerText, [Text.UTF8Encoding]::new($false))

& icacls.exe $queueRoot /inheritance:r /grant:r `
    "*S-1-5-18:(OI)(CI)F" `
    "*S-1-5-32-544:(OI)(CI)F" `
    "${currentAccount}:(OI)(CI)M" `
    "${localServiceSid}:(OI)(CI)M" /C | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Could not secure the elevated broker queue." }
& icacls.exe (Join-Path $serviceRoot "elevated-broker.ps1") /grant "${currentAccount}:RX" "${localServiceSid}:RX" /C | Out-Host
& icacls.exe (Join-Path $serviceRoot "request-elevated-action.ps1") /grant "${currentAccount}:RX" "${localServiceSid}:RX" /C | Out-Host

$brokerManager = Join-Path $serviceRoot "manage-elevated-broker.ps1"
$windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $brokerManager -Action Install
if ($LASTEXITCODE -ne 0) { throw "Could not install the interactive elevated broker task." }
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $brokerManager -Action Start
if ($LASTEXITCODE -ne 0) { throw "Could not start the interactive elevated broker." }

Write-Host "ELEVATED_BROKER_FILES_INSTALLED"
Write-Host "Action=sync-installed-webroot SHA256=$hash"
