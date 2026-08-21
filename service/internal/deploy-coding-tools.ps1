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
$managedServiceFiles = @(
    "elevated-broker.ps1",
    "manage-elevated-broker.ps1",
    "elevated-broker-launcher.exe",
    "interactive-broker.ps1",
    "manage-interactive-broker.ps1",
    "install-interactive-broker.ps1",
    "interactive-broker-launcher.exe",
    "computer-use-helper.exe",
    "computer-use-overlay.exe",
    "activity-log-viewer.exe",
    "computer-use-actions.json",
    "elevated-actions.manifest.json"
)

function Assert-Path([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Description is missing: $Path"
    }
}

function Build-BrokerArtifacts([string]$StageRoot) {
    $serviceStage = Join-Path $StageRoot "service"
    New-Item -ItemType Directory -Path $serviceStage -Force | Out-Null
    foreach ($file in @(
        "elevated-broker.ps1", "manage-elevated-broker.ps1",
        "interactive-broker.ps1", "manage-interactive-broker.ps1", "install-interactive-broker.ps1"
    )) {
        $source = Join-Path $serviceSourceRoot $file
        Assert-Path $source "Broker source"
        Copy-Item -LiteralPath $source -Destination (Join-Path $serviceStage $file) -Force
    }
    $contractSource = Join-Path $StageRoot "app\coding_tools_mcp\computer-use-actions.json"
    Assert-Path $contractSource "Computer Use action contract"
    Copy-Item -LiteralPath $contractSource -Destination (Join-Path $serviceStage "computer-use-actions.json") -Force

    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    Assert-Path $csc "C# compiler"
    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $automationRef = (& $windowsPowerShell -NoLogo -NoProfile -NonInteractive -Command "[System.Management.Automation.PowerShell].Assembly.Location").Trim()
    Assert-Path $automationRef "Windows PowerShell automation assembly"

    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "elevated-broker-launcher.exe")) `
        /reference:$automationRef (Join-Path $serviceSourceRoot "ElevatedBrokerLauncher.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the elevated broker launcher." }
    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "interactive-broker-launcher.exe")) `
        /reference:$automationRef (Join-Path $serviceSourceRoot "InteractiveBrokerLauncher.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the interactive broker launcher." }
    & $csc /nologo /target:exe /optimize+ ("/out:" + (Join-Path $serviceStage "computer-use-helper.exe")) `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\UIAutomationClient.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\UIAutomationTypes.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\WindowsBase.dll" `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $serviceSourceRoot "ComputerUseHelper.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the Computer Use helper." }
    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "computer-use-overlay.exe")) `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $serviceSourceRoot "ComputerUseOverlay.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the Computer Use overlay." }
    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "activity-log-viewer.exe")) `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $serviceSourceRoot "ActivityLogViewer.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the Activity Log viewer." }

    $assetsSource = Join-Path $serviceSourceRoot "assets"
    if (Test-Path -LiteralPath $assetsSource -PathType Container) {
        Copy-Item -LiteralPath $assetsSource -Destination (Join-Path $serviceStage "assets") -Recurse -Force
    }
    New-ElevatedActionManifest `
        (Join-Path $serviceStage "elevated-broker.ps1") `
        (Join-Path $serviceStage "elevated-actions.manifest.json")
    return $serviceStage
}

function Get-PackageVersion([string]$PackageRoot) {
    $init = Join-Path $PackageRoot "__init__.py"
    Assert-Path $init "Private package metadata"
    $match = Select-String -LiteralPath $init -Pattern '__version__\s*=\s*["'']([^"'']+)["'']' | Select-Object -First 1
    if (-not $match) { throw "Could not determine package version from $init" }
    return $match.Matches[0].Groups[1].Value
}

