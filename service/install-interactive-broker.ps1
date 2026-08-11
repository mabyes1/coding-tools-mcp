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

$manager = Join-Path $serviceRoot "manage-interactive-broker.ps1"
$windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Install
if ($LASTEXITCODE -ne 0) { throw "Could not install the non-elevated interactive broker task." }
& $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Start
if ($LASTEXITCODE -ne 0) { throw "Could not start the non-elevated interactive broker." }

Write-Host "INTERACTIVE_BROKER_INSTALL_OK user=$currentAccount session=$currentSession"
