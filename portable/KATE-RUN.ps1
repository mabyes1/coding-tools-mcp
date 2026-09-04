[CmdletBinding()]
param(
    [ValidateSet("safe", "trusted", "dangerous")]
    [string]$PermissionMode = "trusted"
)

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $PSScriptRoot ".runtime"
$venvPython = Join-Path $runtimeRoot "venv\Scripts\python.exe"
$cloudflared = Join-Path $runtimeRoot "cloudflared.exe"
$workspaceFile = Join-Path $runtimeRoot "workspace.txt"
$oauthPasswordFile = Join-Path $runtimeRoot "oauth-password.txt"
$oauthSecretFile = Join-Path $runtimeRoot "oauth-token-secret.txt"
$oauthStateFile = Join-Path $runtimeRoot "oauth-state.sqlite"
$serverLog = Join-Path $runtimeRoot "server.log"
$serverErr = Join-Path $runtimeRoot "server.err.log"
$tunnelLog = Join-Path $runtimeRoot "cloudflared.log"
$tunnelErr = Join-Path $runtimeRoot "cloudflared.err.log"

foreach ($required in @($venvPython, $cloudflared, $workspaceFile, $oauthPasswordFile, $oauthSecretFile)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Portable runtime is not installed. Run KATE-INSTALL.ps1 first. Missing: $required"
    }
}

$workspace = ([IO.File]::ReadAllText($workspaceFile)).Trim()
$oauthPassword = ([IO.File]::ReadAllText($oauthPasswordFile)).Trim()
$oauthSecret = ([IO.File]::ReadAllText($oauthSecretFile)).Trim()
if (-not (Test-Path -LiteralPath $workspace -PathType Container)) {
    throw "Configured workspace no longer exists: $workspace"
}

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*coding_tools_mcp*--port 8765*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*cloudflared*127.0.0.1:8765*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Remove-Item -LiteralPath $serverLog,$serverErr,$tunnelLog,$tunnelErr -Force -ErrorAction SilentlyContinue

$envBlock = @{
    PYTHONPATH = (Join-Path $bundleRoot "private")
    CODING_TOOLS_MCP_OAUTH_PASSWORD = $oauthPassword
    CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET = $oauthSecret
    CODING_TOOLS_MCP_OAUTH_STATE_PATH = $oauthStateFile
    CODING_TOOLS_MCP_OAUTH_ALLOW_DYNAMIC_REGISTRATION = "1"
    CODING_TOOLS_MCP_TELEMETRY = "off"
    CODING_TOOLS_MCP_PERMISSION_MODE = $PermissionMode
    CODING_TOOLS_MCP_WORKSPACE_ALLOWLIST = "kate=$workspace"
    CODING_TOOLS_MCP_MAX_HTTP_SESSIONS = "128"
    CODING_TOOLS_MCP_HTTP_SESSION_TTL_SECONDS = "300"
}

$oldEnv = @{}
foreach ($pair in $envBlock.GetEnumerator()) {
    $oldEnv[$pair.Key] = [Environment]::GetEnvironmentVariable($pair.Key, "Process")
    [Environment]::SetEnvironmentVariable($pair.Key, $pair.Value, "Process")
}
try {
    $server = Start-Process `
        -FilePath $venvPython `
        -ArgumentList @(
            "-m", "coding_tools_mcp",
            ('--workspace="{0}"' -f $workspace),
            "--host", "127.0.0.1",
            "--port", "8765",
            "--oauth-mode",
            "--permission-mode", $PermissionMode
        ) `
        -RedirectStandardOutput $serverLog `
        -RedirectStandardError $serverErr `
        -PassThru
}
finally {
    foreach ($pair in $oldEnv.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($pair.Key, $pair.Value, "Process")
    }
}

$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 300
    if ($server.HasExited) {
        $detail = if (Test-Path $serverErr) { Get-Content $serverErr -Raw } else { "server exited" }
        throw "Coding MCP failed to start: $detail"
    }
    $tcp = New-Object Net.Sockets.TcpClient
    try {
        $tcp.Connect("127.0.0.1", 8765)
        if ($tcp.Connected) { break }
    }
    catch { }
    finally { $tcp.Dispose() }
} while ((Get-Date) -lt $deadline)

if ((Get-Date) -ge $deadline) {
    throw "Coding MCP did not become ready on localhost:8765. See $serverErr"
}

$tunnel = Start-Process `
    -FilePath $cloudflared `
    -ArgumentList @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:8765") `
    -RedirectStandardOutput $tunnelLog `
    -RedirectStandardError $tunnelErr `
    -PassThru

$publicUrl = $null
$deadline = (Get-Date).AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    foreach ($log in @($tunnelLog, $tunnelErr)) {
        if (Test-Path -LiteralPath $log -PathType Leaf) {
            $text = Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue
            $match = [regex]::Match($text, 'https://[a-z0-9-]+\.trycloudflare\.com')
            if ($match.Success) {
                $publicUrl = $match.Value
                break
            }
        }
    }
    if ($publicUrl) { break }
    if ($tunnel.HasExited) {
        $detail = if (Test-Path $tunnelErr) { Get-Content $tunnelErr -Raw } else { "cloudflared exited" }
        throw "Cloudflare tunnel failed to start: $detail"
    }
} while ((Get-Date) -lt $deadline)

if (-not $publicUrl) {
    throw "Cloudflare tunnel URL was not detected. See $tunnelErr"
}

Write-Host ""
Write-Host "KATE CODING MCP IS LIVE" -ForegroundColor Green
Write-Host "Connector URL : $publicUrl/mcp" -ForegroundColor Cyan
Write-Host "OAuth password: $oauthPassword" -ForegroundColor Yellow
Write-Host "Workspace     : $workspace"
Write-Host "Permission    : $PermissionMode"
Write-Host ""
Write-Host "Keep this PowerShell window open while Kate is coding."
Write-Host "The trycloudflare URL is temporary and changes after a restart."
Write-Host "Press Ctrl+C here when you are done."

try {
    Wait-Process -Id $tunnel.Id
}
finally {
    Stop-Process -Id $tunnel.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
}