function Test-Package([string]$PackageRoot) {
    Assert-Path $PackageRoot "Private package"
    Assert-Path (Join-Path $PackageRoot "__main__.py") "Private package entrypoint"
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
    Assert-Path $sourceValidator "Private source validator"
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

function Wait-McpHealth([int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:8766/healthz" -TimeoutSec 3
        } catch {
            $health = $null
        }
    } while ((-not $health) -and (Get-Date) -lt $deadline)
    if (-not $health -or $health.status -ne "ok") {
        throw "MCP health endpoint did not become ready."
    }
    return $health
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

function Stop-PrivateServices {
    foreach ($name in @("WebGPTCloudflareTunnel", "WebGPTCodingToolsMCP")) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($service -and $service.Status -ne "Stopped") {
            Stop-Service -Name $name -Force
        }
    }
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 250
        $running = @(Get-Service -Name WebGPTCloudflareTunnel,WebGPTCodingToolsMCP |
            Where-Object Status -ne "Stopped")
    } while ($running.Count -gt 0 -and (Get-Date) -lt $deadline)
    if ($running.Count -gt 0) { throw "Timed out stopping the private MCP services." }
}

function Start-PrivateServices([string]$ExpectedVersion) {
    Start-Service -Name WebGPTCodingToolsMCP
    $health = Wait-McpHealth
    if ($health.version -ne $ExpectedVersion) {
        throw "MCP started with version $($health.version), expected $ExpectedVersion."
    }
    Start-Service -Name WebGPTCloudflareTunnel
    return $health
}

function Copy-PackageToStage([string]$PackageRoot, [string]$RunnerSource, [string]$StageRoot) {
    New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
    $stageApp = Join-Path $StageRoot "app"
    $packageName = Split-Path -Leaf $PackageRoot
    New-Item -ItemType Directory -Path $stageApp -Force | Out-Null
    Copy-Item -LiteralPath $PackageRoot -Destination (Join-Path $stageApp $packageName) -Recurse -Force
    Copy-Item -LiteralPath $RunnerSource -Destination (Join-Path $StageRoot "run-mcp-service.ps1") -Force
}

function Restore-Bundle([string]$BundlePath, [string]$StageRoot) {
    Assert-Path (Join-Path $BundlePath "app") "Rollback app backup"
    Assert-Path (Join-Path $BundlePath "run-mcp-service.ps1") "Rollback runner backup"
    Assert-Path (Join-Path $BundlePath "service") "Rollback service-component backup"
    Copy-PackageToStage (Join-Path $BundlePath "app\coding_tools_mcp") (Join-Path $BundlePath "run-mcp-service.ps1") $StageRoot
    Copy-Item -LiteralPath (Join-Path $BundlePath "service") -Destination (Join-Path $StageRoot "service") -Recurse -Force
}

function Test-InstalledComputerUseE2E {
    $previousPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $appPath
        $code = @'
from coding_tools_mcp.interactive_exec import request_computer_use
r = request_computer_use(action="list_windows", include_screenshot=False, include_text=False, timeout_seconds=10)
assert r.get("ok") and r.get("action") == "list_windows", r
print("COMPUTER_USE_E2E_OK")
'@
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(20)
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
        & $serverPython -c $code
        if ($LASTEXITCODE -ne 0) {
            throw "Interactive exec syntax-error regression test failed."
        }
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
    }
}

