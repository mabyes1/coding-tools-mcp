[CmdletBinding()]
param(
    [int]$PollMilliseconds = 250
)

$ErrorActionPreference = "Stop"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$queueRoot = Join-Path $serviceRoot "elevated-requests"
$pidPath = Join-Path $queueRoot "broker.pid"
$stopPath = Join-Path $queueRoot "broker.stop"
$protocolVersion = 1
$requestTtlSeconds = 900

# This is deliberately a fixed action map.  Do not turn this into a generic
# "run whatever PowerShell asks for" broker.
$allowedAction = [ordered]@{
    "sync-installed-webroot" = @{
        ScriptPath = "D:\coding-tools-mcp\phoneMonitor\scripts\sync-installed-webroot.ps1"
        ExpectedSha256 = "7D53D02F36E431BB2D9D249771510BE0423941E966369AB027A20766C139AABD"
        Destination = "C:\Program Files\VibeDeck\wwwroot"
        Description = "Sync the approved VibeDeck web root from the private workspace"
    }
}

function Write-AtomicJson([string]$Path, [hashtable]$Payload) {
    $temporary = "$Path.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, ($Payload | ConvertTo-Json -Compress -Depth 8), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Show-Consent([string]$Title, [string]$Message) {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $result = [System.Windows.Forms.MessageBox]::Show(
            $Message,
            $Title,
            [System.Windows.Forms.MessageBoxButtons]::YesNo,
            [System.Windows.Forms.MessageBoxIcon]::Warning,
            [System.Windows.Forms.MessageBoxDefaultButton]::Button2,
            [System.Windows.Forms.MessageBoxOptions]::ServiceNotification
        )
        return $result -eq [System.Windows.Forms.DialogResult]::Yes
    }
    catch {
        return $false
    }
}

function Complete-Request([string]$RequestId, [hashtable]$Response) {
    $Response.protocol = $protocolVersion
    $Response.request_id = $RequestId
    Write-AtomicJson (Join-Path $queueRoot "$RequestId.response") $Response
}

function Handle-Request([string]$ProcessingPath) {
    $requestId = [IO.Path]::GetFileNameWithoutExtension($ProcessingPath)
    try {
        $request = Get-Content -LiteralPath $ProcessingPath -Raw | ConvertFrom-Json
        if ($request.protocol -ne $protocolVersion -or $request.request_id -ne $requestId) {
            Complete-Request $requestId @{ ok = $false; error = "ELEVATION_REQUEST_INVALID"; message = "Invalid broker request envelope." }
            return
        }
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$request.created_at
        if ($age -lt -30 -or $age -gt $requestTtlSeconds) {
            Complete-Request $requestId @{ ok = $false; error = "ELEVATION_REQUEST_EXPIRED"; message = "The elevation request expired before user approval."; retryable = $true }
            return
        }
        $actionName = [string]$request.action
        if (-not $allowedAction.Contains($actionName)) {
            Complete-Request $requestId @{ ok = $false; error = "ELEVATED_ACTION_NOT_ALLOWED"; message = "This action is not registered in the broker." }
            return
        }
        $action = $allowedAction[$actionName]
        if (-not (Test-Path -LiteralPath $action.ScriptPath -PathType Leaf)) {
            Complete-Request $requestId @{ ok = $false; error = "ELEVATED_SCRIPT_NOT_FOUND"; message = "The approved action script is missing."; retryable = $false }
            return
        }
        $actualHash = (Get-FileHash -LiteralPath $action.ScriptPath -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($action.ExpectedSha256 -notmatch '^[A-Fa-f0-9]{64}$' -or $actualHash -ne $action.ExpectedSha256.ToUpperInvariant()) {
            Complete-Request $requestId @{ ok = $false; error = "ELEVATED_SCRIPT_HASH_MISMATCH"; message = "The approved action script hash does not match the broker allowlist."; retryable = $false }
            return
        }
        $message = @"
Coding Tools MCP requests administrator approval.

Action: $actionName
Target: $($action.Destination)
Script: $($action.ScriptPath)

Allow this one action?
"@
        if (-not (Show-Consent "WebGPT MCP — administrator approval" $message)) {
            Complete-Request $requestId @{ ok = $false; error = "UAC_USER_DENIED"; message = "Administrator approval was denied or no interactive desktop was available."; retryable = $true }
            return
        }
        $shell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
        $process = Start-Process -FilePath $shell -Verb RunAs -Wait -PassThru -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $action.ScriptPath
        )
        $ok = $process.ExitCode -eq 0
        Complete-Request $requestId @{
            ok = $ok
            exit_code = $process.ExitCode
            message = if ($ok) { "Elevated action completed." } else { "Elevated action exited with code $($process.ExitCode)." }
            error = if ($ok) { $null } else { "ELEVATED_ACTION_FAILED" }
            retryable = $false
        }
    }
    catch {
        Complete-Request $requestId @{ ok = $false; error = "ELEVATION_BROKER_ERROR"; message = $_.Exception.Message; retryable = $true }
    }
    finally {
        Remove-Item -LiteralPath $ProcessingPath -Force -ErrorAction SilentlyContinue
    }
}

if (-not (Test-Path -LiteralPath $queueRoot -PathType Container)) {
    throw "Elevated broker queue is missing: $queueRoot"
}
if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq "S-1-5-19") {
    throw "The elevated broker must run in the signed-in user's session, not as LocalService."
}

Set-Content -LiteralPath $pidPath -Value ([Diagnostics.Process]::GetCurrentProcess().Id) -Encoding ASCII
try {
    while ($true) {
        if (Test-Path -LiteralPath $stopPath) { break }
        foreach ($requestPath in @(Get-ChildItem -LiteralPath $queueRoot -Filter "*.request" -File -ErrorAction SilentlyContinue | Sort-Object CreationTime)) {
            $processingPath = Join-Path $queueRoot "$([IO.Path]::GetFileNameWithoutExtension($requestPath.Name)).processing"
            try {
                Move-Item -LiteralPath $requestPath.FullName -Destination $processingPath -Force -ErrorAction Stop
            }
            catch {
                continue
            }
            Handle-Request $processingPath
        }
        Start-Sleep -Milliseconds ([Math]::Max(100, [Math]::Min($PollMilliseconds, 2000)))
    }
}
finally {
    Remove-Item -LiteralPath $stopPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}
