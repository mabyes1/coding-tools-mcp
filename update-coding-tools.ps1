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
$entry = Join-Path $PSScriptRoot "service\internal\deploy-coding-tools.ps1"
if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) {
    throw "coding-tools update entrypoint is missing: $entry"
}
& $entry @PSBoundParameters
