[CmdletBinding()]
param(
    [switch]$Rollback,
    [switch]$ValidateOnly,
    [switch]$SkipBrokerRefresh,
    [ValidateRange(0, 30)]
    [int]$StartDelaySeconds = 0
)

$ErrorActionPreference = "Stop"

function Get-Sha256Hex([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "") }
        finally { $sha256.Dispose() }
    }
    finally { $stream.Dispose() }
}

$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$repoRoot = Split-Path -Parent $PSScriptRoot
$privateSource = Join-Path $repoRoot "private\coding_tools_mcp"
$sourceRunner = Join-Path $PSScriptRoot "run-mcp-service.ps1"
$sourceValidator = Join-Path $PSScriptRoot "validate-private-source.py"
$workspaceRoot = Split-Path -Parent $repoRoot
$serverPython = Join-Path $serviceRoot "venv\Scripts\python.exe"
$appPath = Join-Path $serviceRoot "app"
$runnerPath = Join-Path $serviceRoot "run-mcp-service.ps1"
$releaseRoot = Join-Path $serviceRoot "releases"
$elevatedQueueRoot = Join-Path $serviceRoot "elevated-requests"
$localServiceSid = "*S-1-5-19"

function Assert-Path([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Description is missing: $Path"
    }
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
    Copy-PackageToStage (Join-Path $BundlePath "app\coding_tools_mcp") (Join-Path $BundlePath "run-mcp-service.ps1") $StageRoot
}

function Install-BrokerFiles {
    New-Item -ItemType Directory -Path $elevatedQueueRoot -Force | Out-Null
    foreach ($brokerFile in @("elevated-broker.ps1", "request-elevated-action.ps1", "manage-elevated-broker.ps1")) {
        $source = Join-Path $PSScriptRoot $brokerFile
        Assert-Path $source "Elevated broker source"
        Copy-Item -LiteralPath $source -Destination (Join-Path $serviceRoot $brokerFile) -Force
    }
    $brokerPath = Join-Path $serviceRoot "elevated-broker.ps1"
    $webrootSourceScript = Join-Path "D:\coding-tools-mcp" "phoneMonitor\scripts\sync-installed-webroot.ps1"
    if (Test-Path -LiteralPath $webrootSourceScript -PathType Leaf) {
        $webrootHash = Get-Sha256Hex $webrootSourceScript
        $brokerText = Get-Content -LiteralPath $brokerPath -Raw
        $brokerText = $brokerText.Replace("REPLACE_WITH_SYNC_WEBROOT_SHA256", $webrootHash)
        [IO.File]::WriteAllText($brokerPath, $brokerText, [Text.UTF8Encoding]::new($false))
    }
    $updateSourceScript = Join-Path $PSScriptRoot "update-private-mcp.ps1"
    $updateHash = Get-Sha256Hex $updateSourceScript
    $brokerText = Get-Content -LiteralPath $brokerPath -Raw
    $brokerText = $brokerText.Replace("REPLACE_WITH_UPDATE_PRIVATE_MCP_SHA256", $updateHash)
    [IO.File]::WriteAllText($brokerPath, $brokerText, [Text.UTF8Encoding]::new($false))
    & icacls.exe $elevatedQueueRoot /inheritance:r /grant:r `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" `
        "$([Security.Principal.WindowsIdentity]::GetCurrent().Name):(OI)(CI)M" `
        "${localServiceSid}:(OI)(CI)M" /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not secure the elevated broker queue." }
    foreach ($brokerFile in @("elevated-broker.ps1", "request-elevated-action.ps1", "manage-elevated-broker.ps1")) {
        $installedBrokerFile = Join-Path $serviceRoot $brokerFile
        & icacls.exe $installedBrokerFile /grant:r "${localServiceSid}:RX" /C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not grant LocalService read access to $installedBrokerFile." }
    }

    $brokerManager = Join-Path $serviceRoot "manage-elevated-broker.ps1"
    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $brokerManager -Action Install
    if ($LASTEXITCODE -ne 0) { throw "Could not install the interactive elevated broker task." }
    & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $brokerManager -Action Start
    if ($LASTEXITCODE -ne 0) { throw "Could not start the interactive elevated broker." }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $ValidateOnly -and -not $isAdmin) {
    throw "Run this update or rollback as Administrator."
}

Assert-Path $serverPython "Service Python"

if ($StartDelaySeconds -gt 0) {
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
        Test-SourceBehavior (Join-Path $stageRoot "app")
    }
    if ($Rollback) {
        Write-Host "Rolling back private MCP to $bundlePath (version $expectedVersion)..."
    }
    else {
        Write-Host "Updating private MCP to version $expectedVersion..."
    }

    Stop-PrivateServices
    $servicesStopped = $true
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $newBackup = Join-Path $releaseRoot ("backup-" + $timestamp)
    New-Item -ItemType Directory -Path $newBackup -Force | Out-Null
    $oldAppPath = Join-Path $newBackup "app"
    $oldRunnerPath = Join-Path $newBackup "run-mcp-service.ps1"
    Move-Item -LiteralPath $appPath -Destination $oldAppPath
    Move-Item -LiteralPath $runnerPath -Destination $oldRunnerPath
    Move-Item -LiteralPath (Join-Path $stageRoot "app") -Destination $appPath
    Copy-Item -LiteralPath (Join-Path $stageRoot "run-mcp-service.ps1") -Destination $runnerPath -Force
    if (-not $SkipBrokerRefresh) {
        Install-BrokerFiles
    }
    & icacls.exe $appPath /grant "${localServiceSid}:(OI)(CI)RX" /C | Out-Null
    & icacls.exe $runnerPath /grant "${localServiceSid}:RX" /C | Out-Null
    $swapped = $true

    $health = Start-PrivateServices $expectedVersion
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
