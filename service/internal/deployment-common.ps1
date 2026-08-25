function Get-Sha256Hex([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "") }
        finally { $sha256.Dispose() }
    }
    finally { $stream.Dispose() }
}

function New-ElevatedActionManifest([string]$BrokerPath, [string]$OutputPath) {
    $brokerText = [IO.File]::ReadAllText($BrokerPath, [Text.UTF8Encoding]::new($false))
    $blocks = [regex]::Matches(
        $brokerText,
        '(?ms)^\s*"(?<name>[^"]+)"\s*=\s*@\{\s*(?<body>.*?)^\s*\}'
    )
    $actions = [ordered]@{}
    foreach ($block in $blocks) {
        $scriptMatch = [regex]::Match($block.Groups['body'].Value, 'ScriptPath\s*=\s*"(?<path>[^"]+)"')
        if (-not $scriptMatch.Success) { continue }
        $scriptPath = $scriptMatch.Groups['path'].Value
        $actions[$block.Groups['name'].Value] = [ordered]@{
            script_path = $scriptPath
            sha256 = if (Test-Path -LiteralPath $scriptPath -PathType Leaf) { Get-Sha256Hex $scriptPath } else { $null }
        }
    }
    if ($actions.Count -lt 1) { throw "Could not derive elevated action hashes from $BrokerPath" }
    $payload = [ordered]@{
        generated_at = [DateTimeOffset]::UtcNow.ToString("o")
        actions = $actions
    }
    [IO.File]::WriteAllText($OutputPath, ($payload | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
}

function Assert-DeploymentPath([string]$Path, [string]$Description) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "$Description is missing: $Path"
    }
}

function Get-CodingToolsManagedServiceFiles {
    return @(
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
        "web-console-bridge.exe",
        "computer-use-actions.json",
        "elevated-actions.manifest.json"
    )
}

function New-CodingToolsBrokerArtifactStage(
    [string]$StageRoot,
    [string]$ServiceSourceRoot,
    [string]$ContractSource
) {
    $serviceStage = Join-Path $StageRoot "service"
    New-Item -ItemType Directory -Path $serviceStage -Force | Out-Null
    foreach ($file in @(
        "elevated-broker.ps1", "manage-elevated-broker.ps1",
        "interactive-broker.ps1", "manage-interactive-broker.ps1", "install-interactive-broker.ps1"
    )) {
        $source = Join-Path $ServiceSourceRoot $file
        Assert-DeploymentPath $source "Broker source"
        Copy-Item -LiteralPath $source -Destination (Join-Path $serviceStage $file) -Force
    }
    Assert-DeploymentPath $ContractSource "Computer Use action contract"
    Copy-Item -LiteralPath $ContractSource -Destination (Join-Path $serviceStage "computer-use-actions.json") -Force

    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    Assert-DeploymentPath $csc "C# compiler"
    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    $automationRef = (& $windowsPowerShell -NoLogo -NoProfile -NonInteractive -Command "[System.Management.Automation.PowerShell].Assembly.Location").Trim()
    Assert-DeploymentPath $automationRef "Windows PowerShell automation assembly"

    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "elevated-broker-launcher.exe")) `
        /reference:$automationRef (Join-Path $ServiceSourceRoot "ElevatedBrokerLauncher.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the elevated broker launcher." }
    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "interactive-broker-launcher.exe")) `
        /reference:$automationRef (Join-Path $ServiceSourceRoot "InteractiveBrokerLauncher.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the interactive broker launcher." }
    & $csc /nologo /target:exe /optimize+ ("/out:" + (Join-Path $serviceStage "computer-use-helper.exe")) `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\UIAutomationClient.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\UIAutomationTypes.dll" `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\WPF\WindowsBase.dll" `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $ServiceSourceRoot "ComputerUseHelper.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the Computer Use helper." }
    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "computer-use-overlay.exe")) `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $ServiceSourceRoot "ComputerUseOverlay.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the Computer Use overlay." }
    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "activity-log-viewer.exe")) `
        /reference:System.Drawing.dll /reference:System.Windows.Forms.dll (Join-Path $ServiceSourceRoot "ActivityLogViewer.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the Activity Log viewer." }
    & $csc /nologo /target:winexe /optimize+ ("/out:" + (Join-Path $serviceStage "web-console-bridge.exe")) `
        /reference:"$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\System.Web.Extensions.dll" `
        (Join-Path $ServiceSourceRoot "WebConsoleBridge.cs")
    if ($LASTEXITCODE -ne 0) { throw "Could not stage the Web Console bridge." }

    $extensionSource = Join-Path (Split-Path -Parent $ServiceSourceRoot) "browser-extension"
    if (Test-Path -LiteralPath $extensionSource -PathType Container) {
        Copy-Item -LiteralPath $extensionSource -Destination (Join-Path $serviceStage "browser-extension") -Recurse -Force
    }

    $assetsSource = Join-Path $ServiceSourceRoot "assets"
    if (Test-Path -LiteralPath $assetsSource -PathType Container) {
        Copy-Item -LiteralPath $assetsSource -Destination (Join-Path $serviceStage "assets") -Recurse -Force
    }
    New-ElevatedActionManifest `
        (Join-Path $serviceStage "elevated-broker.ps1") `
        (Join-Path $serviceStage "elevated-actions.manifest.json")
    return $serviceStage
}

