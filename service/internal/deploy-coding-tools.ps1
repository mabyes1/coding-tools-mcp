[CmdletBinding()]
param(
    [switch]$Rollback,
    [switch]$ValidateOnly,
    [switch]$SkipBrokerRefresh,
    [switch]$Force,
    [ValidateRange(0, 30)]
    [int]$StartDelaySeconds = 3
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "deployment-common.ps1")

$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$serviceSourceRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $serviceSourceRoot
$privateSource = Join-Path $repoRoot "private\coding_tools_mcp"
$sourceRunner = Join-Path $serviceSourceRoot "run-mcp-service.ps1"
$sourceValidator = Join-Path $serviceSourceRoot "validate-private-source.py"
$workspaceRoot = Split-Path -Parent $repoRoot
$serverPython = Join-Path $serviceRoot "venv\Scripts\python.exe"
$appPath = Join-Path $serviceRoot "app"
$runnerPath = Join-Path $serviceRoot "run-mcp-service.ps1"
$releaseRoot = Join-Path $serviceRoot "releases"
$elevatedQueueRoot = Join-Path $serviceRoot "elevated-requests"
$interactiveQueueRoot = Join-Path $serviceRoot "interactive-requests"
$localServiceSid = "*S-1-5-19"
$managedServiceFiles = Get-CodingToolsManagedServiceFiles

function Get-PackageVersion([string]$PackageRoot) {
    $init = Join-Path $PackageRoot "__init__.py"
    Assert-DeploymentPath $init "Private package metadata"
    $match = Select-String -LiteralPath $init -Pattern '__version__\s*=\s*["'']([^"'']+)["'']' | Select-Object -First 1
    if (-not $match) { throw "Could not determine package version from $init" }
    return $match.Matches[0].Groups[1].Value
}

function Test-Package([string]$PackageRoot) {
    Assert-DeploymentPath $PackageRoot "Private package"
    Assert-DeploymentPath (Join-Path $PackageRoot "__main__.py") "Private package entrypoint"
    $version = Get-PackageVersion $PackageRoot
    & $serverPython -m compileall -q $PackageRoot
    if ($LASTEXITCODE -ne 0) { throw "Private package compile check failed." }
    return $version
}

function Write-BuildIdentity([string]$PackageRoot, [string]$PackageVersion) {
    $git = Get-Command git.exe -ErrorAction Stop | Select-Object -ExpandProperty Source
    $gitSha = (& $git -C $repoRoot rev-parse --short=12 HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($gitSha)) {
        throw "Could not determine the coding-tools Git commit for build identity."
    }

    & $git -C $repoRoot diff --quiet HEAD -- private service
    $trackedDirty = $LASTEXITCODE -ne 0
    $untracked = @(& $git -C $repoRoot ls-files --others --exclude-standard -- private service)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not determine untracked coding-tools source files for build identity."
    }
    $dirty = $trackedDirty -or $untracked.Count -gt 0
    $displayVersion = if ($dirty) { "$PackageVersion-dev+$gitSha" } else { $PackageVersion }
    $identity = [ordered]@{
        package_version = $PackageVersion
        display_version = $displayVersion
        git_sha = $gitSha
        dirty = [bool]$dirty
        build_id = ([DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ") + "-" + $gitSha)
    }
    [IO.File]::WriteAllText(
        (Join-Path $PackageRoot "build-identity.json"),
        ($identity | ConvertTo-Json -Depth 4),
        [Text.UTF8Encoding]::new($false)
    )
    return $identity
}

function Test-SourceBehavior([string]$PackageParent) {
    Assert-DeploymentPath $sourceValidator "Private source validator"
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $PackageParent
        & $serverPython $sourceValidator --package-parent $PackageParent --workspace $workspaceRoot
        if ($LASTEXITCODE -ne 0) { throw "Private source behavioral validation failed." }
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
    }
}

function Assert-NoActiveMcpWork {
    if ($Force) { return }
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:8766/healthz" -TimeoutSec 3
    }
    catch {
        return
    }
    $running = [int]($health.execution.running)
    $inFlight = [int]($health.http_sessions.in_flight)
    if ($running -gt 0 -or $inFlight -gt 0) {
        throw "coding-tools is still busy (execution.running=$running, http_sessions.in_flight=$inFlight). Finish/stop that work first, or rerun this script with -Force."
    }
}

function Notify-ToolListChanged {
    try {
        Invoke-RestMethod -Method Post "http://127.0.0.1:8766/notify-tools-changed" -TimeoutSec 2 | Out-Null
        Start-Sleep -Milliseconds 250
    }
    catch {
        Write-Verbose "Tool-list notification was unavailable: $($_.Exception.Message)"
    }
}

