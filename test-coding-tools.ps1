[CmdletBinding()]
param(
    [ValidateSet("Quick", "Full", "System", "Interactive")]
    [string]$Mode = "Quick",
    [string]$Workspace = "",
    [string]$HealthUrl = "",
    [string]$ServiceRoot = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$runner = if ($Mode -eq "Interactive") {
    Join-Path $repoRoot "service\test-interactive-surfaces.py"
} else {
    Join-Path $repoRoot "service\test-coding-tools.py"
}
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Coding Tools test runner is missing: $runner"
}

$pythonCandidates = @(
    (Join-Path $repoRoot "service\venv\Scripts\python.exe"),
    "C:\ProgramData\WebGPTCodingToolsMCPService\venv\Scripts\python.exe"
)
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $python) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $python = $pythonCommand.Source }
}
if (-not $python) {
    throw "Python was not found. Install the project runtime or set a service venv first."
}

$arguments = @($runner)
if ($Mode -ne "Interactive") { $arguments += @("--mode", $Mode.ToLowerInvariant()) }
if ($Mode -ne "Interactive") {
    if ($Workspace) { $arguments += @("--workspace", $Workspace) }
    if ($HealthUrl) { $arguments += @("--health-url", $HealthUrl) }
    if ($ServiceRoot) { $arguments += @("--service-root", $ServiceRoot) }
}

& $python @arguments
exit $LASTEXITCODE
