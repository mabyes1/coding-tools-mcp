[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("StartAll", "StopAll", "RestartAll", "RestartTunnel", "Update", "Rollback", "Safe", "Trusted", "Yolo", "SwitchWorkspace", "AddWorkspace")]
    [string]$Action,
    [string]$RepoRoot = "D:\coding-tools-mcp\coding-tools-mcp",
    [string]$Workspace = ""
)

$ErrorActionPreference = "Stop"
$serviceRoot = $PSScriptRoot
$logRoot = Join-Path $serviceRoot "logs"
$logPath = Join-Path $logRoot "web-console-admin.log"
$workspaceConfigPath = Join-Path $serviceRoot "data\workspace-config.json"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

$defaultWorkspaceConfig = [pscustomobject]@{
    selected = "coding-tools"
    workspaces = @(
        [pscustomobject]@{ name = "coding-tools"; path = "D:\coding-tools-mcp" },
        [pscustomobject]@{ name = "bulter"; path = "M:\" }
    )
}

function Write-AdminLog([string]$Message) {
    $line = "{0} [{1}] {2}" -f ([DateTimeOffset]::Now.ToString("o")), $Action, $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-ExistingService([string]$Name) {
    return Get-Service -Name $Name -ErrorAction SilentlyContinue
}

function Get-WorkspaceConfig {
    $config = $defaultWorkspaceConfig
    if (Test-Path -LiteralPath $workspaceConfigPath -PathType Leaf) {
        try {
            $config = Get-Content -LiteralPath $workspaceConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        }
        catch {
            throw "Workspace configuration is invalid: $workspaceConfigPath ($($_.Exception.Message))"
        }
    }
    $entries = @($config.workspaces)
    if ($entries.Count -lt 1) { throw "Workspace configuration has no workspaces: $workspaceConfigPath" }
    $seenNames = @{}
    foreach ($entry in $entries) {
        $name = [string]$entry.name
        $path = [string]$entry.path
        if ($name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') { throw "Workspace selector is invalid: $name" }
        if ([string]::IsNullOrWhiteSpace($path)) { throw "Workspace path is empty for selector: $name" }
        $nameKey = $name.ToLowerInvariant()
        if ($seenNames.ContainsKey($nameKey)) { throw "Workspace selector is duplicated: $name" }
        $seenNames[$nameKey] = $true
    }
    $selectedToken = [string]$config.selected
    $selected = $entries | Where-Object {
        ([string]$_.name).Equals($selectedToken, [StringComparison]::OrdinalIgnoreCase) -or
        ([string]$_.path).Equals($selectedToken, [StringComparison]::OrdinalIgnoreCase)
    } | Select-Object -First 1
    if (-not $selected) { throw "Selected workspace is not in the configured allowlist: $selectedToken" }
    return [pscustomobject]@{
        Config = $config
        Entries = $entries
        Selected = $selected
    }
}

function Resolve-WorkspaceEntry([string]$Selector, [object]$WorkspaceConfig) {
    $raw = ([string]$Selector).Trim()
    if ([string]::IsNullOrWhiteSpace($raw)) { throw "Workspace selector is required." }
    $entry = $WorkspaceConfig.Entries | Where-Object {
        ([string]$_.name).Equals($raw, [StringComparison]::OrdinalIgnoreCase)
    } | Select-Object -First 1
    if (-not $entry) {
        $resolvedSelector = $null
        try { $resolvedSelector = (Resolve-Path -LiteralPath $raw -ErrorAction Stop).Path } catch { }
        if ($resolvedSelector) {
            $entry = $WorkspaceConfig.Entries | Where-Object {
                try {
                    $candidate = (Resolve-Path -LiteralPath ([string]$_.path) -ErrorAction Stop).Path
                    $candidate.Equals($resolvedSelector, [StringComparison]::OrdinalIgnoreCase)
                }
                catch { $false }
            } | Select-Object -First 1
        }
    }
    if (-not $entry) { throw "Selected workspace is not in the configured allowlist: $raw" }
    try { $resolvedPath = (Resolve-Path -LiteralPath ([string]$entry.path) -ErrorAction Stop).Path }
    catch { throw "Workspace path does not exist: $([string]$entry.path)" }
    if (-not (Get-Item -LiteralPath $resolvedPath -ErrorAction Stop).PSIsContainer) {
        throw "Workspace path is not a directory: $resolvedPath"
    }
    return [pscustomobject]@{
        Name = [string]$entry.name
        Path = [string]$entry.path
        ResolvedPath = $resolvedPath
    }
}

function Write-WorkspaceConfig([object]$Config, [string]$SelectedName, [object[]]$Entries = $null) {
    $Config.Config.selected = $SelectedName
    if ($null -ne $Entries) { $Config.Config.workspaces = @($Entries) }
    New-Item -ItemType Directory -Path (Split-Path -Parent $workspaceConfigPath) -Force | Out-Null
    $temporary = "$workspaceConfigPath.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText(
        $temporary,
        ($Config.Config | ConvertTo-Json -Depth 8),
        [Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $workspaceConfigPath -Force
}

function Get-PathKey([string]$Path) {
    $value = [string]$Path
    try { $value = (Resolve-Path -LiteralPath $value -ErrorAction Stop).Path } catch { }
    return ($value -replace '[\\/]+$', '')
}

function Assert-NoActiveMcpWork {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:8766/healthz" -TimeoutSec 3
        $running = [int]$health.execution.running
        $inFlight = [int]$health.http_sessions.in_flight
        if ($running -gt 0 -or $inFlight -gt 0) {
            throw "MCP is busy (execution.running=$running, http_sessions.in_flight=$inFlight). Finish the current work before switching workspace."
        }
    }
    catch {
        if ($_.Exception.Message -like "MCP is busy (*)") { throw }
        Write-AdminLog "MCP health was unavailable before workspace switch; continuing with service restart."
    }
}

function Wait-McpWorkspace([string]$ExpectedPath, [int]$TimeoutSeconds = 45) {
    $expectedKey = Get-PathKey $ExpectedPath
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastError = "not probed"
    do {
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:8766/healthz" -TimeoutSec 3
            if ($health.status -eq "ok" -and $health.workspace) {
                $actualKey = Get-PathKey ([string]$health.workspace)
                if ($actualKey.Equals($expectedKey, [StringComparison]::OrdinalIgnoreCase)) { return $health }
                $lastError = "health workspace is $([string]$health.workspace), expected $ExpectedPath"
            }
            else { $lastError = "healthz did not report status=ok" }
        }
        catch { $lastError = $_.Exception.Message }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "MCP did not recover on the selected workspace: $lastError"
}

function New-WorkspaceName([string]$Path, [object[]]$Entries) {
    $leaf = "workspace"
    try { $leaf = [string](Get-Item -LiteralPath $Path -ErrorAction Stop).Name } catch { }
    $candidate = $leaf -replace '[^A-Za-z0-9._-]', '-'
    if ([string]::IsNullOrWhiteSpace($candidate)) { $candidate = "workspace" }
    if ($candidate -notmatch '^[A-Za-z0-9]') { $candidate = "workspace-$candidate" }
    if ($candidate.Length -gt 64) { $candidate = $candidate.Substring(0, 64) }
    $base = $candidate
    $suffix = 2
    while (@($Entries | Where-Object { ([string]$_.name).Equals($candidate, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0) {
        $suffixText = "-$suffix"
        $candidate = $base.Substring(0, [Math]::Min($base.Length, 64 - $suffixText.Length)) + $suffixText
        $suffix++
    }
    return $candidate
}

function Grant-WorkspaceAccess([string]$Path) {
    if ($Path.StartsWith("\\", [StringComparison]::Ordinal)) {
        Write-AdminLog "Workspace is remote; keeping its existing share permissions: $Path"
        return
    }
    & icacls.exe $Path /grant "*S-1-5-19:(OI)(CI)M" /T /C | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not grant LocalService access to workspace: $Path" }
}

function Add-Workspace([string]$Path) {
    $workspaceConfig = Get-WorkspaceConfig
    try { $resolvedPath = (Resolve-Path -LiteralPath ([string]$Path) -ErrorAction Stop).Path }
    catch { throw "Workspace path does not exist: $Path" }
    if (-not (Get-Item -LiteralPath $resolvedPath -ErrorAction Stop).PSIsContainer) {
        throw "Workspace path is not a directory: $resolvedPath"
    }

    $existing = $workspaceConfig.Entries | Where-Object {
        try {
            $candidate = (Resolve-Path -LiteralPath ([string]$_.path) -ErrorAction Stop).Path
            $candidate.Equals($resolvedPath, [StringComparison]::OrdinalIgnoreCase)
        }
        catch { $false }
    } | Select-Object -First 1
    if ($existing) {
        $entry = Resolve-WorkspaceEntry ([string]$existing.name) $workspaceConfig
        Write-AdminLog "Workspace already registered: $($entry.Name) ($($entry.ResolvedPath))"
        return
    }

    $name = New-WorkspaceName $resolvedPath $workspaceConfig.Entries
    $newEntry = [pscustomobject]@{ name = $name; path = $resolvedPath }
    $newEntries = @($workspaceConfig.Entries) + @($newEntry)
    Write-WorkspaceConfig $workspaceConfig ([string]$workspaceConfig.Config.selected) $newEntries
    Write-AdminLog "Workspace added without switching: $name ($resolvedPath)"
}

function Restart-McpService {
    $service = Get-ExistingService "WebGPTCodingToolsMCP"
    if (-not $service) { throw "WebGPTCodingToolsMCP service is not installed" }
    if ($service.Status -ne "Stopped") {
        Write-AdminLog "Stopping WebGPTCodingToolsMCP"
        Stop-Service -Name "WebGPTCodingToolsMCP" -Force -ErrorAction Stop
    }
    Start-Sleep -Milliseconds 500
    Write-AdminLog "Starting WebGPTCodingToolsMCP"
    Start-Service -Name "WebGPTCodingToolsMCP" -ErrorAction Stop
}

function Set-Workspace([string]$Selector) {
    $workspaceConfig = Get-WorkspaceConfig
    $entry = Resolve-WorkspaceEntry $Selector $workspaceConfig
    $previous = Resolve-WorkspaceEntry ([string]$workspaceConfig.Config.selected) $workspaceConfig
    if ($entry.Name.Equals($previous.Name, [StringComparison]::OrdinalIgnoreCase)) {
        Write-AdminLog "Workspace already selected: $($entry.Name) ($($entry.ResolvedPath))"
        return
    }

    Assert-NoActiveMcpWork
    Grant-WorkspaceAccess $entry.ResolvedPath
    Write-WorkspaceConfig $workspaceConfig $entry.Name
    try {
        Restart-McpService
        Wait-McpWorkspace $entry.ResolvedPath | Out-Null
        Write-AdminLog "Workspace switched to $($entry.Name) ($($entry.ResolvedPath))"
    }
    catch {
        $switchError = $_.Exception.Message
        Write-AdminLog "Workspace switch failed; restoring $($previous.Name): $switchError"
        try {
            Write-WorkspaceConfig $workspaceConfig $previous.Name
            Restart-McpService
            Wait-McpWorkspace $previous.ResolvedPath | Out-Null
        }
        catch { Write-AdminLog "Workspace rollback failed: $($_.Exception.Message)" }
        throw $switchError
    }
}

function Stop-ManagedServices {
    foreach ($name in @("WebGPTCloudflareTunnel", "OpenAITunnelClient", "WebGPTCodingToolsMCP")) {
        $service = Get-ExistingService $name
        if ($service -and $service.Status -ne "Stopped") {
            Write-AdminLog "Stopping $name"
            Stop-Service -Name $name -Force -ErrorAction Stop
        }
    }
}

function Start-ManagedServices {
    foreach ($name in @("WebGPTCodingToolsMCP", "OpenAITunnelClient", "WebGPTCloudflareTunnel")) {
        $service = Get-ExistingService $name
        if ($service -and $service.Status -ne "Running") {
            Write-AdminLog "Starting $name"
            Start-Service -Name $name -ErrorAction Stop
        }
    }
}

function Invoke-LoggedScript([string]$Path, [string[]]$Arguments = @()) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required script is missing: $Path"
    }
    Write-AdminLog ("Running {0} {1}" -f $Path, ($Arguments -join " "))
    $powershell = Join-Path $PSHOME "powershell.exe"
    if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
        throw "PowerShell executable is missing: $powershell"
    }

    # Run maintenance scripts in a child PowerShell process and use its exit
    # code as the success signal. Native tools such as git legitimately emit
    # warnings on stderr; invoking the script directly under
    # ErrorActionPreference=Stop converts those stderr records into terminating
    # PowerShell errors and makes a healthy update look failed.
    $childArgs = @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Path) + $Arguments
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $powershell @childArgs 2>&1 | ForEach-Object {
            $text = [string]$_
            if (-not [string]::IsNullOrWhiteSpace($text)) { Write-AdminLog $text }
        }
        $childExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($childExitCode -ne 0) {
        throw "Maintenance script exited with code ${childExitCode}: $Path"
    }
}

try {
    Write-AdminLog "Requested"
    switch ($Action) {
        "StartAll" {
            Start-ManagedServices
        }
        "StopAll" {
            Stop-ManagedServices
        }
        "RestartAll" {
            Stop-ManagedServices
            Start-Sleep -Milliseconds 500
            Start-ManagedServices
        }
        "RestartTunnel" {
            $tunnel = Get-ExistingService "OpenAITunnelClient"
            if (-not $tunnel) { throw "OpenAITunnelClient service is not installed." }
            if ($tunnel.Status -ne "Stopped") {
                Write-AdminLog "Stopping OpenAITunnelClient"
                Stop-Service -Name OpenAITunnelClient -Force -ErrorAction Stop
            }
            Start-Sleep -Milliseconds 500
            Write-AdminLog "Starting OpenAITunnelClient"
            Start-Service -Name OpenAITunnelClient -ErrorAction Stop
        }
        "Update" {
            Invoke-LoggedScript (Join-Path $RepoRoot "update-coding-tools.ps1")
        }
        "Rollback" {
            Invoke-LoggedScript (Join-Path $RepoRoot "update-coding-tools.ps1") @("-Rollback")
        }
        "Safe" {
            Invoke-LoggedScript (Join-Path $serviceRoot "manage-mcp-permissions.ps1") @("-Action", "Safe")
        }
        "Trusted" {
            Invoke-LoggedScript (Join-Path $serviceRoot "manage-mcp-permissions.ps1") @("-Action", "Trusted")
        }
        "Yolo" {
            Invoke-LoggedScript (Join-Path $serviceRoot "manage-mcp-permissions.ps1") @("-Action", "Yolo", "-ConfirmYolo")
        }
        "SwitchWorkspace" {
            Set-Workspace $Workspace
        }
        "AddWorkspace" {
            Add-Workspace $Workspace
        }
    }
    Write-AdminLog "Completed"
    exit 0
}
catch {
    Write-AdminLog ("FAILED: " + $_.Exception.Message)
    throw
}