function Trim-ReleaseBackups([int]$Keep = 20) {
    if (-not (Test-Path -LiteralPath $releaseRoot -PathType Container)) { return }
    @(Get-ChildItem -LiteralPath $releaseRoot -Directory -Filter "backup-*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $Keep) |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
}

function Test-InstalledComputerUseE2E {
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $appPath
        $code = @'
from coding_tools_mcp.interactive_exec import request_computer_use
r = request_computer_use(action="list_windows", include_screenshot=False, include_text=False, timeout_seconds=20)
assert r.get("ok") and r.get("action") == "list_windows", r
print("COMPUTER_USE_E2E_OK")
'@
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(35)
        do {
            & $serverPython -c $code
            if ($LASTEXITCODE -eq 0) { return }
            Start-Sleep -Milliseconds 500
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        throw "Computer Use E2E smoke test did not complete through queue -> broker -> helper -> response."
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
    }
}

function Test-InstalledInteractiveExecE2E {
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $appPath
        $code = @'
from coding_tools_mcp.interactive_exec import request_interactive_exec
r = request_interactive_exec(
    cmd="if (",
    cwd=r"D:\coding-tools-mcp",
    env_overrides={},
    env_policy={"inherit":"core","core_names":[],"include_only":[],"exclude":[]},
    timeout_seconds=10,
)
assert r.get("exit_code") == 1, r
assert str(r.get("stderr", "")).strip(), r
print("INTERACTIVE_EXEC_PARSE_E2E_OK")
'@
        # Broker artifacts are deliberately restarted during deployment. The
        # scheduled task may report Started a few seconds before its heartbeat
        # and queue consumer are actually ready. Treat that startup window the
        # same way as Computer Use instead of rolling a healthy MCP back on the
        # first transient probe failure.
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(35)
        do {
            & $serverPython -c $code
            if ($LASTEXITCODE -eq 0) { return }
            Start-Sleep -Milliseconds 500
        } while ([DateTimeOffset]::UtcNow -lt $deadline)
        throw "Interactive exec syntax-error regression test did not complete after broker readiness retries."
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
    }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $ValidateOnly -and -not $isAdmin) {
    throw "Run this update or rollback as Administrator."
}

Assert-DeploymentPath $serverPython "Service Python"

if (-not $ValidateOnly) {
    Assert-NoActiveMcpWork
}

if (-not $ValidateOnly -and $StartDelaySeconds -gt 0) {
    Write-Host "Waiting $StartDelaySeconds second(s) before restarting MCP so pending connector responses can drain..."
    Start-Sleep -Seconds $StartDelaySeconds
}

