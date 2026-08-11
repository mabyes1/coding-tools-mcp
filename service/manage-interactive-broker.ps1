[CmdletBinding()]
param(
    [ValidateSet("Install", "Uninstall", "Enable", "Disable", "Start", "Stop", "Status")]
    [string]$Action = "Status"
)

$ErrorActionPreference = "Stop"
$taskName = "WebGPT-Interactive-Broker"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$queueRoot = Join-Path $serviceRoot "interactive-requests"
$broker = Join-Path $serviceRoot "interactive-broker.ps1"
$pwsh = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"

function Get-TaskSafe { Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
function Get-BrokerPid {
    $pidPath = Join-Path $queueRoot "broker.pid"
    if (-not (Test-Path -LiteralPath $pidPath)) { return $null }
    try { return [int](Get-Content -LiteralPath $pidPath -Raw) } catch { return $null }
}

function Get-BrokerProcess {
    $brokerPid = Get-BrokerPid
    if (-not $brokerPid) { return $null }
    return Get-Process -Id $brokerPid -ErrorAction SilentlyContinue
}

function Assert-InteractiveCaller {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $sessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
    if ($identity.User.Value -eq "S-1-5-19" -or $sessionId -le 0) {
        throw "This broker action must run from an interactive signed-in user session, not LocalService or Session 0."
    }
}

function Stop-BrokerInstance {
    $brokerProcess = Get-BrokerProcess
    if (-not $brokerProcess) {
        Remove-Item -LiteralPath (Join-Path $queueRoot "broker.pid") -Force -ErrorAction SilentlyContinue
        return
    }
    $stopPath = Join-Path $queueRoot "broker.stop"
    New-Item -ItemType File -Path $stopPath -Force | Out-Null
    try { Wait-Process -Id $brokerProcess.Id -Timeout 5 -ErrorAction SilentlyContinue } catch { }
    if (Get-Process -Id $brokerProcess.Id -ErrorAction SilentlyContinue) {
        Stop-Process -Id $brokerProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $queueRoot "broker.pid") -Force -ErrorAction SilentlyContinue
}

function Wait-ForInteractiveBroker([int]$TimeoutSeconds = 8) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 250
        $brokerProcess = Get-BrokerProcess
        if ($brokerProcess -and $brokerProcess.SessionId -gt 0) {
            Start-Sleep -Milliseconds 500
            $stable = Get-BrokerProcess
            if ($stable -and $stable.Id -eq $brokerProcess.Id -and $stable.SessionId -gt 0) { return $stable }
        }
    } while ((Get-Date) -lt $deadline)
    return $null
}

switch ($Action) {
    "Install" {
        Assert-InteractiveCaller
        if (-not (Test-Path -LiteralPath $broker -PathType Leaf)) { throw "Interactive broker script is missing: $broker" }
        New-Item -ItemType Directory -Path $queueRoot -Force | Out-Null
        Stop-BrokerInstance
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        $taskAction = New-ScheduledTaskAction -Execute $pwsh -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$broker`""
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        # Deliberately Limited: this broker may execute generic commands, so it
        # must never inherit the elevated fixed-action broker's token.
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Description "Non-elevated signed-in desktop execution broker for WebGPT MCP." -Force | Out-Null
        Enable-ScheduledTask -TaskName $taskName | Out-Null
        Write-Host "INTERACTIVE_BROKER_TASK_INSTALLED"
    }
    "Uninstall" {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        & $PSCommandPath -Action Stop
        Write-Host "INTERACTIVE_BROKER_TASK_REMOVED"
    }
    "Enable" { Enable-ScheduledTask -TaskName $taskName | Out-Null; Write-Host "INTERACTIVE_BROKER_TASK_ENABLED" }
    "Disable" { Disable-ScheduledTask -TaskName $taskName | Out-Null; Write-Host "INTERACTIVE_BROKER_TASK_DISABLED" }
    "Start" {
        $existing = Get-BrokerProcess
        if ($existing -and $existing.SessionId -gt 0) {
            Write-Host "INTERACTIVE_BROKER_ALREADY_RUNNING PID=$($existing.Id) SESSION=$($existing.SessionId)"
            break
        }
        if ($existing) { Stop-BrokerInstance }
        $task = Get-TaskSafe
        if (-not $task) { throw "The interactive broker task is not installed." }
        Start-ScheduledTask -TaskName $taskName
        $started = Wait-ForInteractiveBroker
        if (-not $started) { throw "The non-elevated broker did not start in an interactive user session." }
        Write-Host "INTERACTIVE_BROKER_STARTED PID=$($started.Id) SESSION=$($started.SessionId)"
    }
    "Stop" { Stop-BrokerInstance; Write-Host "INTERACTIVE_BROKER_STOPPED" }
    "Status" {
        $task = Get-TaskSafe
        $brokerProcess = Get-BrokerProcess
        $status = $null
        $statusPath = Join-Path $queueRoot "broker.status.json"
        try { $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json } catch { }
        [pscustomobject]@{
            Task = if ($task) { $task.State } else { "NotInstalled" }
            Broker = if ($brokerProcess) { "Running (PID $($brokerProcess.Id), Session $($brokerProcess.SessionId))" } else { "Stopped" }
            User = if ($status) { $status.username } else { $null }
            RunLevel = if ($status) { $status.run_level } else { $null }
            Queue = $queueRoot
            Caller = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            CallerSession = [Diagnostics.Process]::GetCurrentProcess().SessionId
        } | Format-List
    }
}
