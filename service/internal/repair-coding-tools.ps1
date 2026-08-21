$ErrorActionPreference = "Stop"

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

Start-Service -Name WebGPTCodingToolsMCP
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $listener = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
} until ($listener -or (Get-Date) -ge $deadline)
if (-not $listener) {
    throw "MCP service did not open port 8765. Inspect C:\ProgramData\WebGPTCodingToolsMCPService\logs."
}

Start-Service -Name WebGPTCloudflareTunnel
Get-Service -Name WebGPTCodingToolsMCP,WebGPTCloudflareTunnel |
    Select-Object Name,Status,StartType |
    Format-Table -AutoSize
Write-Host "SERVICE_REPAIR_OK"
