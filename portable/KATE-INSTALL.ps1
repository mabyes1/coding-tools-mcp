[CmdletBinding()]
param(
    [string]$Workspace
)

$ErrorActionPreference = "Stop"
$bundleRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path $PSScriptRoot ".runtime"
$venvRoot = Join-Path $runtimeRoot "venv"
$cloudflared = Join-Path $runtimeRoot "cloudflared.exe"
$workspaceFile = Join-Path $runtimeRoot "workspace.txt"
$oauthPasswordFile = Join-Path $runtimeRoot "oauth-password.txt"
$oauthSecretFile = Join-Path $runtimeRoot "oauth-token-secret.txt"

function Resolve-Python {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        return [pscustomobject]@{ Exe = $py.Source; Args = @("-3") }
    }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        return [pscustomobject]@{ Exe = $python.Source; Args = @() }
    }
    throw "Python 3 is required. Install Python 3.11+ and run KATE-INSTALL.ps1 again."
}

if ([string]::IsNullOrWhiteSpace($Workspace)) {
    $Workspace = Read-Host "Workspace folder for Kate (example: C:\coding\project)"
}
if ([string]::IsNullOrWhiteSpace($Workspace)) { throw "Workspace cannot be empty." }
$Workspace = [IO.Path]::GetFullPath($Workspace)
if (-not (Test-Path -LiteralPath $Workspace -PathType Container)) {
    throw "Workspace folder does not exist: $Workspace"
}

foreach ($required in @(
    (Join-Path $bundleRoot "private\coding_tools_mcp\__main__.py"),
    (Join-Path $bundleRoot "private\coding_tools_mcp\bootstrap.py")
)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Portable bundle is incomplete; missing $required"
    }
}

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null

$pythonCommand = Resolve-Python
if (-not (Test-Path -LiteralPath (Join-Path $venvRoot "Scripts\python.exe") -PathType Leaf)) {
    Write-Host "Creating Python environment..."
    & $pythonCommand.Exe @($pythonCommand.Args) -m venv $venvRoot
    if ($LASTEXITCODE -ne 0) { throw "Could not create Python virtual environment." }
}

$venvPython = Join-Path $venvRoot "Scripts\python.exe"
Write-Host "Installing coding-tools-mcp runtime dependencies..."
& $venvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
& $venvPython -m pip install --disable-pip-version-check "coding-tools-mcp==0.2.2"
if ($LASTEXITCODE -ne 0) { throw "coding-tools-mcp dependency installation failed." }

if (-not (Test-Path -LiteralPath $cloudflared -PathType Leaf)) {
    Write-Host "Downloading Cloudflare tunnel client..."
    Invoke-WebRequest `
        -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" `
        -OutFile $cloudflared
    Unblock-File -LiteralPath $cloudflared
}

if (-not (Test-Path -LiteralPath $oauthPasswordFile -PathType Leaf)) {
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $password = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    [IO.File]::WriteAllText($oauthPasswordFile, $password, [Text.UTF8Encoding]::new($false))
}

if (-not (Test-Path -LiteralPath $oauthSecretFile -PathType Leaf)) {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [IO.File]::WriteAllText($oauthSecretFile, [Convert]::ToBase64String($bytes), [Text.UTF8Encoding]::new($false))
}

[IO.File]::WriteAllText($workspaceFile, $Workspace, [Text.UTF8Encoding]::new($false))

Write-Host ""
Write-Host "INSTALL OK" -ForegroundColor Green
Write-Host "Workspace: $Workspace"
Write-Host "Next: run portable\KATE-RUN.ps1"