function Invoke-CodingToolsTunnelServerInfo([int]$TimeoutSeconds = 3) {
    $body = @{
        jsonrpc = "2.0"
        id = "service-readiness"
        method = "tools/call"
        params = @{
            name = "server_info"
            arguments = @{}
        }
    } | ConvertTo-Json -Depth 8 -Compress
    $response = Invoke-RestMethod `
        -Uri "http://127.0.0.1:8767/mcp" `
        -Method Post `
        -ContentType "application/json" `
        -Headers @{
            Accept = "application/json, text/event-stream"
            "MCP-Protocol-Version" = "2026-07-28"
        } `
        -Body $body `
        -TimeoutSec $TimeoutSeconds
    if ($response.error) {
        throw "Tunnel MCP server_info returned JSON-RPC error: $($response.error | ConvertTo-Json -Compress)"
    }
    $result = $response.result
    $info = $result.structuredContent
    if (-not $result -or $result.isError -or -not $info -or $info.ok -ne $true -or $info.server -ne "coding-tools-mcp") {
        throw "Tunnel MCP server_info returned an invalid tool result."
    }
    return $info
}

function Wait-CodingToolsMcpReady([int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    $lastError = "not probed"
    do {
        Start-Sleep -Milliseconds 500
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:8766/healthz" -TimeoutSec 3
            if (-not $health -or $health.status -ne "ok") {
                $lastError = "healthz did not return status=ok"
                continue
            }
            $serverInfo = Invoke-CodingToolsTunnelServerInfo -TimeoutSeconds 3
            if ($serverInfo.version -ne $health.version) {
                $lastError = "health/tunnel version mismatch ($($health.version) != $($serverInfo.version))"
                continue
            }
            if ($serverInfo.workspace -ne $health.workspace) {
                $lastError = "health/tunnel workspace mismatch"
                continue
            }
            return $serverInfo
        }
        catch {
            $lastError = $_.Exception.Message
        }
    } while ((Get-Date) -lt $deadline)
    throw "MCP did not become ready through healthz + Tunnel server_info: $lastError"
}

function Set-CodingToolsMcpRecoveryPolicy {
    & sc.exe failure WebGPTCodingToolsMCP `
        reset= 3600 `
        actions= restart/5000/restart/10000/restart/30000 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure WebGPTCodingToolsMCP service recovery actions."
    }
    & sc.exe failureflag WebGPTCodingToolsMCP 1 | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Could not enable WebGPTCodingToolsMCP failure actions for non-crash failures."
    }
}

