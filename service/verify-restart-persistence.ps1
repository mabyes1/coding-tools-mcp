$ErrorActionPreference = "Stop"

# One-shot verification helper. It intentionally performs a service restart so
# the same access token can be checked against a fresh MCP process.
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $self = $PSCommandPath
    $elevated = Start-Process -FilePath "C:\Program Files\PowerShell\7\pwsh.exe" `
        -Verb RunAs -Wait -PassThru -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $self)
    exit $elevated.ExitCode
}

$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$serverPython = Join-Path $serviceRoot "venv\Scripts\python.exe"
$passwordFile = Join-Path $serviceRoot "oauth-password.machine.dpapi"
$tokenFile = Join-Path ([IO.Path]::GetTempPath()) ("webgpt-mcp-restart-" + [Guid]::NewGuid().ToString("N") + ".json")

function Unprotect-MachineSecret([string]$Path) {
    $encryptedBytes = [Convert]::FromBase64String((Get-Content -LiteralPath $Path -Raw).Trim())
    $plainBytes = [Security.Cryptography.ProtectedData]::Unprotect(
        $encryptedBytes, $null, [Security.Cryptography.DataProtectionScope]::LocalMachine
    )
    return [Text.Encoding]::UTF8.GetString($plainBytes)
}

try {
    $env:TEST_MCP_PASSWORD = Unprotect-MachineSecret $passwordFile
    $env:TEST_MCP_TOKEN_FILE = $tokenFile
    @'
import base64, hashlib, json, os, secrets, urllib.parse, urllib.request
base = "https://mcp.kennyxizi.pp.ua"
local = "http://127.0.0.1:8765"
client_id = "PQWKcrcy4yTeumHoguSJuph3a2oHagI2"
redirect_uri = "https://chatgpt.com/connector/oauth/7huIbrSodcVp"
verifier = secrets.token_urlsafe(48)
challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
params = {
    "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
    "code_challenge": challenge, "code_challenge_method": "S256", "state": "restart-check",
    "resource": base,
}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers): return fp
    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302

opener = urllib.request.build_opener(NoRedirect)
get = opener.open(urllib.request.Request(local + "/oauth/authorize?" + urllib.parse.urlencode(params)), timeout=20)
if get.status != 200:
    raise RuntimeError("authorize GET failed: " + str(get.status))
post = opener.open(urllib.request.Request(
    local + "/oauth/authorize",
    data=urllib.parse.urlencode(params | {"password": os.environ["TEST_MCP_PASSWORD"]}).encode(),
    headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
), timeout=20)
if post.status != 302:
    raise RuntimeError("authorize POST failed: " + str(post.status))
code = urllib.parse.parse_qs(urllib.parse.urlsplit(post.headers.get("Location", "")).query).get("code", [""])[0]
if not code:
    raise RuntimeError("authorization code missing")

def token(data):
    response = urllib.request.urlopen(urllib.request.Request(
        local + "/oauth/token", data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    ), timeout=20)
    return json.load(response)

issued = token({
    "grant_type": "authorization_code", "client_id": client_id, "code": code,
    "redirect_uri": redirect_uri, "code_verifier": verifier, "resource": base,
})
if not issued.get("access_token"):
    raise RuntimeError("access token missing")
if not issued.get("refresh_token"):
    raise RuntimeError("refresh token missing")
with open(os.environ["TEST_MCP_TOKEN_FILE"], "w", encoding="utf-8") as handle:
    json.dump({
        "access_token": issued["access_token"],
        "refresh_token": issued["refresh_token"],
    }, handle)
print("TOKEN_BEFORE_RESTART_READY")
'@ | & $serverPython -
    if ($LASTEXITCODE -ne 0) { throw "token acquisition failed" }
    Remove-Item Env:TEST_MCP_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:TEST_MCP_TOKEN_FILE -ErrorAction SilentlyContinue

    Restart-Service -Name "WebGPTCodingToolsMCP" -Force
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        try { $health = Invoke-RestMethod "http://127.0.0.1:8766/healthz" -TimeoutSec 3 } catch { $health = $null }
    } while ((-not $health) -and (Get-Date) -lt $deadline)
    if (-not $health -or $health.status -ne "ok") { throw "MCP did not become healthy after restart" }

    $env:TEST_MCP_TOKEN_FILE = $tokenFile
    @'
import json, os, urllib.parse, urllib.request
with open(os.environ["TEST_MCP_TOKEN_FILE"], encoding="utf-8") as handle:
    saved = json.load(handle)
refresh_body = urllib.parse.urlencode({
    "grant_type": "refresh_token",
    "client_id": "PQWKcrcy4yTeumHoguSJuph3a2oHagI2",
    "refresh_token": saved["refresh_token"],
    "resource": "https://mcp.kennyxizi.pp.ua",
}).encode()
refresh_response = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8765/oauth/token",
    data=refresh_body,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
    method="POST",
), timeout=20)
refreshed = json.load(refresh_response)
access_token = refreshed.get("access_token")
if not access_token:
    raise RuntimeError("refresh token did not yield a new access token")
body = json.dumps({
    "jsonrpc": "2.0", "id": "restart-check", "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "restart-check", "version": "1"}},
}).encode()
response = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:8765/mcp", data=body,
    headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": "Bearer " + access_token}, method="POST",
), timeout=20)
if response.status != 200:
    raise RuntimeError("MCP after restart failed: " + str(response.status))
print("PRODUCTION_RESTART_REFRESH_PERSISTENCE_OK")
'@ | & $serverPython -
    if ($LASTEXITCODE -ne 0) { throw "token validation after restart failed" }
}
finally {
    Remove-Item Env:TEST_MCP_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:TEST_MCP_TOKEN_FILE -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tokenFile -Force -ErrorAction SilentlyContinue
}