if ($ValidateOnly) {
    Assert-DeploymentPath $privateSource "Private source"
    $validationRoot = Join-Path ([IO.Path]::GetTempPath()) ("webgpt-mcp-validate-" + [Guid]::NewGuid().ToString("N"))
    try {
        $stageApp = Join-Path $validationRoot "app"
        New-Item -ItemType Directory -Path $validationRoot -Force | Out-Null
        New-Item -ItemType Directory -Path $stageApp -Force | Out-Null
        Copy-Item -LiteralPath $privateSource -Destination (Join-Path $stageApp "coding_tools_mcp") -Recurse -Force
        $version = Test-Package (Join-Path $stageApp "coding_tools_mcp")
        Test-SourceBehavior $stageApp
        $validationService = New-CodingToolsBrokerArtifactStage `
            $validationRoot `
            $serviceSourceRoot `
            (Join-Path $validationRoot "app\coding_tools_mcp\computer-use-actions.json")
        $validationBrokerLauncher = Join-Path $validationService "elevated-broker-launcher.exe"
        & $validationBrokerLauncher --self-test
        if ($LASTEXITCODE -ne 0) { throw "Elevated broker launcher runtime self-test failed." }
        $validationInteractiveBrokerLauncher = Join-Path $validationService "interactive-broker-launcher.exe"
        & $validationInteractiveBrokerLauncher --self-test
        if ($LASTEXITCODE -ne 0) { throw "Interactive broker launcher runtime self-test failed." }
        Write-Host "PRIVATE_MCP_VALIDATE_OK version=$version"
        exit 0
    }
    finally {
        Remove-Item -LiteralPath $validationRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Assert-DeploymentPath $serviceRoot "Service root"
Assert-DeploymentPath $appPath "Installed private app"
Assert-DeploymentPath $runnerPath "Installed service runner"
New-Item -ItemType Directory -Path $releaseRoot -Force | Out-Null

$stageRoot = Join-Path $serviceRoot ("update-stage-" + [Guid]::NewGuid().ToString("N"))
$bundlePath = $null
$oldAppPath = $null
$oldRunnerPath = $null
$oldServicePath = $null
$stagedServicePath = $null
$swapped = $false
$servicesStopped = $false
$servicesWereRunning = ((Get-Service WebGPTCodingToolsMCP).Status -eq "Running")

try {
    if ($Rollback) {
        $bundlePath = Get-ChildItem -LiteralPath $releaseRoot -Directory -Filter "backup-*" |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
        if (-not $bundlePath) { throw "No private MCP backup is available for rollback." }
        Restore-CodingToolsBundleToStage $bundlePath $stageRoot
    }
    else {
        Assert-DeploymentPath $privateSource "Private source"
        Copy-CodingToolsPackageToStage $privateSource $sourceRunner $stageRoot
    }

    $expectedVersion = Test-Package (Join-Path $stageRoot "app\coding_tools_mcp")
    if (-not $Rollback) {
        $buildIdentity = Write-BuildIdentity (Join-Path $stageRoot "app\coding_tools_mcp") $expectedVersion
        Write-Host "Staged build identity: $($buildIdentity.display_version)"
        Test-SourceBehavior (Join-Path $stageRoot "app")
    }
    if (-not $SkipBrokerRefresh) {
        $stagedServicePath = if ($Rollback) {
            Join-Path $stageRoot "service"
        }
        else {
            New-CodingToolsBrokerArtifactStage `
                $stageRoot `
                $serviceSourceRoot `
                (Join-Path $stageRoot "app\coding_tools_mcp\computer-use-actions.json")
        }
        Assert-DeploymentPath $stagedServicePath "Staged broker bundle"
    }
    if ($Rollback) {
        Write-Host "Rolling back private MCP to $bundlePath (version $expectedVersion)..."
    }
    else {
        Write-Host "Updating private MCP to version $expectedVersion..."
    }

    Notify-ToolListChanged
    Stop-CodingToolsPrivateServices
    $servicesStopped = $true
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $newBackup = Join-Path $releaseRoot ("backup-" + $timestamp)
    New-Item -ItemType Directory -Path $newBackup -Force | Out-Null
    $oldAppPath = Join-Path $newBackup "app"
    $oldRunnerPath = Join-Path $newBackup "run-mcp-service.ps1"
    $oldServicePath = Join-Path $newBackup "service"
    Backup-CodingToolsServiceComponents $oldServicePath $serviceRoot $managedServiceFiles
    Move-Item -LiteralPath $appPath -Destination $oldAppPath
    Move-Item -LiteralPath $runnerPath -Destination $oldRunnerPath
    Move-Item -LiteralPath (Join-Path $stageRoot "app") -Destination $appPath
    Copy-Item -LiteralPath (Join-Path $stageRoot "run-mcp-service.ps1") -Destination $runnerPath -Force
    # From this point onward the installed app has been replaced. Mark the
    # swap before refreshing brokers so any broker-install/start failure rolls
    # the app/runner back instead of leaving a half-updated service behind.
    $swapped = $true
    if (-not $SkipBrokerRefresh) {
        Install-CodingToolsBrokerArtifacts `
            $stagedServicePath `
            $serviceRoot `
            $managedServiceFiles `
            $elevatedQueueRoot `
            $interactiveQueueRoot `
            $localServiceSid
    }
    & icacls.exe $appPath /grant "${localServiceSid}:(OI)(CI)RX" /C | Out-Null
    & icacls.exe $runnerPath /grant "${localServiceSid}:RX" /C | Out-Null
    $health = Start-CodingToolsPrivateServices $expectedVersion
    if (-not $SkipBrokerRefresh) {
        Test-InstalledInteractiveExecE2E
        Test-InstalledComputerUseE2E
    }
    Trim-ReleaseBackups 20
    Write-Host "PRIVATE_MCP_UPDATE_OK version=$($health.version) backup=$newBackup"
}
catch {
    $failure = $_
    Write-Warning $failure.Exception.Message
    try {
        Stop-CodingToolsPrivateServices
    } catch { }
    if ($swapped -and $oldAppPath -and (Test-Path -LiteralPath $oldAppPath)) {
        Remove-Item -LiteralPath $appPath -Recurse -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $oldAppPath -Destination $appPath -Force
        if ($oldRunnerPath -and (Test-Path -LiteralPath $oldRunnerPath)) {
            Remove-Item -LiteralPath $runnerPath -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $oldRunnerPath -Destination $runnerPath -Force
        }
        if ($oldServicePath) {
            try {
                Restore-CodingToolsServiceComponents `
                    $oldServicePath `
                    $serviceRoot `
                    $managedServiceFiles `
                    $elevatedQueueRoot `
                    $interactiveQueueRoot `
                    $localServiceSid
            }
            catch { Write-Warning "Service-component rollback failed: $($_.Exception.Message)" }
        }
        try { Start-CodingToolsPrivateServices (Get-PackageVersion (Join-Path $appPath "coding_tools_mcp")) | Out-Null } catch { }
    }
    elseif ($servicesStopped -and $servicesWereRunning) {
        try { Start-CodingToolsPrivateServices (Get-PackageVersion (Join-Path $appPath "coding_tools_mcp")) | Out-Null } catch { }
    }
    throw $failure
}
finally {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
}
