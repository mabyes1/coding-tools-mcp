$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "deployment-common.ps1")

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    throw "This installer must run as Administrator."
}

$serviceSourceRoot = Split-Path -Parent $PSScriptRoot
$sourceRoot = Split-Path -Parent $serviceSourceRoot
$templateRoot = $serviceSourceRoot
$privateSource = Join-Path $sourceRoot "private\coding_tools_mcp"
$serviceRoot = "C:\ProgramData\WebGPTCodingToolsMCPService"
$stateRoot = Join-Path $env:LOCALAPPDATA "coding-tools-mcp-web"
$installLog = Join-Path $sourceRoot "service-install-result.log"
$winsw = Join-Path $serviceRoot "winsw.exe"
$winswUrl = "https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe"
$uv = "C:\Users\ken\.local\bin\uv.exe"
$python = "C:\Users\ken\AppData\Local\Programs\Python\Python313\python.exe"
$pythonRoot = Split-Path -Parent $python
$credentialName = "18b544a3-7f65-4842-a5f9-e3aec9b534b4.json"
$credentialSource = Join-Path "C:\Users\ken\.cloudflared" $credentialName
$workspaceRoot = "D:\coding-tools-mcp"
$elevatedQueueRoot = Join-Path $serviceRoot "elevated-requests"
$interactiveQueueRoot = Join-Path $serviceRoot "interactive-requests"
$localServiceSid = "*S-1-5-19"
$managedServiceFiles = Get-CodingToolsManagedServiceFiles
$oauthStateBackup = Join-Path ([IO.Path]::GetTempPath()) ("web-gpt-oauth-state-" + [guid]::NewGuid().ToString("N") + ".sqlite")
$hadOAuthStateBackup = $false
$brokerStageRoot = Join-Path ([IO.Path]::GetTempPath()) ("web-gpt-broker-stage-" + [guid]::NewGuid().ToString("N"))

