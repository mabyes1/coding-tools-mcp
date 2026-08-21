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
