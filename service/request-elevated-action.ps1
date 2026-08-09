[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("sync-installed-webroot")]
    [string]$Action,
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"
$queueRoot = "C:\ProgramData\WebGPTCodingToolsMCPService\elevated-requests"
if (-not (Test-Path -LiteralPath $queueRoot -PathType Container)) {
    [Console]::Error.WriteLine("elevation_required: interactive elevated broker queue is unavailable: $queueRoot")
    exit 125
}
$brokerPidPath = Join-Path $queueRoot "broker.pid"
$brokerPid = $null
try { $brokerPid = [int](Get-Content -LiteralPath $brokerPidPath -Raw -ErrorAction Stop) } catch { }
if (-not $brokerPid -or -not (Get-Process -Id $brokerPid -ErrorAction SilentlyContinue)) {
    [Console]::Error.WriteLine("uac_unavailable: interactive elevated broker is not running; start WebGPT-Elevated-Broker first.")
    exit 125
}
$requestId = [Guid]::NewGuid().ToString("N")
$requestPath = Join-Path $queueRoot "$requestId.request"
$responsePath = Join-Path $queueRoot "$requestId.response"
$temporary = Join-Path $queueRoot ".$requestId.request.tmp"
$payload = @{
    protocol = 1
    request_id = $requestId
    action = $Action
    created_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    requested_by = [Security.Principal.WindowsIdentity]::GetCurrent().Name
} | ConvertTo-Json -Compress

try {
    [IO.File]::WriteAllText($temporary, $payload, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $requestPath -Force
    $deadline = (Get-Date).AddSeconds([Math]::Max(1, [Math]::Min($TimeoutSeconds, 600)))
    do {
        if (Test-Path -LiteralPath $responsePath -PathType Leaf) {
            $response = Get-Content -LiteralPath $responsePath -Raw | ConvertFrom-Json
            Remove-Item -LiteralPath $responsePath -Force -ErrorAction SilentlyContinue
            if ($response.ok) {
                Write-Host ([string]$response.message)
                exit ([int]($response.exit_code | ForEach-Object { if ($_ -eq $null) { 0 } else { $_ } }))
            }
            [Console]::Error.WriteLine("$($response.error): $($response.message)")
            exit 1
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)
    [Console]::Error.WriteLine("uac_unavailable: timed out waiting for interactive elevation approval.")
    exit 124
}
finally {
    Remove-Item -LiteralPath $temporary,$requestPath -Force -ErrorAction SilentlyContinue
}
