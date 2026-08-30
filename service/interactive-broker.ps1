[CmdletBinding()]
param(
    [int]$PollMilliseconds = 100
)

$ErrorActionPreference = "Stop"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$queueRoot = Join-Path $serviceRoot "interactive-requests"
$pidPath = Join-Path $queueRoot "broker.pid"
$heartbeatPath = Join-Path $queueRoot "broker.heartbeat"
$statusPath = Join-Path $queueRoot "broker.status.json"
$stopPath = Join-Path $queueRoot "broker.stop"
$logPath = Join-Path $queueRoot "broker.log"
$protocolVersion = 1
$requestTtlSeconds = 900
$maxCapturedBytes = 1048576
$heartbeatIntervalSeconds = 5
$webConsoleHeartbeatTtlSeconds = 90
$workRetentionDays = 7

try {
    $earlyIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $earlySessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
    $earlyLine = "{0} EARLY_BOOT pid={1} session={2} user={3} userInteractive={4}`r`n" -f `
        ([DateTimeOffset]::Now.ToString("o")),
        ([Diagnostics.Process]::GetCurrentProcess().Id),
        $earlySessionId,
        $earlyIdentity.Name,
        ([bool][Environment]::UserInteractive)
    [IO.File]::AppendAllText($logPath, $earlyLine, [Text.UTF8Encoding]::new($false))
}
catch { }

function Write-BrokerLog([string]$Message) {
    try {
        $timestamp = [DateTimeOffset]::Now.ToString("o")
        [IO.File]::AppendAllText($logPath, "$timestamp $Message`r`n", [Text.UTF8Encoding]::new($false))
    }
    catch { }
}

function Write-AtomicJson([string]$Path, [hashtable]$Payload) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, ($Payload | ConvertTo-Json -Compress -Depth 10), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Update-BrokerHeartbeat {
    try {
        [IO.File]::WriteAllText(
            $heartbeatPath,
            [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString(),
            [Text.Encoding]::ASCII
        )
    }
    catch { }
}

function Clear-StaleBrokerArtifacts {
    # A broker restart invalidates unfinished IPC. Replaying an interactive
    # action is unsafe because we cannot know whether it already happened.
    foreach ($pattern in @("*.request", "*.processing", "*.response", "*.browser-extension.pending", "*.web-human-help.json", "*.web-human-help.response", "*.web-human-help.seen")) {
        Get-ChildItem -LiteralPath $queueRoot -Filter $pattern -File -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath (Join-Path $queueRoot "web-console.heartbeat") -Force -ErrorAction SilentlyContinue
    $cutoff = (Get-Date).AddDays(-$workRetentionDays)
    Get-ChildItem -LiteralPath $queueRoot -Directory -Filter "work-*" -ErrorAction SilentlyContinue |
        Where-Object LastWriteTime -lt $cutoff |
        ForEach-Object {
            try { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction Stop }
            catch { Write-BrokerLog "STALE_WORK_CLEANUP_FAILED path=$($_.FullName) error=$($_.Exception.Message)" }
        }
}

function Complete-Request([string]$RequestId, [hashtable]$Response) {
    $Response.protocol = $protocolVersion
    $Response.request_id = $RequestId
    Write-AtomicJson (Join-Path $queueRoot "$RequestId.response") $Response
}

function Convert-UiText([string]$Base64) {
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Base64))
}

$computerUseHelper = Join-Path $PSScriptRoot "computer-use-helper.exe"
$activityLogViewer = Join-Path $PSScriptRoot "activity-log-viewer.exe"
$webConsoleBridge = Join-Path $PSScriptRoot "web-console-bridge.exe"
$activityLogPath = Join-Path $serviceRoot "logs\ai-activity.log"
$activityLogViewerPid = Join-Path $queueRoot "activity-log-viewer.pid"
$activityLogViewerDnd = Join-Path $queueRoot "activity-log-viewer.dnd"
$activityLogViewerDesktopOptIn = Join-Path $queueRoot "activity-log-viewer.desktop"
$webConsolePid = Join-Path $queueRoot "web-console.pid"
$webConsoleHeartbeat = Join-Path $queueRoot "web-console.heartbeat"

function Start-WebConsoleBridge {
    if (-not (Test-Path -LiteralPath $webConsoleBridge -PathType Leaf)) { return }
    $running = $false
    if (Test-Path -LiteralPath $webConsolePid -PathType Leaf) {
        try {
            $bridgePid = [int]([IO.File]::ReadAllText($webConsolePid).Trim())
            if ($bridgePid -gt 0 -and (Get-Process -Id $bridgePid -ErrorAction SilentlyContinue)) { $running = $true }
        }
        catch { }
    }
    if ($running) { return }
    Remove-Item -LiteralPath $webConsolePid,$webConsoleHeartbeat -Force -ErrorAction SilentlyContinue
    try {
        Start-Process -FilePath $webConsoleBridge -WindowStyle Hidden -ArgumentList @(
            "--log", $activityLogPath,
            "--queue", $queueRoot,
            "--permission-mode", (Join-Path $serviceRoot "permission-mode.txt"),
            "--repo", "D:\coding-tools-mcp\coding-tools-mcp",
            "--pid", $webConsolePid,
            "--port", "8768"
        ) | Out-Null
    }
    catch {
        Write-BrokerLog "WEB_CONSOLE_BRIDGE_FAILED $($_.Exception.Message)"
    }
}

function Stop-WebConsoleBridge {
    if (Test-Path -LiteralPath $webConsolePid -PathType Leaf) {
        try {
            $bridgePid = [int]([IO.File]::ReadAllText($webConsolePid).Trim())
            if ($bridgePid -gt 0) { Stop-Process -Id $bridgePid -Force -ErrorAction SilentlyContinue }
        }
        catch { }
    }
    Remove-Item -LiteralPath $webConsolePid,$webConsoleHeartbeat -Force -ErrorAction SilentlyContinue
}

function Test-WebConsoleConnected {
    if (-not (Test-Path -LiteralPath $webConsoleHeartbeat -PathType Leaf)) { return $false }
    try {
        # Do not open/read the heartbeat file here. WebConsoleBridge replaces it
        # atomically on every poll, and a content read can briefly collide with
        # that replace/write and incorrectly report the console as disconnected.
        # File metadata is sufficient for liveness and does not contend with the
        # writer's content handle.
        $lastWriteUtc = [IO.File]::GetLastWriteTimeUtc($webConsoleHeartbeat)
        if ($lastWriteUtc -eq [DateTime]::MinValue) { return $false }
        $ageSeconds = ([DateTime]::UtcNow - $lastWriteUtc).TotalSeconds
        return ($ageSeconds -ge -5 -and $ageSeconds -le $webConsoleHeartbeatTtlSeconds)
    }
    catch { return $false }
}

function Start-ActivityLogViewer {
    # The browser drawer is the primary surface. Keep the legacy desktop viewer
    # available as an explicit opt-in fallback without opening a separate window
    # on every broker start or MCP action.
    if (-not (Test-Path -LiteralPath $activityLogViewerDesktopOptIn -PathType Leaf)) {
        Remove-Item -LiteralPath $activityLogViewerPid -Force -ErrorAction SilentlyContinue
        return
    }
    if (-not (Test-Path -LiteralPath $activityLogViewer -PathType Leaf)) { return }
    $running = $false
    if (Test-Path -LiteralPath $activityLogViewerPid -PathType Leaf) {
        try {
            $viewerPid = [int]([IO.File]::ReadAllText($activityLogViewerPid).Trim())
            if ($viewerPid -gt 0 -and (Get-Process -Id $viewerPid -ErrorAction SilentlyContinue)) { $running = $true }
        }
        catch { }
    }
    if ($running) { return }

    try {
        Start-Process -FilePath $activityLogViewer -ArgumentList @(
            "--log", $activityLogPath,
            "--pid", $activityLogViewerPid,
            "--dnd", $activityLogViewerDnd
        ) | Out-Null
    }
    catch {
        Write-BrokerLog "ACTIVITY_LOG_VIEWER_FAILED $($_.Exception.Message)"
    }
}

function Try-HandleHumanHelpInWebConsole([string]$RequestId, $Request, [int]$TimeoutSeconds) {
    if (-not (Test-WebConsoleConnected)) { return $false }

    $pendingPath = Join-Path $queueRoot "$RequestId.web-human-help.json"
    $webResponsePath = Join-Path $queueRoot "$RequestId.web-human-help.response"
    $webSeenPath = Join-Path $queueRoot "$RequestId.web-human-help.seen"
    $webActivityPath = Join-Path $queueRoot "$RequestId.web-human-help.activity"
    $payload = @{
        protocol = $protocolVersion
        request_id = $RequestId
        created_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        reason = [string]$Request.reason
        request = [string]$Request.request
        expected_result = [string]$Request.expected_result
        return_to_agent = [string]$Request.return_to_agent
        mode = [string]$Request.mode
        fallback = [string]$Request.fallback
        timeout_seconds = $TimeoutSeconds
    }
    try {
        Write-AtomicJson $pendingPath $payload
        Write-BrokerLog "HUMAN_HELP_WEB_START id=$RequestId timeout=$TimeoutSeconds"
        $deliveryDeadline = [DateTimeOffset]::UtcNow.AddSeconds([Math]::Min(5, [Math]::Max(1, $TimeoutSeconds)))
        while ([DateTimeOffset]::UtcNow -lt $deliveryDeadline -and -not (Test-Path -LiteralPath $webSeenPath -PathType Leaf)) {
            Start-Sleep -Milliseconds 100
        }
        if (-not (Test-Path -LiteralPath $webSeenPath -PathType Leaf)) {
            Write-BrokerLog "HUMAN_HELP_WEB_NOT_SEEN id=$RequestId fallback=desktop"
            return $false
        }
        Write-BrokerLog "HUMAN_HELP_WEB_SEEN id=$RequestId"
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
        $lastWebActivityUtc = [DateTime]::MinValue
        while ([DateTimeOffset]::UtcNow -lt $deadline) {
            if (Test-Path -LiteralPath $webResponsePath -PathType Leaf) {
                $webResponse = [IO.File]::ReadAllText($webResponsePath, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
                if ([string]$webResponse.request_id -ne $RequestId) { throw "Web console response id did not match." }
                $answer = [string]$webResponse.answer
                $requestedOutcome = [string]$webResponse.outcome
                $outcome = if ($requestedOutcome -eq "cancelled") { "skip" } elseif ([string]::IsNullOrWhiteSpace($answer)) { "done" } else { "submitted" }
                Complete-Request $RequestId @{
                    ok = $true
                    status = "human_response"
                    outcome = $outcome
                    answer = $answer
                    timed_out = $false
                    mode = [string]$Request.mode
                    fallback = [string]$Request.fallback
                    execution_context = "web_console"
                    message = "Human-help web console prompt completed."
                    retryable = $false
                }
                Write-BrokerLog "HUMAN_HELP_WEB_END id=$RequestId outcome=$outcome"
                return $true
            }
            if (Test-Path -LiteralPath $webActivityPath -PathType Leaf) {
                $webActivityUtc = (Get-Item -LiteralPath $webActivityPath -ErrorAction SilentlyContinue).LastWriteTimeUtc
                if ($null -ne $webActivityUtc -and $webActivityUtc -gt $lastWebActivityUtc) {
                    $lastWebActivityUtc = $webActivityUtc
                    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
                    Write-BrokerLog "HUMAN_HELP_WEB_ACTIVITY id=$RequestId timeout_reset=$TimeoutSeconds"
                }
            }
            Start-Sleep -Milliseconds 100
        }

        Complete-Request $RequestId @{
            ok = $true
            status = "human_response"
            outcome = "timeout"
            answer = ""
            timed_out = $true
            mode = [string]$Request.mode
            fallback = [string]$Request.fallback
            execution_context = "web_console"
            message = "Human-help web console prompt timed out."
            retryable = $false
        }
        Write-BrokerLog "HUMAN_HELP_WEB_END id=$RequestId outcome=timeout"
        return $true
    }
    catch {
        Write-BrokerLog "HUMAN_HELP_WEB_ERROR id=$RequestId error=$($_.Exception.Message) fallback=desktop"
        return $false
    }
    finally {
        Remove-Item -LiteralPath $pendingPath,$webResponsePath,$webSeenPath,$webActivityPath -Force -ErrorAction SilentlyContinue
    }
}

function Handle-ComputerUseRequest([string]$RequestId, $Request) {
    if ([bool]$Request.browser_only) {
        Handle-BrowserExtensionRequest $RequestId $Request
        return
    }
    if (-not (Test-Path -LiteralPath $computerUseHelper -PathType Leaf)) {
        Complete-Request $RequestId @{ ok = $false; error = "COMPUTER_USE_UNAVAILABLE"; message = "Computer Use helper is not installed."; retryable = $true }
        return
    }
    $scratchRoot = Join-Path $queueRoot ("computer-use-" + $RequestId)
    $responsePath = Join-Path $scratchRoot "response.json"
    $errorPath = Join-Path $scratchRoot "error.txt"
    New-Item -ItemType Directory -Path $scratchRoot -Force | Out-Null
    try {
        $requestJson = $Request | ConvertTo-Json -Compress -Depth 12
        $requestBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($requestJson))
        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $computerUseHelper
        $startInfo.Arguments = "--request-base64 $requestBase64 --response-file `"$responsePath`" --error-file `"$errorPath`""
        $startInfo.WorkingDirectory = $PSScriptRoot
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            throw "Computer Use helper could not be started."
        }
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(60)
        while (-not $process.HasExited -and [DateTimeOffset]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 50
            $process.Refresh()
        }
        if (-not $process.HasExited) {
            Stop-ProcessTree $process.Id
            Complete-Request $RequestId @{ ok = $false; error = "COMPUTER_USE_TIMEOUT"; message = "Computer Use helper timed out."; retryable = $true }
            return
        }
        $process.WaitForExit()
        $exitCode = $process.ExitCode
        $process.Dispose()
        $process = $null
        $stdout = if (Test-Path -LiteralPath $responsePath -PathType Leaf) {
            [IO.File]::ReadAllText($responsePath, [Text.UTF8Encoding]::new($false))
        } else { "" }
        $stderr = if (Test-Path -LiteralPath $errorPath -PathType Leaf) {
            [IO.File]::ReadAllText($errorPath, [Text.UTF8Encoding]::new($false))
        } else { "" }
        if ($exitCode -ne 0) {
            Write-BrokerLog "COMPUTER_USE_HELPER_FAILED id=$RequestId exit=$exitCode stderr=$($stderr.Trim())"
            Complete-Request $RequestId @{ ok = $false; error = "COMPUTER_USE_HELPER_FAILED"; message = ($stderr.Trim() | Select-Object -First 1); retryable = $true }
            return
        }
        $decoded = $stdout | ConvertFrom-Json -ErrorAction Stop
        $payload = @{}
        if ($null -eq $decoded) {
            throw "Computer Use helper returned an empty JSON payload."
        }
        foreach ($property in @($decoded.PSObject.Properties)) {
            if ($null -eq $property) { continue }
            $name = [string]$property.Name
            if ([string]::IsNullOrWhiteSpace($name)) { continue }
            $payload[$name] = $property.Value
        }
        Write-BrokerLog "COMPUTER_USE_HELPER_OK id=$RequestId exit=$exitCode stdout_chars=$($stdout.Length) keys=$([string]::Join(',', @($payload.Keys)))"
        if ($payload.Count -eq 0) {
            throw "Computer Use helper JSON parsed successfully but exposed no properties."
        }
        Complete-Request $RequestId $payload
    }
    catch {
        Write-BrokerLog "COMPUTER_USE_BROKER_ERROR id=$RequestId type=$($_.Exception.GetType().FullName) message=$($_.Exception.Message)"
        Complete-Request $RequestId @{ ok = $false; error = "COMPUTER_USE_BROKER_ERROR"; message = $_.Exception.Message; retryable = $true }
    }
    finally {
        if ($process) {
            try { $process.Dispose() } catch { }
        }
        Remove-Item -LiteralPath $scratchRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Handle-BrowserExtensionRequest([string]$RequestId, $Request) {
    Start-WebConsoleBridge
    $pendingPath = Join-Path $queueRoot "$RequestId.browser-extension.pending"
    $processingPath = Join-Path $queueRoot "$RequestId.browser-extension.processing"
    $responsePath = Join-Path $queueRoot "$RequestId.browser-extension.response"
    try {
        $temporary = "$pendingPath.$([Guid]::NewGuid().ToString('N')).tmp"
        [IO.File]::WriteAllText(
            $temporary,
            ($Request | ConvertTo-Json -Compress -Depth 12),
            [Text.UTF8Encoding]::new($false)
        )
        Move-Item -LiteralPath $temporary -Destination $pendingPath -Force
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(65)
        while ([DateTimeOffset]::UtcNow -lt $deadline) {
            if (Test-Path -LiteralPath $responsePath -PathType Leaf) {
                $decoded = [IO.File]::ReadAllText($responsePath, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json -ErrorAction Stop
                $payload = @{}
                foreach ($property in @($decoded.PSObject.Properties)) {
                    if ($null -eq $property -or [string]::IsNullOrWhiteSpace([string]$property.Name)) { continue }
                    $payload[[string]$property.Name] = $property.Value
                }
                if ($payload.Count -eq 0) { throw "Browser extension returned an empty response." }
                Write-BrokerLog "BROWSER_EXTENSION_OK id=$RequestId action=$([string]$Request.action) keys=$([string]::Join(',', @($payload.Keys)))"
                Complete-Request $RequestId $payload
                return
            }
            Start-Sleep -Milliseconds 75
        }
        $heartbeat = Join-Path $queueRoot "browser-extension.heartbeat"
        $heartbeatAge = if (Test-Path -LiteralPath $heartbeat -PathType Leaf) {
            [Math]::Round(([DateTimeOffset]::UtcNow - [DateTimeOffset](Get-Item -LiteralPath $heartbeat).LastWriteTimeUtc).TotalSeconds, 1)
        } else { $null }
        Complete-Request $RequestId @{
            ok = $false
            error = "BROWSER_EXTENSION_UNAVAILABLE"
            message = "Coding Tools Chrome extension did not answer the Browser Use request. Reload the unpacked extension or open Chrome and try again."
            retryable = $true
            heartbeat_age_seconds = $heartbeatAge
        }
    }
    catch {
        Write-BrokerLog "BROWSER_EXTENSION_ERROR id=$RequestId error=$($_.Exception.Message)"
        Complete-Request $RequestId @{ ok = $false; error = "BROWSER_EXTENSION_ERROR"; message = $_.Exception.Message; retryable = $true }
    }
    finally {
        Remove-Item -LiteralPath $pendingPath,$processingPath,$responsePath -Force -ErrorAction SilentlyContinue
    }
}

function Test-PatternMatch([string]$Name, $Patterns) {
    foreach ($pattern in @($Patterns)) {
        if ($Name -like [string]$pattern) { return $true }
    }
    return $false
}

function Set-ChildEnvironment($Policy, $Overrides, [string]$ScratchRoot) {
    $original = @{}
    foreach ($entry in [Environment]::GetEnvironmentVariables("Process").GetEnumerator()) {
        $original[[string]$entry.Key] = [string]$entry.Value
    }

    $inherit = [string]$Policy.inherit
    $coreNames = @($Policy.core_names | ForEach-Object { ([string]$_).ToUpperInvariant() })
    $includeOnly = @($Policy.include_only)
    $exclude = @($Policy.exclude)
    foreach ($name in @($original.Keys)) {
        $keep = $true
        if ($inherit -eq "none") { $keep = $false }
        elseif ($inherit -eq "core" -and $name.ToUpperInvariant() -notin $coreNames) { $keep = $false }
        if ($keep -and $includeOnly.Count -gt 0 -and -not (Test-PatternMatch $name $includeOnly)) { $keep = $false }
        if ($keep -and $exclude.Count -gt 0 -and (Test-PatternMatch $name $exclude)) { $keep = $false }
        if (-not $keep) { [Environment]::SetEnvironmentVariable($name, $null, "Process") }
    }

    foreach ($property in @($Policy.set.PSObject.Properties)) {
        [Environment]::SetEnvironmentVariable([string]$property.Name, [string]$property.Value, "Process")
    }
    foreach ($property in @($Overrides.PSObject.Properties)) {
        [Environment]::SetEnvironmentVariable([string]$property.Name, [string]$property.Value, "Process")
    }
    foreach ($pair in @{
        MCP_SESSION_TMP = $ScratchRoot
        TEMP = $ScratchRoot
        TMP = $ScratchRoot
        TMPDIR = $ScratchRoot
    }.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable([string]$pair.Key, [string]$pair.Value, "Process")
    }
    return $original
}

function Restore-BrokerEnvironment($Original) {
    foreach ($entry in [Environment]::GetEnvironmentVariables("Process").GetEnumerator()) {
        [Environment]::SetEnvironmentVariable([string]$entry.Key, $null, "Process")
    }
    foreach ($entry in $Original.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable([string]$entry.Key, [string]$entry.Value, "Process")
    }
}

function Read-CapturedText([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return @{ text = ""; total_bytes = 0; truncated = $false }
    }
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        $total = $stream.Length
        $start = [Math]::Max(0L, $total - $maxCapturedBytes)
        if ($start -gt 0) { [void]$stream.Seek($start, [IO.SeekOrigin]::Begin) }
        $count = [int]($total - $start)
        $buffer = New-Object byte[] $count
        $read = $stream.Read($buffer, 0, $count)
        $text = [Text.Encoding]::UTF8.GetString($buffer, 0, $read)
        return @{ text = $text; total_bytes = [long]$total; truncated = ($start -gt 0) }
    }
    finally {
        $stream.Dispose()
    }
}

function Stop-ProcessTree([int]$ProcessId) {
    try { & "$env:WINDIR\System32\taskkill.exe" /PID $ProcessId /T /F 2>$null | Out-Null } catch { }
}

function Handle-ExecRequest([string]$RequestId, $Request) {
    $command = [string]$Request.cmd
    $workingDirectory = [string]$Request.cwd
    $timeoutMs = [int]$Request.timeout_ms
    if ([string]::IsNullOrWhiteSpace($command) -or $command.Length -gt 262144) {
        Complete-Request $RequestId @{ ok = $false; error = "INTERACTIVE_REQUEST_INVALID"; message = "Command is missing or too large."; retryable = $false }
        return
    }
    if (-not (Test-Path -LiteralPath $workingDirectory -PathType Container)) {
        Complete-Request $RequestId @{ ok = $false; error = "INTERACTIVE_WORKDIR_INVALID"; message = "Interactive command working directory does not exist."; retryable = $false }
        return
    }

    $parseTokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput(
        $command,
        [ref]$parseTokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count -gt 0) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $parseMessage = (@($parseErrors | ForEach-Object { $_.Message }) -join [Environment]::NewLine).Trim()
        Complete-Request $RequestId @{
            ok = $true
            status = "exited"
            exit_code = 1
            timed_out = $false
            elapsed_ms = 0
            process_id = $null
            stdout = ""
            stderr = $parseMessage
            stdout_total_bytes = 0
            stderr_total_bytes = [Text.Encoding]::UTF8.GetByteCount($parseMessage)
            stdout_truncated = $false
            stderr_truncated = $false
            execution_context = "active_user"
            execution_identity = @{
                username = $identity.Name
                session_id = [Diagnostics.Process]::GetCurrentProcess().SessionId
                elevated = $false
                run_level = "limited"
            }
            message = "Interactive command contains a PowerShell syntax error."
            retryable = $false
        }
        return
    }
    $timeoutMs = [Math]::Max(1000, [Math]::Min($timeoutMs, 600000))
    $scratchRoot = Join-Path $queueRoot ("work-" + $RequestId)
    New-Item -ItemType Directory -Path $scratchRoot -Force | Out-Null
    $stdoutPath = Join-Path $scratchRoot "stdout.txt"
    $stderrPath = Join-Path $scratchRoot "stderr.txt"
    $originalEnvironment = $null
    $process = $null
    $started = [Diagnostics.Stopwatch]::StartNew()
    try {
        $originalEnvironment = Set-ChildEnvironment $Request.env_policy $Request.env_overrides $scratchRoot
        $shellCandidates = @(
            "C:\Program Files\PowerShell\7\pwsh.exe",
            (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe")
        )
        $shell = $shellCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (-not $shell) { throw "PowerShell is unavailable in the interactive user session." }
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
        $argumentList = @("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded)
        $process = Start-Process -FilePath $shell -ArgumentList $argumentList -WorkingDirectory $workingDirectory -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        $deadline = [DateTimeOffset]::UtcNow.AddMilliseconds($timeoutMs)
        while (-not $process.HasExited -and [DateTimeOffset]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 50
            $process.Refresh()
        }
        $timedOut = -not $process.HasExited
        if ($timedOut) {
            Stop-ProcessTree $process.Id
            try { $process.WaitForExit(3000) } catch { }
            $process.Refresh()
        }
        else {
            # Start-Process can report HasExited before the Process object has
            # finalized ExitCode/redirected-stream bookkeeping.  A zero-time
            # race here previously surfaced successful active_user commands as
            # exit_code=null.
            $process.WaitForExit()
            $process.Refresh()
        }
        $exitCode = if ($timedOut) { $null } else { [int]$process.ExitCode }
        $started.Stop()
        $stdout = Read-CapturedText $stdoutPath
        $stderr = Read-CapturedText $stderrPath
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        Complete-Request $RequestId @{
            ok = $true
            status = if ($timedOut) { "timeout" } else { "exited" }
            exit_code = $exitCode
            timed_out = $timedOut
            elapsed_ms = [int]$started.ElapsedMilliseconds
            process_id = $process.Id
            stdout = [string]$stdout.text
            stderr = [string]$stderr.text
            stdout_total_bytes = [long]$stdout.total_bytes
            stderr_total_bytes = [long]$stderr.total_bytes
            stdout_truncated = [bool]$stdout.truncated
            stderr_truncated = [bool]$stderr.truncated
            execution_context = "active_user"
            execution_identity = @{
                username = $identity.Name
                session_id = [Diagnostics.Process]::GetCurrentProcess().SessionId
                elevated = $false
                run_level = "limited"
            }
            message = if ($timedOut) { "Interactive command timed out and its process tree was terminated." } else { "Interactive command completed." }
            retryable = $false
        }
    }
    catch {
        if ($process -and -not $process.HasExited) { Stop-ProcessTree $process.Id }
        Complete-Request $RequestId @{ ok = $false; error = "INTERACTIVE_BROKER_ERROR"; message = $_.Exception.Message; retryable = $true }
    }
    finally {
        if ($originalEnvironment) { Restore-BrokerEnvironment $originalEnvironment }
        Remove-Item -LiteralPath $scratchRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Handle-HumanHelpRequest([string]$RequestId, $Request) {
    $requestText = [string]$Request.request
    $expectedResult = [string]$Request.expected_result
    $returnToAgent = [string]$Request.return_to_agent
    $reason = [string]$Request.reason
    $mode = [string]$Request.mode
    $fallback = [string]$Request.fallback
    $delivery = [string]$Request.delivery
    if ([string]::IsNullOrWhiteSpace($delivery)) { $delivery = "auto" }
    $timeoutSeconds = [Math]::Max(5, [Math]::Min([int]$Request.timeout_seconds, 300))
    if ([string]::IsNullOrWhiteSpace($requestText) -or $requestText.Length -gt 4000) {
        Complete-Request $RequestId @{ ok = $false; error = "HUMAN_HELP_REQUEST_INVALID"; message = "Human-help request is missing or too large."; retryable = $false }
        return
    }

    if ($delivery -ne "desktop_only" -and (Try-HandleHumanHelpInWebConsole $RequestId $Request $timeoutSeconds)) { return }

    try {
        Write-BrokerLog "HUMAN_HELP_START id=$RequestId reason=$reason mode=$mode fallback=$fallback timeout=$timeoutSeconds"
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing

        # Layout deliberately follows the human-help reference: 836 x 709, a dark
        # desktop surface, hero area, request card, reply box, and paired actions.
        $state = @{
            outcome = "closed"
            timed_out = $false
            remaining = $timeoutSeconds
            last_input_at = $null
        }
        $typingGraceSeconds = 4
        $cornerRadius = 18
        $uiFontName = "Microsoft JhengHei UI"
        $ink = [System.Drawing.Color]::FromArgb(235, 240, 248)
        $muted = [System.Drawing.Color]::FromArgb(166, 177, 195)
        $form = New-Object System.Windows.Forms.Form
        $form.Text = Convert-UiText "5Yqp44GR44Gm44GP44Gg44GV44GE44CB44Kx44Oz5qeY"
        $form.StartPosition = "CenterScreen"; $form.TopMost = $true
        $form.Width = 836; $form.Height = 709
        $form.MinimumSize = New-Object System.Drawing.Size(836, 709)
        $form.MaximumSize = New-Object System.Drawing.Size(836, 709)
        $form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::None
        $form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
        $form.MaximizeBox = $false; $form.ShowInTaskbar = $true
        $form.BackColor = [System.Drawing.Color]::FromArgb(12, 18, 25)
        $form.ForeColor = $ink

        $applyRoundedRegion = {
            $width = $form.ClientSize.Width
            $height = $form.ClientSize.Height
            if ($width -le ($cornerRadius * 2) -or $height -le ($cornerRadius * 2)) { return }
            $diameter = $cornerRadius * 2
            $path = New-Object System.Drawing.Drawing2D.GraphicsPath
            $path.AddArc(0, 0, $diameter, $diameter, 180, 90)
            $path.AddArc($width - $diameter - 1, 0, $diameter, $diameter, 270, 90)
            $path.AddArc($width - $diameter - 1, $height - $diameter - 1, $diameter, $diameter, 0, 90)
            $path.AddArc(0, $height - $diameter - 1, $diameter, $diameter, 90, 90)
            $path.CloseFigure()
            $oldRegion = $form.Region
            $form.Region = New-Object System.Drawing.Region($path)
            if ($oldRegion) { $oldRegion.Dispose() }
            $path.Dispose()
        }
        $form.Add_SizeChanged({ & $applyRoundedRegion })

        $setRoundedRegion = {
            param($control, [int]$radius)
            $width = $control.ClientSize.Width
            $height = $control.ClientSize.Height
            if ($width -le ($radius * 2) -or $height -le ($radius * 2)) { return }
            $diameter = $radius * 2
            $path = New-Object System.Drawing.Drawing2D.GraphicsPath
            $path.AddArc(0, 0, $diameter, $diameter, 180, 90)
            $path.AddArc($width - $diameter - 1, 0, $diameter, $diameter, 270, 90)
            $path.AddArc($width - $diameter - 1, $height - $diameter - 1, $diameter, $diameter, 0, 90)
            $path.AddArc(0, $height - $diameter - 1, $diameter, $diameter, 90, 90)
            $path.CloseFigure()
            $oldRegion = $control.Region
            $control.Region = New-Object System.Drawing.Region($path)
            if ($oldRegion) { $oldRegion.Dispose() }
            $path.Dispose()
        }

        $header = New-Object System.Windows.Forms.Panel
        $header.BackColor = [System.Drawing.Color]::FromArgb(15, 22, 31)
        $header.BorderStyle = [System.Windows.Forms.BorderStyle]::None
        $header.Left = 0; $header.Top = 0; $header.Width = 836; $header.Height = 53; $header.Anchor = "Top,Left,Right"
        $form.Controls.Add($header)

        $headerDivider = New-Object System.Windows.Forms.Panel
        $headerDivider.BackColor = [System.Drawing.Color]::FromArgb(37, 48, 62)
        $headerDivider.Left = 18; $headerDivider.Top = 52; $headerDivider.Width = 800; $headerDivider.Height = 1
        $header.Controls.Add($headerDivider)

        $windowTitle = New-Object System.Windows.Forms.Label
        $windowTitle.Text = Convert-UiText "5Yqp44GR44Gm44GP44Gg44GV44GE44CB44Kx44Oz5qeY"; $windowTitle.AutoSize = $true
        $windowTitle.Font = New-Object System.Drawing.Font($uiFontName, 11); $windowTitle.ForeColor = $ink; $windowTitle.Left = 18; $windowTitle.Top = 16
        $header.Controls.Add($windowTitle)
        $minimizeButton = New-Object System.Windows.Forms.Button
        $minimizeButton.Text = Convert-UiText "4oiS"; $minimizeButton.Font = New-Object System.Drawing.Font($uiFontName, 12)
        $minimizeButton.FlatStyle = "Flat"; $minimizeButton.FlatAppearance.BorderSize = 0; $minimizeButton.BackColor = $header.BackColor; $minimizeButton.ForeColor = $muted
        $minimizeButton.Left = 714; $minimizeButton.Top = 4; $minimizeButton.Width = 48; $minimizeButton.Height = 44; $minimizeButton.Anchor = "Top,Right"
        $minimizeButton.Add_Click({ $form.WindowState = "Minimized" }); $header.Controls.Add($minimizeButton)
        $closeButton = New-Object System.Windows.Forms.Button
        $closeButton.Text = Convert-UiText "w5c="; $closeButton.Font = New-Object System.Drawing.Font($uiFontName, 14)
        $closeButton.FlatStyle = "Flat"; $closeButton.FlatAppearance.BorderSize = 0; $closeButton.BackColor = $header.BackColor; $closeButton.ForeColor = $muted
        $closeButton.Left = 770; $closeButton.Top = 4; $closeButton.Width = 48; $closeButton.Height = 44; $closeButton.Anchor = "Top,Right"
        $closeButton.Add_Click({ $form.Close() }); $header.Controls.Add($closeButton)

        $dragState = @{ active = $false; cursor = $null; location = $null }
        $startWindowDrag = {
            param($sender, $eventArgs)
            if ($eventArgs.Button -ne [System.Windows.Forms.MouseButtons]::Left) { return }
            $dragState.active = $true
            $dragState.cursor = [System.Windows.Forms.Cursor]::Position
            $dragState.location = $form.Location
            $sender.Capture = $true
        }
        $moveWindowDrag = {
            param($sender, $eventArgs)
            if (-not $dragState.active -or [System.Windows.Forms.Control]::MouseButtons -ne [System.Windows.Forms.MouseButtons]::Left) { return }
            $cursor = [System.Windows.Forms.Cursor]::Position
            $newX = ([int]$dragState.location.X) + ([int]$cursor.X) - ([int]$dragState.cursor.X)
            $newY = ([int]$dragState.location.Y) + ([int]$cursor.Y) - ([int]$dragState.cursor.Y)
            $form.Location = [System.Drawing.Point]::new($newX, $newY)
        }
        $stopWindowDrag = {
            param($sender, $eventArgs)
            $dragState.active = $false
            $sender.Capture = $false
        }
        foreach ($dragSurface in @($header, $windowTitle)) {
            $dragSurface.Add_MouseDown($startWindowDrag)
            $dragSurface.Add_MouseMove($moveWindowDrag)
            $dragSurface.Add_MouseUp($stopWindowDrag)
        }

        $title = New-Object System.Windows.Forms.Label
        $title.Text = Convert-UiText "5Yqp44GR44Gm44GP44Gg44GV44GE44CB44Kx44Oz5qeY"; $title.AutoSize = $true
        $title.Font = New-Object System.Drawing.Font($uiFontName, 23, [System.Drawing.FontStyle]::Bold)
        $title.ForeColor = $ink; $title.Left = 36; $title.Top = 76
        $form.Controls.Add($title)

        $meta = New-Object System.Windows.Forms.Label
        $meta.Text = if ($mode -eq "blocking") { Convert-UiText "6YCZ5LiA5q2l55yf55qE6ZyA6KaB5L2g55qE5Y2U5Yqp77yM5oiR5YWI5Y6a6JGX6IeJ55qu5rGC5pWR5LiA5LiL44CC" } else { Convert-UiText "6YCZ5LiA5q2l5L2g5YGa5pyD5b+r5b6I5aSa77yM5oiR5YWI5Y6a6JGX6IeJ55qu5rGC5pWR5LiA5LiL44CC" }
        $meta.Font = New-Object System.Drawing.Font($uiFontName, 11)
        $meta.ForeColor = $muted; $meta.AutoSize = $true; $meta.Left = 38; $meta.Top = 125
        $form.Controls.Add($meta)

        $mascotAsset = Join-Path $PSScriptRoot "assets\human-help-mascot.png"
        $mascot = New-Object System.Windows.Forms.PictureBox
        $mascot.SizeMode = [System.Windows.Forms.PictureBoxSizeMode]::Zoom
        $mascot.BackColor = [System.Drawing.Color]::Transparent
        if (Test-Path -LiteralPath $mascotAsset -PathType Leaf) {
            $mascot.Image = [System.Drawing.Image]::FromFile($mascotAsset)
        }
        $mascot.Left = 658; $mascot.Top = 57; $mascot.Width = 134; $mascot.Height = 118; $mascot.Anchor = "Top,Right"
        $form.Controls.Add($mascot)

        $requestPanel = New-Object System.Windows.Forms.Panel
        $requestPanel.BackColor = [System.Drawing.Color]::FromArgb(20, 28, 38)
        $requestPanel.BorderStyle = [System.Windows.Forms.BorderStyle]::None
        $requestPanel.Left = 36; $requestPanel.Top = 174; $requestPanel.Width = 756; $requestPanel.Height = 248
        $form.Controls.Add($requestPanel)
        & $setRoundedRegion $requestPanel 16

        $requestHeading = New-Object System.Windows.Forms.Label
        $requestHeading.Text = Convert-UiText "6KuL5ZyoIFBvd2VyU2hlbGwg5Z+36KGM6YCZ5qKd5oyH5Luk77yM5a6M5oiQ5b6M5oqK6Ly45Ye66LK85Zue5L6G44CC"; $requestHeading.AutoSize = $true
        $requestHeading.Font = New-Object System.Drawing.Font($uiFontName, 12, [System.Drawing.FontStyle]::Bold)
        $requestHeading.ForeColor = $ink; $requestHeading.Left = 22; $requestHeading.Top = 18
        $requestPanel.Controls.Add($requestHeading)

        $requestBoxPanel = New-Object System.Windows.Forms.Panel
        $requestBoxPanel.BackColor = [System.Drawing.Color]::FromArgb(10, 16, 23)
        $requestBoxPanel.Left = 22; $requestBoxPanel.Top = 52; $requestBoxPanel.Width = 712; $requestBoxPanel.Height = 88
        $requestPanel.Controls.Add($requestBoxPanel)
        & $setRoundedRegion $requestBoxPanel 11

        $requestBox = New-Object System.Windows.Forms.RichTextBox
        $requestBox.ReadOnly = $true; $requestBox.ScrollBars = [System.Windows.Forms.RichTextBoxScrollBars]::None
        $requestBox.WordWrap = $true; $requestBox.DetectUrls = $false
        $requestBox.Font = New-Object System.Drawing.Font($uiFontName, 10)
        $requestBox.BorderStyle = [System.Windows.Forms.BorderStyle]::None
        $requestBox.BackColor = [System.Drawing.Color]::FromArgb(10, 16, 23); $requestBox.ForeColor = [System.Drawing.Color]::FromArgb(213, 224, 238)
        $requestBox.Left = 12; $requestBox.Top = 10; $requestBox.Width = 688; $requestBox.Height = 68; $requestBox.Text = $requestText
        $requestBoxPanel.Controls.Add($requestBox)

        $separator = New-Object System.Windows.Forms.Panel
        $separator.BackColor = [System.Drawing.Color]::FromArgb(42, 54, 69); $separator.Left = 22; $separator.Top = 153; $separator.Width = 712; $separator.Height = 1
        $requestPanel.Controls.Add($separator)

        $details = New-Object System.Windows.Forms.Label
        $detailLines = @()
        if (-not [string]::IsNullOrWhiteSpace($expectedResult)) { $detailLines += ((Convert-UiText "5oiR5pyf5b6F55yL5Yiw77ya") + $expectedResult) }
        if (-not [string]::IsNullOrWhiteSpace($returnToAgent)) { $detailLines += (Convert-UiText "5ou/5Yiw57WQ5p6c5b6M77yM5oiR5pyD57m857qM5LiL5LiA5q2l44CC") }
        if ($detailLines.Count -eq 0) { $detailLines += (Convert-UiText "5LiN55So5a+r5b6X5b6I5a6M5pW077yM6LK85LiK5L2g55yL5Yiw55qE5YWn5a655bCx5aW944CC") }
        $details.Text = ($detailLines -join "`r`n")
        $details.Font = New-Object System.Drawing.Font($uiFontName, 9)
        $details.ForeColor = $muted; $details.Left = 22; $details.Top = 169; $details.Width = 712; $details.Height = 62
        $requestPanel.Controls.Add($details)

        $answerLabel = New-Object System.Windows.Forms.Label
        $answerLabel.Text = Convert-UiText "5aaC5p6c5pa55L6/77yM6KuL5oqK57WQ5p6c5oiW5LiA5Y+l6Kmx55WZ57Wm5oiR77ya"; $answerLabel.AutoSize = $true
        $answerLabel.Font = New-Object System.Drawing.Font($uiFontName, 11); $answerLabel.ForeColor = $ink; $answerLabel.Left = 38; $answerLabel.Top = 443
        $form.Controls.Add($answerLabel)

        $answerBoxPanel = New-Object System.Windows.Forms.Panel
        $answerBoxPanel.BackColor = [System.Drawing.Color]::FromArgb(17, 25, 34)
        $answerBoxPanel.Left = 36; $answerBoxPanel.Top = 474; $answerBoxPanel.Width = 756; $answerBoxPanel.Height = 112
        $form.Controls.Add($answerBoxPanel)
        & $setRoundedRegion $answerBoxPanel 14

        $answerBox = New-Object System.Windows.Forms.RichTextBox
        $answerBox.ScrollBars = [System.Windows.Forms.RichTextBoxScrollBars]::None
        $answerBox.WordWrap = $true; $answerBox.DetectUrls = $false; $answerBox.AcceptsTab = $false
        $answerBox.Font = New-Object System.Drawing.Font($uiFontName, 10)
        $answerBox.BorderStyle = [System.Windows.Forms.BorderStyle]::None; $answerBox.BackColor = $answerBoxPanel.BackColor; $answerBox.ForeColor = $ink
        $answerBox.Left = 14; $answerBox.Top = 13; $answerBox.Width = 728; $answerBox.Height = 86
        $answerBoxPanel.Controls.Add($answerBox)

        $placeholder = New-Object System.Windows.Forms.Label
        $placeholder.Text = Convert-UiText "5Zyo6YCZ6KOh6Ly45YWl77ybRW50ZXIg6YCB5Ye677yMQ3RybCArIEVudGVyIOaPm+ihjA=="; $placeholder.AutoSize = $true
        $placeholder.Font = New-Object System.Drawing.Font($uiFontName, 10); $placeholder.ForeColor = [System.Drawing.Color]::FromArgb(110, 122, 141)
        $placeholder.Left = 15; $placeholder.Top = 14; $placeholder.BackColor = $answerBox.BackColor; $placeholder.Cursor = [System.Windows.Forms.Cursors]::IBeam
        $placeholder.Add_Click({ $answerBox.Focus() })
        $answerBox.Add_TextChanged({
            $placeholder.Visible = [string]::IsNullOrWhiteSpace($answerBox.Text)
            $state.last_input_at = [DateTime]::UtcNow
            $state.remaining = $timeoutSeconds
        })
        $answerBox.Add_KeyDown({
            param($sender, $eventArgs)
            $state.last_input_at = [DateTime]::UtcNow
            $state.remaining = $timeoutSeconds
            if ($eventArgs.Control -and $eventArgs.KeyCode -eq [System.Windows.Forms.Keys]::Enter) {
                $answerBox.SelectedText = [Environment]::NewLine
                $eventArgs.SuppressKeyPress = $true
                $eventArgs.Handled = $true
            }
            elseif (-not $eventArgs.Control -and -not $eventArgs.Shift -and $eventArgs.KeyCode -eq [System.Windows.Forms.Keys]::Enter) {
                $submitButton.PerformClick()
                $eventArgs.SuppressKeyPress = $true
                $eventArgs.Handled = $true
            }
        })
        $answerBoxPanel.Controls.Add($placeholder)

        $countdown = New-Object System.Windows.Forms.Label
        $countdownTemplate = Convert-UiText "5L2g5LiN55CG5oiR5Lmf5rKS6Zec5L+C77yb5aaC5p6c5q2j5Zyo6Ly45YWl77yM5oiR5pyD562J5L2g44CCDQrmiJHmnIPlnKgge259IOenkuW+jOe5vOe6jOaDs+i+puazleOAgg=="
        $countdown.Text = $countdownTemplate.Replace("{n}", [string]$timeoutSeconds)
        $countdown.Font = New-Object System.Drawing.Font($uiFontName, 9); $countdown.ForeColor = $muted; $countdown.AutoSize = $false
        $countdown.Left = 38; $countdown.Top = 611; $countdown.Width = 360; $countdown.Height = 58
        $form.Controls.Add($countdown)

        $submitButton = New-Object System.Windows.Forms.Button
        $submitButton.Text = Convert-UiText "5Lqk57Wm5L2g5LqG"; $submitButton.Font = New-Object System.Drawing.Font($uiFontName, 10, [System.Drawing.FontStyle]::Bold)
        $submitButton.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat; $submitButton.FlatAppearance.BorderSize = 0
        $submitButton.UseVisualStyleBackColor = $false
        $submitButton.FlatAppearance.MouseOverBackColor = [System.Drawing.Color]::FromArgb(61, 135, 231)
        $submitButton.FlatAppearance.MouseDownBackColor = [System.Drawing.Color]::FromArgb(39, 102, 190)
        $submitButton.BackColor = [System.Drawing.Color]::FromArgb(47, 117, 213); $submitButton.ForeColor = [System.Drawing.Color]::White
        $submitButton.Width = 164; $submitButton.Height = 48; $submitButton.Left = 628; $submitButton.Top = 620
        $submitButton.Add_Click({
            $state.outcome = if ([string]::IsNullOrWhiteSpace($answerBox.Text)) { "done" } else { "submitted" }
            $form.Close()
        })
        $submitButton.Add_MouseEnter({ $submitButton.BackColor = [System.Drawing.Color]::FromArgb(61, 135, 231) })
        $submitButton.Add_MouseLeave({ $submitButton.BackColor = [System.Drawing.Color]::FromArgb(47, 117, 213) })
        & $setRoundedRegion $submitButton 14
        $form.Controls.Add($submitButton)

        $skipButton = New-Object System.Windows.Forms.Button
        $skipButton.Text = if ($fallback -eq "continue_best_effort") {
            Convert-UiText "5L2g6Ieq5bex5oOz6L6m5rOV"
        } else {
            Convert-UiText "54++5Zyo5bmr5LiN5LqG"
        }
        $skipButton.Font = New-Object System.Drawing.Font($uiFontName, 10)
        $skipButton.FlatStyle = [System.Windows.Forms.FlatStyle]::Flat
        $skipButton.FlatAppearance.BorderSize = 0
        $skipButton.BackColor = [System.Drawing.Color]::FromArgb(24, 33, 44)
        $skipButton.ForeColor = $ink
        $skipButton.Width = 164
        $skipButton.Height = 48
        $skipButton.Left = 444
        $skipButton.Top = 620
        $skipButton.Add_Click({
            $state.outcome = "skip"
            $form.Close()
        })
        $skipButton.Add_MouseEnter({ $skipButton.BackColor = [System.Drawing.Color]::FromArgb(34, 46, 60) })
        $skipButton.Add_MouseLeave({ $skipButton.BackColor = [System.Drawing.Color]::FromArgb(24, 33, 44) })
        & $setRoundedRegion $skipButton 14
        $form.Controls.Add($skipButton)

        $timer = New-Object System.Windows.Forms.Timer
        $timer.Interval = 1000
        $timer.Add_Tick({
            if ($null -ne $state.last_input_at) {
                $typingIdleSeconds = ([DateTime]::UtcNow - [DateTime]$state.last_input_at).TotalSeconds
                if ($typingIdleSeconds -lt $typingGraceSeconds) {
                    $countdown.Text = Convert-UiText "5YG15ris5Yiw5L2g5q2j5Zyo6Ly45YWl77yM5YCS5pW45bey6YeN5paw6ZaL5aeL44CC"
                    return
                }
            }
            $state.remaining = [int]$state.remaining - 1
            $countdown.Text = $countdownTemplate.Replace("{n}", [string]$state.remaining)
            if ([int]$state.remaining -le 0) {
                $state.outcome = "timeout"
                $state.timed_out = $true
                $timer.Stop()
                $form.Close()
            }
        })
        $form.Add_Shown({
            & $applyRoundedRegion
            $form.BringToFront()
            $form.Activate()
            $answerBox.Focus()
            $timer.Start()
        })
        [void]$form.ShowDialog()
        $timer.Stop()
        $answer = [string]$answerBox.Text
        $timer.Dispose()
        if ($mascot.Image) { $mascot.Image.Dispose() }
        $form.Dispose()

        Write-BrokerLog "HUMAN_HELP_END id=$RequestId outcome=$($state.outcome) timed_out=$($state.timed_out)"
        Complete-Request $RequestId @{
            ok = $true
            status = "human_response"
            outcome = [string]$state.outcome
            answer = $answer
            timed_out = [bool]$state.timed_out
            mode = $mode
            fallback = $fallback
            execution_context = "active_user"
            message = "Human-help desktop prompt completed."
            retryable = $false
        }
    }
    catch {
        Write-BrokerLog "HUMAN_HELP_ERROR id=$RequestId error=$($_.Exception.Message)"
        Complete-Request $RequestId @{ ok = $false; error = "HUMAN_HELP_UI_ERROR"; message = $_.Exception.Message; retryable = $true }
    }
}

function Handle-Request([string]$ProcessingPath) {
    $requestId = [IO.Path]::GetFileNameWithoutExtension($ProcessingPath)
    try {
        Start-ActivityLogViewer
        $request = [IO.File]::ReadAllText($ProcessingPath, [Text.UTF8Encoding]::new($false)) | ConvertFrom-Json
        if ($request.protocol -ne $protocolVersion -or $request.request_id -ne $requestId) {
            Complete-Request $requestId @{ ok = $false; error = "INTERACTIVE_REQUEST_INVALID"; message = "Invalid interactive broker request envelope."; retryable = $false }
            return
        }
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$request.created_at
        if ($age -lt -30 -or $age -gt $requestTtlSeconds) {
            Complete-Request $requestId @{ ok = $false; error = "INTERACTIVE_REQUEST_EXPIRED"; message = "The interactive execution request expired."; retryable = $true }
            return
        }
        switch ([string]$request.kind) {
            "exec" { Handle-ExecRequest $requestId $request }
            "human_help" { Handle-HumanHelpRequest $requestId $request }
            "computer_use" {
                if (Get-Command Handle-ComputerUseRequest -ErrorAction SilentlyContinue) {
                    Handle-ComputerUseRequest $requestId $request
                }
                else {
                    Complete-Request $requestId @{ ok = $false; error = "COMPUTER_USE_UNAVAILABLE"; message = "Computer Use helper is not installed."; retryable = $true }
                }
            }
            default {
                Complete-Request $requestId @{ ok = $false; error = "INTERACTIVE_REQUEST_INVALID"; message = "Unsupported interactive broker request kind."; retryable = $false }
            }
        }
    }
    catch {
        Complete-Request $requestId @{ ok = $false; error = "INTERACTIVE_BROKER_ERROR"; message = $_.Exception.Message; retryable = $true }
    }
    finally {
        Remove-Item -LiteralPath $ProcessingPath -Force -ErrorAction SilentlyContinue
    }
}

try {
    Write-BrokerLog "BOOT pid=$([Diagnostics.Process]::GetCurrentProcess().Id) queue_exists=$(Test-Path -LiteralPath $queueRoot -PathType Container)"
    if (-not (Test-Path -LiteralPath $queueRoot -PathType Container)) {
        throw "Interactive broker queue is missing: $queueRoot"
    }
    Clear-StaleBrokerArtifacts
    $brokerIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $brokerPrincipal = [Security.Principal.WindowsPrincipal]::new($brokerIdentity)
    $brokerSessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
    $brokerIsAdministrator = $brokerPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Write-BrokerLog "BOOT_IDENTITY user=$($brokerIdentity.Name) sid=$($brokerIdentity.User.Value) session=$brokerSessionId admin=$brokerIsAdministrator userinteractive=$([Environment]::UserInteractive)"
    if ($brokerIdentity.User.Value -eq "S-1-5-19" -or $brokerSessionId -le 0) {
        throw "The interactive broker must run as the signed-in user in an interactive session, not LocalService or Session 0."
    }
    if ($brokerIsAdministrator) {
        throw "The interactive broker must use RunLevel Limited. Generic active_user commands are never allowed to inherit Administrator rights."
    }

    Set-Content -LiteralPath $pidPath -Value ([Diagnostics.Process]::GetCurrentProcess().Id) -Encoding ASCII
    Update-BrokerHeartbeat
    Write-BrokerLog "BOOT_PID_WRITTEN path=$pidPath"
    Write-AtomicJson $statusPath @{
        username = $brokerIdentity.Name
        session_id = $brokerSessionId
        user_interactive = [bool][Environment]::UserInteractive
        elevated = $false
        run_level = "limited"
        pid = [Diagnostics.Process]::GetCurrentProcess().Id
        started_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    }
    Write-BrokerLog "BOOT_STATUS_WRITTEN path=$statusPath"
    Start-WebConsoleBridge
    Start-ActivityLogViewer
    Write-BrokerLog "START pid=$([Diagnostics.Process]::GetCurrentProcess().Id) session=$brokerSessionId user=$($brokerIdentity.Name) runlevel=limited"
}
catch {
    Write-BrokerLog "BOOT_FATAL $($_.Exception.GetType().FullName): $($_.Exception.Message)"
    throw
}
try {
    $nextHeartbeat = [DateTimeOffset]::UtcNow
    while ($true) {
        if (Test-Path -LiteralPath $stopPath) { break }
        if ([DateTimeOffset]::UtcNow -ge $nextHeartbeat) {
            Update-BrokerHeartbeat
            $nextHeartbeat = [DateTimeOffset]::UtcNow.AddSeconds($heartbeatIntervalSeconds)
        }
        foreach ($requestPath in @(Get-ChildItem -LiteralPath $queueRoot -Filter "*.request" -File -ErrorAction SilentlyContinue | Sort-Object CreationTime)) {
            $processingPath = Join-Path $queueRoot "$([IO.Path]::GetFileNameWithoutExtension($requestPath.Name)).processing"
            try { Move-Item -LiteralPath $requestPath.FullName -Destination $processingPath -Force -ErrorAction Stop }
            catch { continue }
            Handle-Request $processingPath
        }
        Start-Sleep -Milliseconds ([Math]::Max(50, [Math]::Min($PollMilliseconds, 2000)))
    }
}
catch {
    Write-BrokerLog "FATAL $($_.Exception.GetType().FullName): $($_.Exception.Message)"
    throw
}
finally {
    Write-BrokerLog "STOP pid=$([Diagnostics.Process]::GetCurrentProcess().Id)"
    Stop-WebConsoleBridge
    Remove-Item -LiteralPath $stopPath,$pidPath,$heartbeatPath,$statusPath -Force -ErrorAction SilentlyContinue
}