function Stop-CodingToolsPrivateServices([int]$TimeoutSeconds = 20) {
    foreach ($name in @("WebGPTCloudflareTunnel", "WebGPTCodingToolsMCP")) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($service -and $service.Status -ne "Stopped") {
            Stop-Service -Name $name -Force
        }
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Milliseconds 250
        $running = @(Get-Service -Name WebGPTCloudflareTunnel,WebGPTCodingToolsMCP -ErrorAction SilentlyContinue |
            Where-Object Status -ne "Stopped")
    } while ($running.Count -gt 0 -and (Get-Date) -lt $deadline)
    if ($running.Count -gt 0) { throw "Timed out stopping the private MCP services." }

    # WinSW stops the PowerShell service host, but the venv launcher can leave
    # its base Python child behind. A retained child keeps one or more MCP
    # listeners alive and can make the replacement service look healthy while
    # tunnel traffic is still routed to the stale process.
    $reservedPorts = @(8765, 8766, 8767)
    $listenerPids = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -eq "127.0.0.1" -and $_.LocalPort -in $reservedPorts } |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($listenerPid in $listenerPids) {
        if ($listenerPid -gt 0 -and $listenerPid -ne $PID) {
            $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $listenerPid" -ErrorAction SilentlyContinue
            $ownerCommandLine = if ($owner) { [string]$owner.CommandLine } else { "" }
            $ownerName = if ($owner) { [string]$owner.Name } else { "unknown" }
            $managedListener = $ownerCommandLine -match '(?i)(coding_tools_mcp|run-mcp-service\.ps1|WebGPTCodingToolsMCPService)'
            if (-not $managedListener) {
                $ownedPorts = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                    Where-Object { $_.LocalAddress -eq "127.0.0.1" -and $_.LocalPort -in $reservedPorts -and $_.OwningProcess -eq $listenerPid } |
                    Select-Object -ExpandProperty LocalPort -Unique) -join ","
                throw "Reserved MCP port collision: port(s) $ownedPorts are owned by unexpected process pid=$listenerPid name=$ownerName. Refusing to kill it automatically."
            }
            Stop-Process -Id $listenerPid -Force -ErrorAction SilentlyContinue
        }
    }

    $listenerDeadline = (Get-Date).AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 250
        $retainedListeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -eq "127.0.0.1" -and $_.LocalPort -in $reservedPorts })
    } while ($retainedListeners.Count -gt 0 -and (Get-Date) -lt $listenerDeadline)
    if ($retainedListeners.Count -gt 0) {
        $retainedSummary = ($retainedListeners | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort) pid=$($_.OwningProcess)" }) -join ", "
        throw "Timed out clearing retained private MCP listeners: $retainedSummary"
    }
}

function Start-CodingToolsPrivateServices([string]$ExpectedVersion = "") {
    Set-CodingToolsMcpRecoveryPolicy
    Start-Service -Name WebGPTCodingToolsMCP
    $serverInfo = Wait-CodingToolsMcpReady
    if (-not [string]::IsNullOrWhiteSpace($ExpectedVersion) -and $serverInfo.version -ne $ExpectedVersion) {
        throw "MCP started with version $($serverInfo.version), expected $ExpectedVersion."
    }
    Start-Service -Name WebGPTCloudflareTunnel
    return $serverInfo
}

function Copy-CodingToolsPackageToStage([string]$PackageRoot, [string]$RunnerSource, [string]$StageRoot) {
    New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
    $stageApp = Join-Path $StageRoot "app"
    $packageName = Split-Path -Leaf $PackageRoot
    New-Item -ItemType Directory -Path $stageApp -Force | Out-Null
    Copy-Item -LiteralPath $PackageRoot -Destination (Join-Path $stageApp $packageName) -Recurse -Force
    Copy-Item -LiteralPath $RunnerSource -Destination (Join-Path $StageRoot "run-mcp-service.ps1") -Force
}

