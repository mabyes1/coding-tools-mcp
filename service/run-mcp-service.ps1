$ErrorActionPreference = "Stop"

$serviceRoot = Split-Path -Parent $PSCommandPath
$passwordFile = Join-Path $serviceRoot "oauth-password.machine.dpapi"
$tokenSecretFile = Join-Path $serviceRoot "oauth-token-secret.machine.dpapi"
$serverPython = Join-Path $serviceRoot "venv\Scripts\python.exe"
$privateAppRoot = Join-Path $serviceRoot "app"
$oauthStatePath = Join-Path $serviceRoot "data\oauth-state.sqlite"
$permissionModePath = Join-Path $serviceRoot "permission-mode.txt"
$workspaceConfigPath = Join-Path $serviceRoot "data\workspace-config.json"

$defaultWorkspaceConfig = [pscustomobject]@{
    selected = "coding-tools"
    workspaces = @(
        [pscustomobject]@{ name = "coding-tools"; path = "D:\coding-tools-mcp" },
        [pscustomobject]@{ name = "bulter"; path = "M:\" }
    )
}

function Get-WorkspaceRuntimeConfig {
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
        if ($name -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
            throw "Workspace selector is invalid: $name"
        }
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
        Selected = $selected
        Entries = $entries
    }
}

$workspaceConfig = Get-WorkspaceRuntimeConfig
$workspaceRoot = [string]$workspaceConfig.Selected.path
$workspaceAllowlist = ($workspaceConfig.Entries | ForEach-Object {
    "{0}={1}" -f ([string]$_.name), ([string]$_.path)
}) -join ";"

function Unprotect-MachineSecret([string]$Path) {
    $encryptedBytes = [Convert]::FromBase64String(
        (Get-Content -LiteralPath $Path -Raw).Trim()
    )
    $plainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $encryptedBytes,
        $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    return [Text.Encoding]::UTF8.GetString($plainBytes)
}

$env:CODING_TOOLS_MCP_SERVER_URL = "https://mcp.kennyxizi.pp.ua"
$env:CODING_TOOLS_MCP_OAUTH_PASSWORD = Unprotect-MachineSecret $passwordFile
$env:CODING_TOOLS_MCP_OAUTH_TOKEN_SECRET = Unprotect-MachineSecret $tokenSecretFile
$env:CODING_TOOLS_MCP_OAUTH_CLIENT_ID = "PQWKcrcy4yTeumHoguSJuph3a2oHagI2"
$env:CODING_TOOLS_MCP_OAUTH_REDIRECT_URIS = "https://chatgpt.com/connector/oauth/7huIbrSodcVp"
$env:CODING_TOOLS_MCP_OAUTH_STATE_PATH = $oauthStatePath
$env:CODING_TOOLS_MCP_OAUTH_TOKEN_TTL = "2592000"
$env:CODING_TOOLS_MCP_OAUTH_REFRESH_TOKEN_TTL = "31536000"
$env:CODING_TOOLS_MCP_OAUTH_ALLOW_DYNAMIC_REGISTRATION = "1"
$env:CODING_TOOLS_MCP_TELEMETRY = "off"
$permissionMode = "safe"
if (Test-Path -LiteralPath $permissionModePath -PathType Leaf) {
    $configuredMode = ([IO.File]::ReadAllText($permissionModePath, [Text.UTF8Encoding]::new($false))).Trim().ToLowerInvariant()
    if ($configuredMode -in @("safe", "trusted", "dangerous")) { $permissionMode = $configuredMode }
    else { throw "Invalid MCP permission mode in ${permissionModePath}: $configuredMode" }
}
$env:CODING_TOOLS_MCP_PERMISSION_MODE = $permissionMode
$env:CODING_TOOLS_MCP_MAX_HTTP_SESSIONS = "256"
$env:CODING_TOOLS_MCP_HTTP_SESSION_TTL_SECONDS = "300"
$env:CODING_TOOLS_MCP_MAX_HTTP_SESSIONS_PER_OWNER = "64"
$env:CODING_TOOLS_MCP_HEALTH_PORT = "8766"
$env:CODING_TOOLS_MCP_TUNNEL_PORT = "8767"
$env:CODING_TOOLS_MCP_RUNTIME_ROOT = Join-Path $serviceRoot "runtime"
$env:CODING_TOOLS_MCP_PWSH_PATH = "C:\Program Files\PowerShell\7\pwsh.exe"
$env:CODING_TOOLS_MCP_WORKSPACE_ALLOWLIST = $workspaceAllowlist
$env:CODING_TOOLS_MCP_EXECUTABLE_ALLOWLIST = "adb.exe;git.exe;dotnet.exe;node.exe;pwsh.exe"
$env:CODING_TOOLS_MCP_ELEVATED_QUEUE = Join-Path $serviceRoot "elevated-requests"
$env:PYTHONPATH = $privateAppRoot

$devTools = "D:\DevTools"
if (Test-Path -LiteralPath $devTools -PathType Container) {
    $env:PATH = "$devTools;$env:PATH"
    function Resolve-DevTool([string]$ToolName) {
        $direct = Join-Path $devTools ($ToolName + ".exe")
        if (Test-Path -LiteralPath $direct -PathType Leaf) { return $direct }
        return Get-ChildItem -LiteralPath $devTools -Filter ($ToolName + ".exe") -File -Recurse -Depth 4 -ErrorAction SilentlyContinue |
            Sort-Object { $_.FullName.Length }, FullName |
            Select-Object -First 1 -ExpandProperty FullName
    }
    foreach ($toolName in @("adb", "rg", "fd")) {
        $candidate = Resolve-DevTool $toolName
        if ($candidate) {
            $candidateDir = Split-Path -Parent $candidate
            if ($env:PATH -notlike "*$candidateDir*") { $env:PATH = "$candidateDir;$env:PATH" }
            $envName = "CODING_TOOLS_MCP_" + $toolName.ToUpperInvariant() + "_PATH"
            Set-Item -Path ("Env:" + $envName) -Value $candidate
        }
    }
}

foreach ($requiredPath in @($serverPython, (Join-Path $privateAppRoot "coding_tools_mcp\__main__.py"))) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Private MCP runtime is incomplete; missing $requiredPath"
    }
}

& $serverPython -m coding_tools_mcp `
    --workspace $workspaceRoot `
    --host 127.0.0.1 `
    --port 8765 `
    --oauth-mode

exit $LASTEXITCODE
