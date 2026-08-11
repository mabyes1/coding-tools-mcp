[CmdletBinding()]
param(
    [int]$PollMilliseconds = 100
)

$ErrorActionPreference = "Stop"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$queueRoot = Join-Path $serviceRoot "interactive-requests"
$pidPath = Join-Path $queueRoot "broker.pid"
$statusPath = Join-Path $queueRoot "broker.status.json"
$stopPath = Join-Path $queueRoot "broker.stop"
$logPath = Join-Path $queueRoot "broker.log"
$protocolVersion = 1
$requestTtlSeconds = 900
$maxCapturedBytes = 1048576

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

function Complete-Request([string]$RequestId, [hashtable]$Response) {
    $Response.protocol = $protocolVersion
    $Response.request_id = $RequestId
    Write-AtomicJson (Join-Path $queueRoot "$RequestId.response") $Response
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

function Handle-Request([string]$ProcessingPath) {
    $requestId = [IO.Path]::GetFileNameWithoutExtension($ProcessingPath)
    try {
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
        if ([string]$request.kind -ne "exec") {
            Complete-Request $requestId @{ ok = $false; error = "INTERACTIVE_REQUEST_INVALID"; message = "Unsupported interactive broker request kind."; retryable = $false }
            return
        }
        Handle-ExecRequest $requestId $request
    }
    catch {
        Complete-Request $requestId @{ ok = $false; error = "INTERACTIVE_BROKER_ERROR"; message = $_.Exception.Message; retryable = $true }
    }
    finally {
        Remove-Item -LiteralPath $ProcessingPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $queueRoot -PathType Container)) {
    throw "Interactive broker queue is missing: $queueRoot"
}
$brokerIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$brokerPrincipal = [Security.Principal.WindowsPrincipal]::new($brokerIdentity)
$brokerSessionId = [Diagnostics.Process]::GetCurrentProcess().SessionId
if ($brokerIdentity.User.Value -eq "S-1-5-19" -or $brokerSessionId -le 0) {
    throw "The interactive broker must run as the signed-in user in an interactive session, not LocalService or Session 0."
}
if ($brokerPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "The interactive broker must use RunLevel Limited. Generic active_user commands are never allowed to inherit Administrator rights."
}

Set-Content -LiteralPath $pidPath -Value ([Diagnostics.Process]::GetCurrentProcess().Id) -Encoding ASCII
Write-AtomicJson $statusPath @{
    username = $brokerIdentity.Name
    session_id = $brokerSessionId
    elevated = $false
    run_level = "limited"
    pid = [Diagnostics.Process]::GetCurrentProcess().Id
    started_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}
Write-BrokerLog "START pid=$([Diagnostics.Process]::GetCurrentProcess().Id) session=$brokerSessionId user=$($brokerIdentity.Name) runlevel=limited"
try {
    while ($true) {
        if (Test-Path -LiteralPath $stopPath) { break }
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
    Remove-Item -LiteralPath $stopPath,$pidPath,$statusPath -Force -ErrorAction SilentlyContinue
}