function Restore-CodingToolsBundleToStage([string]$BundlePath, [string]$StageRoot) {
    Assert-DeploymentPath (Join-Path $BundlePath "app") "Rollback app backup"
    Assert-DeploymentPath (Join-Path $BundlePath "run-mcp-service.ps1") "Rollback runner backup"
    Assert-DeploymentPath (Join-Path $BundlePath "service") "Rollback service-component backup"
    Copy-CodingToolsPackageToStage `
        (Join-Path $BundlePath "app\coding_tools_mcp") `
        (Join-Path $BundlePath "run-mcp-service.ps1") `
        $StageRoot
    Copy-Item -LiteralPath (Join-Path $BundlePath "service") -Destination (Join-Path $StageRoot "service") -Recurse -Force
}

function Stop-CodingToolsBrokerProcesses([string]$ServiceRoot) {
    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    foreach ($pair in @(
        @{ Manager = "manage-elevated-broker.ps1"; Task = "WebGPT-Elevated-Broker" },
        @{ Manager = "manage-interactive-broker.ps1"; Task = "WebGPT-Interactive-Broker" }
    )) {
        $manager = Join-Path $ServiceRoot $pair.Manager
        if (Test-Path -LiteralPath $manager -PathType Leaf) {
            & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
                -File $manager -Action Stop 2>$null | Out-Null
        }
        Stop-ScheduledTask -TaskName $pair.Task -ErrorAction SilentlyContinue
    }
    Get-Process -Name "elevated-broker-launcher","interactive-broker-launcher","computer-use-overlay","activity-log-viewer","web-console-bridge" -ErrorAction SilentlyContinue |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

function Set-CodingToolsBrokerPermissions(
    [string]$ServiceRoot,
    [string]$ElevatedQueueRoot,
    [string]$InteractiveQueueRoot,
    [string]$LocalServiceSid,
    [string]$CurrentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
) {
    New-Item -ItemType Directory -Path $ElevatedQueueRoot,$InteractiveQueueRoot -Force | Out-Null
    & icacls.exe $ElevatedQueueRoot /inheritance:r /remove:g "${CurrentAccount}" /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not remove the signed-in user's write access from the elevated broker queue." }
    & icacls.exe $ElevatedQueueRoot /inheritance:r /grant:r `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" `
        "${LocalServiceSid}:(OI)(CI)M" /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not secure the elevated broker queue." }

    & icacls.exe $InteractiveQueueRoot /inheritance:r /grant:r `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" `
        "${CurrentAccount}:(OI)(CI)M" `
        "${LocalServiceSid}:(OI)(CI)M" /C | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not secure the interactive broker queue." }

    foreach ($file in @("elevated-broker.ps1", "elevated-broker-launcher.exe", "manage-elevated-broker.ps1", "elevated-actions.manifest.json")) {
        $path = Join-Path $ServiceRoot $file
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        & icacls.exe $path /inheritance:r /grant:r `
            "*S-1-5-18:F" `
            "*S-1-5-32-544:F" `
            "${CurrentAccount}:RX" `
            "${LocalServiceSid}:RX" /C | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Could not protect privileged broker file: $path" }
    }
}

function Backup-CodingToolsServiceComponents(
    [string]$Destination,
    [string]$ServiceRoot,
    [string[]]$ManagedServiceFiles
) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($file in $ManagedServiceFiles) {
        $source = Join-Path $ServiceRoot $file
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $Destination $file) -Force
        }
    }
    $assets = Join-Path $ServiceRoot "assets"
    if (Test-Path -LiteralPath $assets -PathType Container) {
        Copy-Item -LiteralPath $assets -Destination (Join-Path $Destination "assets") -Recurse -Force
    }
    $extension = Join-Path $ServiceRoot "browser-extension"
    if (Test-Path -LiteralPath $extension -PathType Container) {
        Copy-Item -LiteralPath $extension -Destination (Join-Path $Destination "browser-extension") -Recurse -Force
    }
}

function Install-CodingToolsBrokerArtifacts(
    [string]$ServiceStage,
    [string]$ServiceRoot,
    [string[]]$ManagedServiceFiles,
    [string]$ElevatedQueueRoot,
    [string]$InteractiveQueueRoot,
    [string]$LocalServiceSid
) {
    Assert-DeploymentPath $ServiceStage "Staged broker artifacts"
    Stop-CodingToolsBrokerProcesses $ServiceRoot
    foreach ($file in $ManagedServiceFiles) {
        $source = Join-Path $ServiceStage $file
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $ServiceRoot $file) -Force
        }
    }
    $stagedAssets = Join-Path $ServiceStage "assets"
    if (Test-Path -LiteralPath $stagedAssets -PathType Container) {
        Remove-Item -LiteralPath (Join-Path $ServiceRoot "assets") -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath $stagedAssets -Destination (Join-Path $ServiceRoot "assets") -Recurse -Force
    }
    $stagedExtension = Join-Path $ServiceStage "browser-extension"
    if (Test-Path -LiteralPath $stagedExtension -PathType Container) {
        Remove-Item -LiteralPath (Join-Path $ServiceRoot "browser-extension") -Recurse -Force -ErrorAction SilentlyContinue
        Copy-Item -LiteralPath $stagedExtension -Destination (Join-Path $ServiceRoot "browser-extension") -Recurse -Force
    }

    Set-CodingToolsBrokerPermissions $ServiceRoot $ElevatedQueueRoot $InteractiveQueueRoot $LocalServiceSid

    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    foreach ($managerName in @("manage-elevated-broker.ps1", "manage-interactive-broker.ps1")) {
        $manager = Join-Path $ServiceRoot $managerName
        & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Install
        if ($LASTEXITCODE -ne 0) { throw "Could not install broker task through $managerName" }
        & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Start
        if ($LASTEXITCODE -ne 0) { throw "Could not start broker task through $managerName" }
    }
}

function Restore-CodingToolsServiceComponents(
    [string]$Source,
    [string]$ServiceRoot,
    [string[]]$ManagedServiceFiles,
    [string]$ElevatedQueueRoot,
    [string]$InteractiveQueueRoot,
    [string]$LocalServiceSid
) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { return }
    Stop-CodingToolsBrokerProcesses $ServiceRoot
    foreach ($file in $ManagedServiceFiles) {
        $destination = Join-Path $ServiceRoot $file
        Remove-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
        $backup = Join-Path $Source $file
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            Copy-Item -LiteralPath $backup -Destination $destination -Force
        }
    }
    $assetsDestination = Join-Path $ServiceRoot "assets"
    Remove-Item -LiteralPath $assetsDestination -Recurse -Force -ErrorAction SilentlyContinue
    $assetsBackup = Join-Path $Source "assets"
    if (Test-Path -LiteralPath $assetsBackup -PathType Container) {
        Copy-Item -LiteralPath $assetsBackup -Destination $assetsDestination -Recurse -Force
    }
    $extensionDestination = Join-Path $ServiceRoot "browser-extension"
    Remove-Item -LiteralPath $extensionDestination -Recurse -Force -ErrorAction SilentlyContinue
    $extensionBackup = Join-Path $Source "browser-extension"
    if (Test-Path -LiteralPath $extensionBackup -PathType Container) {
        Copy-Item -LiteralPath $extensionBackup -Destination $extensionDestination -Recurse -Force
    }
    Set-CodingToolsBrokerPermissions $ServiceRoot $ElevatedQueueRoot $InteractiveQueueRoot $LocalServiceSid
    $windowsPowerShell = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
    foreach ($managerName in @("manage-elevated-broker.ps1", "manage-interactive-broker.ps1")) {
        $manager = Join-Path $ServiceRoot $managerName
        if (-not (Test-Path -LiteralPath $manager -PathType Leaf)) { continue }
        & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Install 2>$null | Out-Null
        & $windowsPowerShell -NoProfile -ExecutionPolicy Bypass -File $manager -Action Start 2>$null | Out-Null
    }
}
