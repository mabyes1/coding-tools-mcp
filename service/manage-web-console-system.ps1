[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("StartAll", "StopAll", "RestartAll", "RestartTunnel", "Update", "Rollback", "Safe", "Trusted", "Yolo")]
    [string]$Action,
    [string]$RepoRoot = "D:\coding-tools-mcp\coding-tools-mcp"
)

$ErrorActionPreference = "Stop"
$serviceRoot = $PSScriptRoot
$logRoot = Join-Path $serviceRoot "logs"
$logPath = Join-Path $logRoot "web-console-admin.log"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

function Write-AdminLog([string]$Message) {
    $line = "{0} [{1}] {2}" -f ([DateTimeOffset]::Now.ToString("o")), $Action, $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    Write-Host $line
}

function Get-ExistingService([string]$Name) {
    return Get-Service -Name $Name -ErrorAction SilentlyContinue
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
    }
    Write-AdminLog "Completed"
    exit 0
}
catch {
    Write-AdminLog ("FAILED: " + $_.Exception.Message)
    throw
}
