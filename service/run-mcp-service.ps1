$ErrorActionPreference = "Stop"

$serviceRoot = Split-Path -Parent $PSCommandPath
$passwordFile = Join-Path $serviceRoot "oauth-password.machine.dpapi"
$tokenSecretFile = Join-Path $serviceRoot "oauth-token-secret.machine.dpapi"
$serverPython = Join-Path $serviceRoot "venv\Scripts\python.exe"
$privateAppRoot = Join-Path $serviceRoot "app"
$oauthStatePath = Join-Path $serviceRoot "data\oauth-state.sqlite"

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
$env:CODING_TOOLS_MCP_OAUTH_ALLOW_DYNAMIC_REGISTRATION = "0"
$env:CODING_TOOLS_MCP_TELEMETRY = "off"
$env:CODING_TOOLS_MCP_MAX_HTTP_SESSIONS = "256"
$env:CODING_TOOLS_MCP_HTTP_SESSION_TTL_SECONDS = "300"
$env:CODING_TOOLS_MCP_MAX_HTTP_SESSIONS_PER_OWNER = "32"
$env:CODING_TOOLS_MCP_HEALTH_PORT = "8766"
$env:CODING_TOOLS_MCP_RUNTIME_ROOT = Join-Path $serviceRoot "runtime"
$env:CODING_TOOLS_MCP_PWSH_PATH = "C:\Program Files\PowerShell\7\pwsh.exe"
$env:CODING_TOOLS_MCP_WORKSPACE_ALLOWLIST = "coding-tools=D:\coding-tools-mcp"
$env:CODING_TOOLS_MCP_EXECUTABLE_ALLOWLIST = "adb.exe;git.exe;dotnet.exe;node.exe;pwsh.exe"
$env:CODING_TOOLS_MCP_ELEVATED_QUEUE = Join-Path $serviceRoot "elevated-requests"
$env:PYTHONPATH = $privateAppRoot

foreach ($requiredPath in @($serverPython, (Join-Path $privateAppRoot "coding_tools_mcp\__main__.py"))) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Private MCP runtime is incomplete; missing $requiredPath"
    }
}

& $serverPython -m coding_tools_mcp `
    --workspace "D:\coding-tools-mcp" `
    --host 127.0.0.1 `
    --port 8765 `
    --oauth-mode

exit $LASTEXITCODE
