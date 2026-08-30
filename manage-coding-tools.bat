@echo off
setlocal

fltmc >nul 2>&1
if errorlevel 1 (
  powershell.exe -NoProfile -Command "Start-Process -FilePath '%ComSpec%' -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
  exit /b
)

:menu
cls
echo ==========================================
echo   Coding Tools - Windows Service Manager
echo ==========================================
echo.
sc query WebGPTCodingToolsMCP | findstr /I "STATE"
sc query OpenAITunnelClient | findstr /I "STATE"
sc query WebGPTCloudflareTunnel | findstr /I "STATE"
echo.
echo [1] Start all services
echo [2] Stop all services
echo [3] Restart all services
echo [4] Open Windows Services
echo [5] Show detailed health
echo [6] Prune idle MCP sessions
echo [7] Open local health page
echo [8] Update private MCP code (manual)
echo [9] Roll back private MCP code (manual)
echo [T] Restart OpenAI Tunnel only
echo [P] Manage permissions / YOLO mode
echo [0] Exit
echo.
choice /C 123456789TP0 /N /M "Choose: "

if errorlevel 12 exit /b
if errorlevel 11 (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\manage-mcp-permissions.ps1" -Action Menu
  goto menu
)
if errorlevel 10 (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\manage-web-console-system.ps1" -Action RestartTunnel
  pause
  goto menu
)
if errorlevel 9 (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-coding-tools.ps1" -Rollback
  pause
  goto menu
)
if errorlevel 8 (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-coding-tools.ps1"
  pause
  goto menu
)
if errorlevel 7 (
  start "Coding Tools health" "http://127.0.0.1:8766/"
  goto menu
)
if errorlevel 6 (
  powershell.exe -NoProfile -Command "try { Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8766/prune' | ConvertTo-Json -Depth 8 } catch { Write-Host ('Health endpoint unavailable: ' + $_.Exception.Message) -ForegroundColor Yellow }"
  pause
  goto menu
)
if errorlevel 5 (
  powershell.exe -NoProfile -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:8766/healthz' | ConvertTo-Json -Depth 8 } catch { Write-Host ('Health endpoint unavailable: ' + $_.Exception.Message) -ForegroundColor Yellow }"
  pause
  goto menu
)
if errorlevel 4 (
  start services.msc
  goto menu
)
if errorlevel 3 (
  net stop WebGPTCloudflareTunnel
  net stop OpenAITunnelClient
  net stop WebGPTCodingToolsMCP
  net start WebGPTCodingToolsMCP
  net start OpenAITunnelClient
  net start WebGPTCloudflareTunnel
  pause
  goto menu
)
if errorlevel 2 (
  net stop WebGPTCloudflareTunnel
  net stop OpenAITunnelClient
  net stop WebGPTCodingToolsMCP
  pause
  goto menu
)
if errorlevel 1 (
  net start WebGPTCodingToolsMCP
  net start OpenAITunnelClient
  net start WebGPTCloudflareTunnel
  pause
  goto menu
)
