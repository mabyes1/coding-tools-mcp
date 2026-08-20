[CmdletBinding()]
param(
    [switch]$Rollback,
    [switch]$ValidateOnly,
    [switch]$SkipBrokerRefresh,
    [ValidateRange(0, 30)]
    [int]$StartDelaySeconds = 3
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
$interactiveQueueRoot = Join-Path $serviceRoot "interactive-requests"
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
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    Assert-Path $csc "C# compiler"
    $windowsPowerShellForBuild = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $automationRef = (& $windowsPowerShellForBuild -NoLogo -NoProfile -NonInteractive -Command "[System.Management.Automation.PowerShell].Assembly.Location").Trim()
    Assert-Path $automationRef "Windows PowerShell automation assembly"
    $brokerLauncherSource = Join-Path $PSScriptRoot "ElevatedBrokerLauncher.cs"
    Assert-Path $brokerLauncherSource "Elevated broker launcher source"
    $brokerLauncherExe = Join-Path $serviceRoot "elevated-broker-launcher.exe"

    # The scheduled elevated broker runs from elevated-broker-launcher.exe.
    # Stop both the broker and its launcher before recompiling in place; Windows
    # otherwise keeps the executable locked and csc cannot replace it.
    $installedBrokerManager = Join-Path $serviceRoot "manage-elevated-broker.ps1"
    if (Test-Path -LiteralPath $installedBrokerManager -PathType Leaf) {
        & $windowsPowerShellForBuild -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
            -File $installedBrokerManager -Action Stop 2>$null | Out-Null
    }
    Stop-ScheduledTask -TaskName "WebGPT-Elevated-Broker" -ErrorAction SilentlyContinue
    Get-Process -Name "elevated-broker-launcher" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    $unlockDeadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
    while ((Get-Process -Name "elevated-broker-launcher" -ErrorAction SilentlyContinue) -and
           [DateTimeOffset]::UtcNow -lt $unlockDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (Get-Process -Name "elevated-broker-launcher" -ErrorAction SilentlyContinue) {
        throw "Could not stop the existing elevated broker launcher before update."
    }

    & $csc /nologo /target:winexe /optimize+ /out:$brokerLauncherExe `
        /reference:$automationRef `
        $brokerLauncherSource
    if ($LASTEXITCODE -ne 0) { throw "Could not build the windowless elevated broker launcher." }
    $brokerPath = Join-Path $serviceRoot "elevated-broker.ps1"
    $webrootSourceScript = Join-Path "D:\coding-tools-mcp" "phoneMonitor\scripts\sync-installed-webroot.ps1"
    if (Test-Path -LiteralPath $webrootSourceScript -PathType Leaf) {
        $webrootHash = Get-Sha256Hex $webrootSourceScript
        $brokerText = [IO.File]::ReadAllText($brokerPath, [Text.UTF8Encoding]::new($false))
        $brokerText = $brokerText.Replace("REPLACE_WITH_SYNC_WEBROOT_SHA256", $webrootHash)
        [IO.File]::WriteAllText($brokerPath, $brokerText, [Text.UTF8Encoding]::new($false))
    }
    $updateSourceScript = Join-Path $PSScriptRoot "update-private-mcp.ps1"
    $updateHash = Get-Sha256Hex $updateSourceScript
    $brokerText = [IO.File]::ReadAllText($brokerPath, [Text.UTF8Encoding]::new($false))
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

    New-Item -ItemType Directory -Path $interactiveQueueRoot -Force | Out-Null
    $activityLogViewerPidPath = Join-Path $interactiveQueueRoot "activity-log-viewer.pid"
    if (Test-Path -LiteralPath $activityLogViewerPidPath -PathType Leaf) {
        try {
            $activityLogViewerPid = [int]([IO.File]::ReadAllText($activityLogViewerPidPath).Trim())
            if ($activityLogViewerPid -gt 0) {
                Stop-Process -Id $activityLogViewerPid -Force -ErrorAction SilentlyContinue
                try { Wait-Process -Id $activityLogViewerPid -Timeout 5 -ErrorAction SilentlyContinue } catch { }
            }
        }
        catch { }
        Remove-Item -LiteralPath $activityLogViewerPidPath -Force -ErrorAction SilentlyContinue
    }
    foreach ($brokerFile in @("interactive-broker.ps1", "manage-interactive-broker.ps1", "install-interactive-broker.ps1")) {
        $source = Join-Path $PSScriptRoot $brokerFile
        Assert-Path $source "Interactive broker source"
        Copy-Item -LiteralPath $source -Destination (Join-Path $serviceRoot $brokerFile) -Force
    }
    $interactiveBrokerManager = Join-Path $serviceRoot "manage-interactive-broker.ps1"
    if (Test-Path -LiteralPath $interactiveBrokerManager -PathType Leaf) {
        & $windowsPowerShell -NoLogo -NoProfile -ExecutionPolicy Bypass `
            -File $interactiveBrokerManager -Action Stop 2>$null | Out-Null
    }
    Stop-ScheduledTask -TaskName "WebGPT-Interactive-Broker" -ErrorAction SilentlyContinue
    Get-Process -Name "interactive-broker-launcher" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    $interactiveUnlockDeadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
    while ((Get-Process -Name "interactive-broker-launcher" -ErrorAction SilentlyContinue) -and
           [DateTimeOffset]::UtcNow -lt $interactiveUnlockDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (Get-Process -Name "interactive-broker-launcher" -ErrorAction SilentlyContinue) {
        throw "Could not stop the existing interactive broker launcher before update."
    }

    $interactiveBrokerLauncherSource = Join-Path $PSScriptRoot "InteractiveBrokerLauncher.cs"
    Assert-Path $interactiveBrokerLauncherSource "Interactive broker launcher source"
    $interactiveBrokerLauncherExe = Join-Path $serviceRoot "interactive-broker-launcher.exe"
    & $csc /nologo /target:winexe /optimize+ /out:$interactiveBrokerLauncherExe `
        /reference:$automationRef `
        $interactiveBrokerLauncherSource
    if ($LASTEXITCODE -ne 0) { throw "Could not build the windowless interactive broker launcher." }
    $computerUseSource = Join-Path $PSScriptRoot "ComputerUseHelper.cs"
    Assert-Path $computerUseSource "Computer Use helper source"
    $computerUseExe = Join-Path $serviceRoot "computer-use-helper.exe"
    & $csc /nologo /target:exe /optimize+ /out:$computerUseExe `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\UIAutomationClient.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\UIAutomationTypes.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\WindowsBase.dll" `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll $computerUseSource
    if ($LASTEXITCODE -ne 0) { throw "Could not build Computer Use helper." }
    $computerUseOverlaySource = Join-Path $PSScriptRoot "ComputerUseOverlay.cs"
    Assert-Path $computerUseOverlaySource "Computer Use overlay source"
    $computerUseOverlayExe = Join-Path $serviceRoot "computer-use-overlay.exe"
    $computerUseOverlayPidPath = Join-Path $interactiveQueueRoot "computer-use-overlay.pid"
    if (Test-Path -LiteralPath $computerUseOverlayPidPath -PathType Leaf) {
        try {
            $computerUseOverlayPid = [int]([IO.File]::ReadAllText($computerUseOverlayPidPath).Trim())
            if ($computerUseOverlayPid -gt 0) {
                Stop-Process -Id $computerUseOverlayPid -Force -ErrorAction SilentlyContinue
                try { Wait-Process -Id $computerUseOverlayPid -Timeout 5 -ErrorAction SilentlyContinue } catch { }
            }
        }
        catch { }
        Remove-Item -LiteralPath $computerUseOverlayPidPath -Force -ErrorAction SilentlyContinue
    }
    Get-Process -Name "computer-use-overlay" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
    & $csc /nologo /target:winexe /optimize+ /out:$computerUseOverlayExe `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll $computerUseOverlaySource
    if ($LASTEXITCODE -ne 0) { throw "Could not build Computer Use overlay." }
    $activityLogViewerSource = Join-Path $PSScriptRoot "ActivityLogViewer.cs"
    Assert-Path $activityLogViewerSource "Activity Log viewer source"
    $activityLogViewerExe = Join-Path $serviceRoot "activity-log-viewer.exe"
    & $csc /nologo /target:winexe /optimize+ /out:$activityLogViewerExe `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll $activityLogViewerSource
    if ($LASTEXITCODE -ne 0) { throw "Could not build Activity Log viewer." }
    $mascotAssetSource = Join-Path $PSScriptRoot "assets"
    if (Test-Path -LiteralPath $mascotAssetSource -PathType Container) {
        Copy-Item -LiteralPath $mascotAssetSource -Destination (Join-Path $serviceRoot "assets") -Recurse -Force
    }
    & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $interactiveBrokerManager -Action Install
    if ($LASTEXITCODE -ne 0) { throw "Could not install the non-elevated interactive broker task." }
    & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $interactiveBrokerManager -Action Start
    if ($LASTEXITCODE -ne 0) { throw "Could not start the non-elevated interactive broker." }
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $ValidateOnly -and -not $isAdmin) {
    throw "Run this update or rollback as Administrator."
}

Assert-Path $serverPython "Service Python"

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
        $validationCsc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
        Assert-Path $validationCsc "C# compiler"
        $validationWindowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
        $validationAutomationRef = (& $validationWindowsPowerShell -NoLogo -NoProfile -NonInteractive -Command "[System.Management.Automation.PowerShell].Assembly.Location").Trim()
        Assert-Path $validationAutomationRef "Windows PowerShell automation assembly"
        $validationBrokerLauncher = Join-Path $validationRoot "elevated-broker-launcher.exe"
        & $validationCsc /nologo /target:winexe /optimize+ /out:$validationBrokerLauncher `
            /reference:$validationAutomationRef `
            (Join-Path $PSScriptRoot "ElevatedBrokerLauncher.cs")
        if ($LASTEXITCODE -ne 0) { throw "Elevated broker launcher validation build failed." }
        & $validationBrokerLauncher --self-test
        if ($LASTEXITCODE -ne 0) { throw "Elevated broker launcher runtime self-test failed." }
        $validationInteractiveBrokerLauncher = Join-Path $validationRoot "interactive-broker-launcher.exe"
        & $validationCsc /nologo /target:winexe /optimize+ /out:$validationInteractiveBrokerLauncher `
            /reference:$validationAutomationRef `
            (Join-Path $PSScriptRoot "InteractiveBrokerLauncher.cs")
        if ($LASTEXITCODE -ne 0) { throw "Interactive broker launcher validation build failed." }
        & $validationInteractiveBrokerLauncher --self-test
        if ($LASTEXITCODE -ne 0) { throw "Interactive broker launcher runtime self-test failed." }
        $validationComputerUse = Join-Path $validationRoot "computer-use-helper.exe"
        & $validationCsc /nologo /target:exe /optimize+ /out:$validationComputerUse `
            /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll" `
            /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\UIAutomationClient.dll" `
            /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\UIAutomationTypes.dll" `
            /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\WindowsBase.dll" `
            /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $PSScriptRoot "ComputerUseHelper.cs")
        if ($LASTEXITCODE -ne 0) { throw "Computer Use helper validation build failed." }
        $validationOverlay = Join-Path $validationRoot "computer-use-overlay.exe"
        & $validationCsc /nologo /target:winexe /optimize+ /out:$validationOverlay `
            /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $PSScriptRoot "ComputerUseOverlay.cs")
        if ($LASTEXITCODE -ne 0) { throw "Computer Use overlay validation build failed." }
        $validationActivityLogViewer = Join-Path $validationRoot "activity-log-viewer.exe"
        & $validationCsc /nologo /target:winexe /optimize+ /out:$validationActivityLogViewer `
            /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $PSScriptRoot "ActivityLogViewer.cs")
        if ($LASTEXITCODE -ne 0) { throw "Activity Log viewer validation build failed." }
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
    # From this point onward the installed app has been replaced. Mark the
    # swap before refreshing brokers so any broker-install/start failure rolls
    # the app/runner back instead of leaving a half-updated service behind.
    $swapped = $true
    if (-not $SkipBrokerRefresh) {
        Install-BrokerFiles
    }
    & icacls.exe $appPath /grant "${localServiceSid}:(OI)(CI)RX" /C | Out-Null
    & icacls.exe $runnerPath /grant "${localServiceSid}:RX" /C | Out-Null
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
