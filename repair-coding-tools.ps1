$ErrorActionPreference = "Stop"
$implementation = Join-Path $PSScriptRoot "service\internal\repair-coding-tools.ps1"
if (-not (Test-Path -LiteralPath $implementation -PathType Leaf)) {
    throw "coding-tools repair implementation is missing: $implementation"
}
& $implementation