function Stop-BrokerProcesses {
    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    foreach ($pair in @(
        @{ Manager = "manage-elevated-broker.ps1"; Task = "WebGPT-Elevated-Broker" },
        @{ Manager = "manage-interactive-broker.ps1"; Task = "WebGPT-Interactive-Broker" }
    )) {
        $manager = Join-Path $serviceRoot $pair.Manager
        if (Test-Path -LiteralPath $manager -PathType Leaf) {
            & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
                -File $manager -Action Stop 2>$null | Out-Null
        }
        Stop-ScheduledTask -TaskName $pair.Task -ErrorAction SilentlyContinue
    }
    Get-Process -Name "elevated-broker-launcher","interactive-broker-launcher","computer-use-overlay","activity-log-viewer" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

function Set-InstalledBrokerPermissions {
    New-Item -ItemType Directory -Path $elevatedQueueRoot,$interactiveQueueRoot -Force | Out-Null
    $currentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $elevatedQueueRoot /inheritance:r /remove:g "${currentAccount}" /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not remove the signed-in user's write access from the elevated broker queue." }
    & icacls.exe $elevatedQueueRoot /inheritance:r /grant:r `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" `
        "${localServiceSid}:(OI)(CI)M" /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not secure the elevated broker queue." }

    & icacls.exe $interactiveQueueRoot /inheritance:r /grant:r `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" `
        "${currentAccount}:(OI)(CI)M" `
        "${localServiceSid}:(OI)(CI)M" /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not secure the interactive broker queue." }

    foreach ($file in @("elevated-broker.ps1", "elevated-broker-launcher.exe", "manage-elevated-broker.ps1", "elevated-actions.manifest.json")) {
        $path = Join-Path $serviceRoot $file
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        & icacls.exe $path /inheritance:r /grant:r `
            "*S-1-5-18:F" `
            "*S-1-5-32-544:F" `
            "${currentAccount}:RX" `
            "${localServiceSid}:RX" /C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not protect privileged broker file: $path" }
    }
}

function Backup-ServiceComponents([string]$Destination) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($file in $managedServiceFiles) {
        $source = Join-Path $serviceRoot $file
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $Destination $file) -Force
        }
    }
    $assets = Join-Path $serviceRoot "assets"
    if (Test-Path -LiteralPath $assets -PathType Container) {
        Copy-Item -LiteralPath $assets -Destination (Join-Path $Destination "assets") -Recurse -Force
    }
}

function Install-StagedBrokerArtifacts([string]$ServiceStage) {
    Assert-Path $ServiceStage "Staged broker artifacts"
    Stop-BrokerProcesses
    foreach ($file in $managedServiceFiles) {
        $source = Join-Path $ServiceStage $file
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $serviceRoot $file) -Force
        }
    }
    $stagedAssets = Join-Path $ServiceStage "assets"
    if (Test-Path -LiteralPath $stagedAssets -PathType Container) {
        Remove-Item -LiteralPath (Join-Path $serviceRoot "assets") -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath $stagedAssets -Destination (Join-Path $serviceRoot "assets") -Recurse -Force
    }

    Set-InstalledBrokerPermissions

    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    foreach ($managerName in @("manage-elevated-broker.ps1", "manage-interactive-broker.ps1")) {
        $manager = Join-Path $serviceRoot $managerName
        & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Install
        if ($LASTEXITCODE -ne 0) { throw "Could not install broker task through $managerName" }
        & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Start
        if ($LASTEXITCODE -ne 0) { throw "Could not start broker task through $managerName" }
    }
}

function Restore-ServiceComponents([string]$Source) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { return }
    Stop-BrokerProcesses
    foreach ($file in $managedServiceFiles) {
        $destination = Join-Path $serviceRoot $file
        Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        $backup = Join-Path $Source $file
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            Copy-Item -LiteralPath $backup -Destination $destination -Force
        }
    }
    $assetsDestination = Join-Path $serviceRoot "assets"
    Remove-Item -LiteralPath $assetsDestination -Recurse -Force -ErrorAction SilentlyContinue
    $assetsBackup = Join-Path $Source "assets"
    if (Test-Path -LiteralPath $assetsBackup -PathType Container) {
        Copy-Item -LiteralPath $assetsBackup -Destination $assetsDestination -Recurse -Force
    }
    Set-InstalledBrokerPermissions
    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    foreach ($managerName in @("manage-elevated-broker.ps1", "manage-interactive-broker.ps1")) {
        $manager = Join-Path $serviceRoot $managerName
        if (-not (Test-Path -LiteralPath $manager -PathType Leaf)) { continue }
        & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Install 2>$null | Out-Null
        & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Start 2>$null | Out-Null
    }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $ValidateOnly -and -not $isAdmin) {
    throw "Run this update or rollback as Administrator."
}

Assert-Path $serverPython "Service Python"

if (-not $ValidateOnly) {
    Assert-NoActiveMcpWork
}

if (-not $ValidateOnly -and $StartDelaySeconds -gt 0) {
    Write-Host "Waiting $StartDelaySeconds second(s) before restarting MCP so pending connector responses can drain..."
    Start-Sleep -Seconds $StartDelaySeconds
}

if ($ValidateOnly) {
    Assert-Path $privateSource "Private source"
    $validationRoot = Join-Path ([IO.Path]::GetTempPath()) ("webgpt-mcp-validate-" + [Guid]::NewGuid().ToString("N"))
    try {
        $stageApp = Join-Path $validationRoot "app"
        New-Item -ItemType Directory -Path $validationRoot -Force | Out-Null
        New-Item -ItemType Directory -Path $stageApp -Force | Out-Null
        Copy-Item -LiteralPath $privateSource -Destination (Join-Path $stageApp "coding_tools_mcp") -Recurse -Force
        $version = Test-Package (Join-Path $stageApp "coding_tools_mcp")
        Test-SourceBehavior $stageApp
        $validationService = Build-BrokerArtifacts $validationRoot
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

Assert-Path $serviceRoot "Service root"
Assert-Path $appPath "Installed private app"
Assert-Path $runnerPath "Installed service runner"
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
        Restore-Bundle $bundlePath $stageRoot
    }
    else {
        Assert-Path $privateSource "Private source"
        Copy-PackageToStage $privateSource $sourceRunner $stageRoot
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
            Build-BrokerArtifacts $stageRoot
        }
        Assert-Path $stagedServicePath "Staged broker bundle"
    }
    if ($Rollback) {
        Write-Host "Rolling back private MCP to $bundlePath (version $expectedVersion)..."
    }
    else {
        Write-Host "Updating private MCP to version $expectedVersion..."
    }

    Notify-ToolListChanged
    Stop-PrivateServices
    $servicesStopped = $true
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $newBackup = Join-Path $releaseRoot ("backup-" + $timestamp)
    New-Item -ItemType Directory -Path $newBackup -Force | Out-Null
    $oldAppPath = Join-Path $newBackup "app"
    $oldRunnerPath = Join-Path $newBackup "run-mcp-service.ps1"
    $oldServicePath = Join-Path $newBackup "service"
    Backup-ServiceComponents $oldServicePath
    Move-Item -LiteralPath $appPath -Destination $oldAppPath
    Move-Item -LiteralPath $runnerPath -Destination $oldRunnerPath
    Move-Item -LiteralPath (Join-Path $stageRoot "app") -Destination $appPath
    Copy-Item -LiteralPath (Join-Path $stageRoot "run-mcp-service.ps1") -Destination $runnerPath -Force
    # From this point onward the installed app has been replaced. Mark the
    # swap before refreshing brokers so any broker-install/start failure rolls
    # the app/runner back instead of leaving a half-updated service behind.
    $swapped = $true
    if (-not $SkipBrokerRefresh) {
        Install-StagedBrokerArtifacts $stagedServicePath
    }
    & icacls.exe $appPath /grant "${localServiceSid}:(OI)(CI)RX" /C | Out-Null
    & icacls.exe $runnerPath /grant "${localServiceSid}:RX" /C | Out-Null
    $health = Start-PrivateServices $expectedVersion
    if (-not $SkipBrokerRefresh) {
        Test-InstalledComputerUseE2E
        Test-InstalledInteractiveExecE2E
    }
    Trim-ReleaseBackups 20
    Write-Host "PRIVATE_MCP_UPDATE_OK version=$($health.version) backup=$newBackup"
}
catch {
    $failure = $_
    Write-Warning $failure.Exception.Message
    try {
        Stop-PrivateServices
    } catch { }
    if ($swapped -and $oldAppPath -and (Test-Path -LiteralPath $oldAppPath)) {
        Remove-Item -LiteralPath $appPath -Recurse -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $oldAppPath -Destination $appPath -Force
        if ($oldRunnerPath -and (Test-Path -LiteralPath $oldRunnerPath)) {
            Remove-Item -LiteralPath $runnerPath -Force -ErrorAction SilentlyContinue
            Move-Item -LiteralPath $oldRunnerPath -Destination $runnerPath -Force
        }
        if ($oldServicePath) {
            try { Restore-ServiceComponents $oldServicePath } catch { Write-Warning "Service-component rollback failed: $($_.Exception.Message)" }
        }
        try { Start-PrivateServices (Get-PackageVersion (Join-Path $appPath "coding_tools_mcp")) | Out-Null } catch { }
    }
    elseif ($servicesStopped -and $servicesWereRunning) {
        try { Start-PrivateServices (Get-PackageVersion (Join-Path $appPath "coding_tools_mcp")) | Out-Null } catch { }
    }
    throw $failure
}
finally {
    Remove-Item -LiteralPath $stageRoot -Recurse -Force -ErrorAction SilentlyContinue
}
