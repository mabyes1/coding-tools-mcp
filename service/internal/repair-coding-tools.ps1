$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "deployment-common.ps1")

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    throw "Run this repair as Administrator."
}

$localServiceSid = "*S-1-5-19"
$pythonRoot = "C:\Users\ken\AppData\Local\Programs\Python\Python313"
& icacls.exe $pythonRoot /grant "${localServiceSid}:(OI)(CI)RX" /C | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Could not grant LocalService read/execute access to the fixed Python runtime."
}

Start-CodingToolsPrivateServices | Out-Null
Get-Service -Name WebGPTCodingToolsMCP,WebGPTCloudflareTunnel |
    Select-Object Name,Status,StartType |
    Format-Table -AutoSize
Write-Host "SERVICE_REPAIR_OK"
