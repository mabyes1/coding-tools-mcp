[CmdletBinding()]
param(
    [ValidateSet("Install", "Uninstall", "Enable", "Disable", "Start", "Stop", "Status")]
    [string]$Action = "Status"
)

$ErrorActionPreference = "Stop"
$taskName = "WebGPT-Elevated-Broker"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$queueRoot = Join-Path $serviceRoot "elevated-requests"
$broker = Join-Path $serviceRoot "elevated-broker.ps1"
$pwsh = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"

function Get-TaskSafe { Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
function Get-BrokerPid {
    $pidPath = Join-Path $queueRoot "broker.pid"
    if (-not (Test-Path -LiteralPath $pidPath)) { return $null }
    try { return [int](Get-Content -LiteralPath $pidPath -Raw) } catch { return $null }
}

switch ($Action) {
    "Install" {
        if (-not (Test-Path -LiteralPath $broker -PathType Leaf)) { throw "Broker script is missing: $broker" }
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $taskAction = New-ScheduledTaskAction -Execute $pwsh -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$broker`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        # PowerShell's ScheduledTask cmdlets call this logon mode
        # ``Interactive`` (the underlying XML uses InteractiveToken).
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Description "Interactive approval broker for fixed WebGPT MCP deployment actions." -Force | Out-Null
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        Write-Host "ELEVATED_BROKER_TASK_INSTALLED"
    }
    "Uninstall" {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        & $PSCommandPath -Action Stop
        Write-Host "ELEVATED_BROKER_TASK_REMOVED"
    }
    "Enable" { Enable-ScheduledTask -TaskName $taskName | Out-Null; Write-Host "ELEVATED_BROKER_TASK_ENABLED" }
    "Disable" { Disable-ScheduledTask -TaskName $taskName | Out-Null; Write-Host "ELEVATED_BROKER_TASK_DISABLED" }
    "Start" {
        $existing = Get-BrokerPid
        if ($existing) {
            try { Get-Process -Id $existing -ErrorAction Stop | Out-Null; Write-Host "ELEVATED_BROKER_ALREADY_RUNNING"; break } catch { }
        }
        Start-Process -FilePath $pwsh -WindowStyle Hidden -ArgumentList @("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $broker) | Out-Null
        Start-Sleep -Milliseconds 500
        Write-Host "ELEVATED_BROKER_STARTED"
    }
    "Stop" {
        $stopPath = Join-Path $queueRoot "broker.stop"
        New-Item -ItemType File -Path $stopPath -Force | Out-Null
        $brokerPid = Get-BrokerPid
        if ($brokerPid) { try { Wait-Process -Id $brokerPid -Timeout 5 -ErrorAction SilentlyContinue } catch { } }
        if ($brokerPid) { try { Stop-Process -Id $brokerPid -Force -ErrorAction SilentlyContinue } catch { } }
        Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
        Write-Host "ELEVATED_BROKER_STOPPED"
    }
    "Status" {
        $task = Get-TaskSafe
        $brokerPid = Get-BrokerPid
        $running = $false
        if ($brokerPid) { try { Get-Process -Id $brokerPid -ErrorAction Stop | Out-Null; $running = $true } catch { } }
        [pscustomobject]@{
            Task = if ($task) { $task.State } else { "NotInstalled" }
            Broker = if ($running) { "Running (PID $brokerPid)" } else { "Stopped" }
            Queue = $queueRoot
            User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        } | Format-List
    }
}
