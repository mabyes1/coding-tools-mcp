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
$launcher = Join-Path $serviceRoot "interactive-broker-launcher.exe"

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

function Stop-WebConsoleInstance {
    $webConsolePidPath = Join-Path $queueRoot "web-console.pid"
    if (Test-Path -LiteralPath $webConsolePidPath -PathType Leaf) {
        try {
            $webConsolePid = [int](Get-Content -LiteralPath $webConsolePidPath -Raw)
            if ($webConsolePid -gt 0) { Stop-Process -Id $webConsolePid -Force -ErrorAction SilentlyContinue }
        }
        catch { }
    }
    Remove-Item -LiteralPath $webConsolePidPath,(Join-Path $queueRoot "web-console.heartbeat") -Force -ErrorAction SilentlyContinue
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
        Stop-WebConsoleInstance
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
    Stop-WebConsoleInstance
}

function Wait-ForInteractiveBroker([int]$TimeoutSeconds = 15) {
    $statusPath = Join-Path $queueRoot "broker.status.json"
    $heartbeatPath = Join-Path $queueRoot "broker.heartbeat"
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 250
        $brokerProcess = Get-BrokerProcess
        if ($brokerProcess -and $brokerProcess.SessionId -gt 0) {
            $status = $null
            try {
                $status = [IO.File]::ReadAllText($statusPath, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
            }
            catch { }
            $heartbeatFresh = $false
            try {
                $heartbeatAge = [DateTime]::UtcNow - (Get-Item -LiteralPath $heartbeatPath -ErrorAction Stop).LastWriteTimeUtc
                $heartbeatFresh = $heartbeatAge.TotalSeconds -le 3
            }
            catch { }
            $statusMatches = $status `
                -and [int]$status.pid -eq $brokerProcess.Id `
                -and [int]$status.session_id -eq $brokerProcess.SessionId `
                -and [bool]$status.user_interactive `
                -and -not [bool]$status.elevated
            if ($statusMatches -and $heartbeatFresh) {
                Start-Sleep -Milliseconds 500
                $stable = Get-BrokerProcess
                if ($stable -and $stable.Id -eq $brokerProcess.Id -and $stable.SessionId -eq $brokerProcess.SessionId) {
                    return $stable
                }
            }
        }
    } while ((Get-Date) -lt $deadline)
    return $null
}

switch ($Action) {
    "Install" {
        Assert-InteractiveCaller
        if (-not (Test-Path -LiteralPath $broker -PathType Leaf)) { throw "Interactive broker script is missing: $broker" }
        if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "Interactive broker launcher is missing: $launcher" }
        New-Item -ItemType Directory -Path $queueRoot -Force | Out-Null
        Stop-BrokerInstance
        $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        # Run the PowerShell broker in-process inside a WinExe launcher. Using
        # powershell.exe directly can cause Windows 11 Terminal delegation to
        # create a visible terminal even with -WindowStyle Hidden.
        $taskAction = New-ScheduledTaskAction -Execute $launcher
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        # This is a long-lived desktop broker.  Task Scheduler's defaults are
        # optimized for short jobs and can leave the MCP with a stale broker.pid
        # after an unexpected termination.  Keep the broker alive indefinitely
        # and let Task Scheduler heal it when the process dies.
        $settings = New-ScheduledTaskSettingsSet `
            -MultipleInstances IgnoreNew `
            -RestartCount 999 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -StartWhenAvailable `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries
        # Deliberately Limited: this broker may execute generic commands, so it
        # must never inherit the elevated fixed-action broker's token.
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description "Non-elevated signed-in desktop execution broker for WebGPT MCP." -Force | Out-Null
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
        if ($existing) {
            Stop-BrokerInstance
        }
        else {
            # A hard-killed broker cannot run its finally block, so stale
            # identity files may survive even though the process is gone.
            Remove-Item -LiteralPath (Join-Path $queueRoot "broker.pid"),(Join-Path $queueRoot "broker.status.json") -Force -ErrorAction SilentlyContinue
        }
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
        $webConsoleProcess = $null
        try {
            $webConsolePid = [int](Get-Content -LiteralPath (Join-Path $queueRoot "web-console.pid") -Raw)
            $webConsoleProcess = Get-Process -Id $webConsolePid -ErrorAction SilentlyContinue
        }
        catch { }
        try { $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json } catch { }
        [pscustomobject]@{
            Task = if ($task) { $task.State } else { "NotInstalled" }
            Broker = if ($brokerProcess) { "Running (PID $($brokerProcess.Id), Session $($brokerProcess.SessionId))" } else { "Stopped" }
            WebConsole = if ($webConsoleProcess) { "Running (PID $($webConsoleProcess.Id), http://127.0.0.1:8768)" } else { "Stopped" }
            User = if ($status) { $status.username } else { $null }
            RunLevel = if ($status) { $status.run_level } else { $null }
            Queue = $queueRoot
            Caller = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            CallerSession = [Diagnostics.Process]::GetCurrentProcess().SessionId
        } | Format-List
    }
}
