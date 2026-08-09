[CmdletBinding()]
param(
    [ValidateSet("Menu", "Status", "Safe", "Trusted", "Yolo")]
    [string]$Action = "Menu",
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$modePath = Join-Path $serviceRoot "permission-mode.txt"
$validModes = @("safe", "trusted", "dangerous")

function Get-ConfiguredMode {
    if (-not (Test-Path -LiteralPath $modePath -PathType Leaf)) { return "safe" }
    $mode = ([IO.File]::ReadAllText($modePath, [Text.UTF8Encoding]::new($false))).Trim().ToLowerInvariant()
    return $(if ($mode -in $validModes) { $mode } else { "invalid:$mode" })
}

function Show-Status {
    $configured = Get-ConfiguredMode
    $health = try { Invoke-RestMethod http://127.0.0.1:8766/healthz -TimeoutSec 3 } catch { $null }
    [pscustomobject]@{
        ConfiguredMode = $configured
        RunningMode = if ($health) { $health.permission_mode } else { "service unavailable" }
        SkipAllPermissions = if ($health) { $health.dangerously_skip_all_permissions } else { $null }
        InteractiveApproval = if ($health) { $health.permission_approval_transport } else { "unknown" }
        ModeFile = $modePath
    } | Format-List
}

function Restart-McpServices {
    Stop-Service WebGPTCloudflareTunnel -Force -ErrorAction SilentlyContinue
    Stop-Service WebGPTCodingToolsMCP -Force -ErrorAction SilentlyContinue
    Start-Service WebGPTCodingToolsMCP
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $health = try { Invoke-RestMethod http://127.0.0.1:8766/healthz -TimeoutSec 2 } catch { $null }
    } while (-not $health -and (Get-Date) -lt $deadline)
    if (-not $health) { throw "MCP health endpoint did not recover after permission-mode restart." }
    Start-Service WebGPTCloudflareTunnel
}

function Set-Mode([string]$Mode) {
    if ($Mode -notin $validModes) { throw "Unsupported permission mode: $Mode" }
    if ($Mode -eq "dangerous") {
        Write-Host "WARNING: YOLO disables command permission gates, network restrictions, secret-env filtering, and the Linux sandbox." -ForegroundColor Red
        $confirmation = Read-Host "Type YOLO to continue"
        if ($confirmation -cne "YOLO") { Write-Host "Cancelled."; return }
    }
    $temporary = "$modePath.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, "$Mode`r`n", [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $modePath -Force
    Write-Host "Configured WebGPT MCP permission mode: $Mode" -ForegroundColor Cyan
    if (-not $NoRestart) { Restart-McpServices }
    Show-Status
}

if ($Action -eq "Menu") {
    :permissionMenu while ($true) {
        Clear-Host
        Write-Host "=========================================="
        Write-Host "  WebGPT MCP Permission Manager"
        Write-Host "=========================================="
        Show-Status
        Write-Host "[1] Safe     - ask before network/scripts/destructive or external paths"
        Write-Host "[2] Trusted  - allow network and inline scripts; keep path/sandbox guards"
        Write-Host "[3] YOLO     - disable all MCP permission gates"
        Write-Host "[0] Back"
        $choice = Read-Host "Choose"
        switch ($choice) {
            "1" { Set-Mode "safe"; [void](Read-Host "Press Enter to continue") }
            "2" { Set-Mode "trusted"; [void](Read-Host "Press Enter to continue") }
            "3" { Set-Mode "dangerous"; [void](Read-Host "Press Enter to continue") }
            "0" { break permissionMenu }
        }
    }
}
elseif ($Action -eq "Status") { Show-Status }
elseif ($Action -eq "Safe") { Set-Mode "safe" }
elseif ($Action -eq "Trusted") { Set-Mode "trusted" }
elseif ($Action -eq "Yolo") { Set-Mode "dangerous" }