Start-Transcript -LiteralPath $installLog -Force
try {
    foreach ($requiredPath in @(
        $templateRoot,
        $privateSource,
        $workspaceRoot,
        $uv,
        $python,
        $credentialSource,
        (Join-Path $stateRoot "oauth-password.dpapi"),
        (Join-Path $stateRoot "oauth-token-secret.dpapi")
    )) {
        if (-not (Test-Path -LiteralPath $requiredPath)) {
            throw "Required path is missing: $requiredPath"
        }
    }

    Stop-CodingToolsPrivateServices 15
    foreach ($serviceName in @("WebGPTCloudflareTunnel", "WebGPTCodingToolsMCP")) {
        if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
            & sc.exe delete $serviceName | Out-Host
        }
    }
    if (Test-Path -LiteralPath $serviceRoot) {
        $resolvedServiceRoot = (Resolve-Path -LiteralPath $serviceRoot).Path
        if ($resolvedServiceRoot -ne "C:\ProgramData\WebGPTCodingToolsMCPService") {
            throw "Refusing to clean unexpected service path: $resolvedServiceRoot"
        }
        $existingOAuthState = Join-Path $resolvedServiceRoot "data\oauth-state.sqlite"
        if (Test-Path -LiteralPath $existingOAuthState) {
            Copy-Item -LiteralPath $existingOAuthState -Destination $oauthStateBackup -Force
            foreach ($suffix in @("-wal", "-shm")) {
                $sidecar = "$existingOAuthState$suffix"
                if (Test-Path -LiteralPath $sidecar) {
                    Copy-Item -LiteralPath $sidecar -Destination "$oauthStateBackup$suffix" -Force
                }
            }
            $hadOAuthStateBackup = $true
        }
        & takeown.exe /F $resolvedServiceRoot /A /R /D Y | Out-Host
        & icacls.exe $resolvedServiceRoot /grant `
            "*S-1-5-18:(OI)(CI)F" `
            "*S-1-5-32-544:(OI)(CI)F" `
            "$([Security.Principal.WindowsIdentity]::GetCurrent().Name):(OI)(CI)F" /T /C | Out-Host
        Get-ChildItem -LiteralPath $resolvedServiceRoot -Recurse -Force | ForEach-Object {
            if (-not $_.PSIsContainer -and $_.IsReadOnly) {
                $_.IsReadOnly = $false
            }
        }
        Remove-Item -LiteralPath $resolvedServiceRoot -Recurse -Force
    }

    New-Item -ItemType Directory -Path $serviceRoot -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $serviceRoot "logs") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $serviceRoot "data") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $serviceRoot "runtime") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $serviceRoot "app") -Force | Out-Null
    New-Item -ItemType Directory -Path $elevatedQueueRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $interactiveQueueRoot -Force | Out-Null
    $currentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name

    # Recover cleanly if an earlier install was interrupted after ACL hardening.
    & icacls.exe $serviceRoot /grant `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" `
        "${currentAccount}:(OI)(CI)F" /T /C | Out-Host

    if (-not (Test-Path -LiteralPath $winsw)) {
        Write-Host "Downloading the official WinSW 2.12.0 service wrapper..."
        Invoke-WebRequest -Uri $winswUrl -OutFile $winsw
    }
    Unblock-File -LiteralPath $winsw

    Write-Host "Creating a fixed Python environment for the MCP service..."
    $venvRoot = Join-Path $serviceRoot "venv"
    & $uv venv $venvRoot --python $python --clear
    if ($LASTEXITCODE -ne 0) {
        throw "uv venv failed with exit code $LASTEXITCODE."
    }
    $venvPython = Join-Path $venvRoot "Scripts\python.exe"
    & $uv pip install --python $venvPython "coding-tools-mcp==0.2.2"
    if ($LASTEXITCODE -ne 0) {
        throw "Installing coding-tools-mcp failed with exit code $LASTEXITCODE."
    }
    Copy-Item -LiteralPath $privateSource -Destination (Join-Path $serviceRoot "app") -Recurse -Force
    if ($hadOAuthStateBackup) {
        Copy-Item -LiteralPath $oauthStateBackup `
            -Destination (Join-Path $serviceRoot "data\oauth-state.sqlite") -Force
        foreach ($suffix in @("-wal", "-shm")) {
            $sidecar = "$oauthStateBackup$suffix"
            if (Test-Path -LiteralPath $sidecar) {
                Copy-Item -LiteralPath $sidecar `
                    -Destination "$(Join-Path $serviceRoot 'data\oauth-state.sqlite')$suffix" -Force
            }
        }
    }

    Copy-Item -LiteralPath (Join-Path $templateRoot "run-mcp-service.ps1") -Destination $serviceRoot -Force
    Copy-Item -LiteralPath (Join-Path $templateRoot "gpt-coding-mcp.yml") -Destination $serviceRoot -Force
    Copy-Item -LiteralPath (Join-Path $templateRoot "WebGPTCodingToolsMCP.xml") -Destination $serviceRoot -Force
    Copy-Item -LiteralPath (Join-Path $templateRoot "WebGPTCloudflareTunnel.xml") -Destination $serviceRoot -Force
    New-Item -ItemType Directory -Path $brokerStageRoot -Force | Out-Null
    $brokerServiceStage = New-CodingToolsBrokerArtifactStage `
        $brokerStageRoot `
        $templateRoot `
        (Join-Path $privateSource "computer-use-actions.json")
    # Existing broker processes are stopped by Install-CodingToolsBrokerArtifacts.
    Copy-Item -LiteralPath $credentialSource -Destination (Join-Path $serviceRoot $credentialName) -Force
    Copy-Item -LiteralPath $winsw -Destination (Join-Path $serviceRoot "WebGPTCodingToolsMCP.exe") -Force
    Copy-Item -LiteralPath $winsw -Destination (Join-Path $serviceRoot "WebGPTCloudflareTunnel.exe") -Force
    Unblock-File -LiteralPath (Join-Path $serviceRoot "WebGPTCodingToolsMCP.exe")
    Unblock-File -LiteralPath (Join-Path $serviceRoot "WebGPTCloudflareTunnel.exe")

    function Convert-CurrentUserSecretToMachineSecret([string]$Source, [string]$Destination) {
        $secure = ConvertTo-SecureString (Get-Content -LiteralPath $Source -Raw).Trim()
        $plain = [Net.NetworkCredential]::new("", $secure).Password
        $plainBytes = [Text.Encoding]::UTF8.GetBytes($plain)
        try {
            $protectedBytes = [Security.Cryptography.ProtectedData]::Protect(
                $plainBytes,
                $null,
                [Security.Cryptography.DataProtectionScope]::LocalMachine
            )
            [IO.File]::WriteAllText($Destination, [Convert]::ToBase64String($protectedBytes))
        }
        finally {
            [Array]::Clear($plainBytes, 0, $plainBytes.Length)
            $plain = $null
        }
    }

    Convert-CurrentUserSecretToMachineSecret `
        (Join-Path $stateRoot "oauth-password.dpapi") `
        (Join-Path $serviceRoot "oauth-password.machine.dpapi")
    Convert-CurrentUserSecretToMachineSecret `
        (Join-Path $stateRoot "oauth-token-secret.dpapi") `
        (Join-Path $serviceRoot "oauth-token-secret.machine.dpapi")

    foreach ($sensitiveFile in @(
        (Join-Path $serviceRoot $credentialName),
        (Join-Path $serviceRoot "oauth-password.machine.dpapi"),
        (Join-Path $serviceRoot "oauth-token-secret.machine.dpapi")
    )) {
        & icacls.exe $sensitiveFile /inheritance:r /grant:r `
            "*S-1-5-18:F" `
            "*S-1-5-32-544:F" `
            "${currentAccount}:F" | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Securing sensitive service file failed: $sensitiveFile"
        }
    }
    & icacls.exe $serviceRoot /inheritance:r /grant:r `
        "*S-1-5-18:(OI)(CI)F" `
        "*S-1-5-32-544:(OI)(CI)F" `
        "${currentAccount}:(OI)(CI)F" /C | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Securing the service root ACL failed."
    }
    foreach ($child in Get-ChildItem -LiteralPath $serviceRoot -Force) {
        & icacls.exe $child.FullName /reset /T /C | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "Securing service tree failed at $($child.FullName)."
        }
    }

    # The MCP and tunnel processes do not need SYSTEM.  LocalService gets only
    # the exact service tree and workspace required for this private endpoint.
    & icacls.exe $serviceRoot /grant `
        "${localServiceSid}:(OI)(CI)RX" /C | Out-Host
    & icacls.exe (Join-Path $serviceRoot "data") /grant `
        "${localServiceSid}:(OI)(CI)M" /C | Out-Host
    & icacls.exe (Join-Path $serviceRoot "runtime") /grant `
        "${localServiceSid}:(OI)(CI)M" /C | Out-Host
    & icacls.exe (Join-Path $serviceRoot "logs") /grant `
        "${localServiceSid}:(OI)(CI)M" /C | Out-Host
    & icacls.exe $workspaceRoot /grant `
        "${localServiceSid}:(OI)(CI)M" /T /C | Out-Host
    & icacls.exe $pythonRoot /grant `
        "${localServiceSid}:(OI)(CI)RX" /C | Out-Host

    Install-CodingToolsBrokerArtifacts `
        $brokerServiceStage `
        $serviceRoot `
        $managedServiceFiles `
        $elevatedQueueRoot `
        $interactiveQueueRoot `
        $localServiceSid

    Write-Host "Stopping and removing the old scheduled supervisors..."
    foreach ($taskName in @("WebGPT-CodingTools-MCP", "WebGPT-Cloudflare-Tunnel")) {
        $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        if ($task) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        }
    }

    Start-Sleep -Seconds 2
    $relatedProcesses = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -and (
            $_.CommandLine -like "*run-web-gpt-mcp-service.ps1*" -or
            $_.CommandLine -like "*run-web-gpt-tunnel-service.ps1*" -or
            $_.CommandLine -like "*gpt-coding-mcp.yml*"
        )
    }
    foreach ($process in $relatedProcesses) {
        if ($process.ProcessId -ne $PID) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }

    foreach ($xmlName in @("WebGPTCloudflareTunnel.xml", "WebGPTCodingToolsMCP.xml")) {
        $serviceId = [IO.Path]::GetFileNameWithoutExtension($xmlName)
        if (Get-Service -Name $serviceId -ErrorAction SilentlyContinue) {
            $wrapper = Join-Path $serviceRoot "$serviceId.exe"
            & $wrapper stop | Out-Host
            & $wrapper uninstall | Out-Host
        }
    }

    Write-Host "Installing genuine Windows services..."
    foreach ($xmlName in @("WebGPTCodingToolsMCP.xml", "WebGPTCloudflareTunnel.xml")) {
        $serviceId = [IO.Path]::GetFileNameWithoutExtension($xmlName)
        $wrapper = Join-Path $serviceRoot "$serviceId.exe"
        & $wrapper install | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw "WinSW failed to install $xmlName."
        }
    }

    & sc.exe failure WebGPTCloudflareTunnel reset= 0 actions= "" | Out-Host
    & sc.exe config WebGPTCodingToolsMCP obj= "NT AUTHORITY\LocalService" | Out-Host
    & sc.exe config WebGPTCloudflareTunnel obj= "NT AUTHORITY\LocalService" | Out-Host
    & sc.exe config WebGPTCloudflareTunnel depend= WebGPTCodingToolsMCP | Out-Host

    Start-CodingToolsPrivateServices | Out-Null

    Get-Service -Name WebGPTCodingToolsMCP, WebGPTCloudflareTunnel |
        Select-Object Name, Status, StartType |
        Format-Table -AutoSize
    Write-Host "SERVICE_INSTALL_OK"
}
finally {
    Remove-Item -LiteralPath $brokerStageRoot -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $oauthStateBackup) {
        Remove-Item -LiteralPath $oauthStateBackup -Force -ErrorAction SilentlyContinue
    }
    foreach ($suffix in @("-wal", "-shm")) {
        $sidecar = "$oauthStateBackup$suffix"
        if (Test-Path -LiteralPath $sidecar) {
            Remove-Item -LiteralPath $sidecar -Force -ErrorAction SilentlyContinue
        }
    }
    Stop-Transcript
}
