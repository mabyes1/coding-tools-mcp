$ErrorActionPreference = "Stop"
$implementation = Join-Path $PSScriptRoot "service\internal\install-coding-tools.ps1"
if (-not (Test-Path -LiteralPath $implementation -PathType Leaf)) {
    throw "coding-tools installer implementation is missing: $implementation"
}
& $implementation
