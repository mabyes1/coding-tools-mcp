@echo off
setlocal

fltmc >nul 2>&1
if errorlevel 1 (
  powershell.exe -NoProfile -Command "Start-Process -FilePath '%ComSpec%' -ArgumentList '/c \"\"%~f0\"\"' -Verb RunAs"
  exit /b
)

echo WebGPT fixed-action elevated broker
echo [1] Install / repair / enable at user logon and start now
echo [2] Start now
echo [3] Stop now
echo [4] Disable logon task
echo [5] Uninstall logon task
echo [6] Status
echo [7] Exit
choice /C 1234567 /N /M "Choose: "
if errorlevel 7 exit /b
if errorlevel 6 goto status
if errorlevel 5 goto uninstall
if errorlevel 4 goto disable
if errorlevel 3 goto stop
if errorlevel 2 goto start
if errorlevel 1 goto install
:status
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\manage-elevated-broker.ps1" -Action Status
goto done
:uninstall
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\manage-elevated-broker.ps1" -Action Uninstall
goto done
:disable
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\manage-elevated-broker.ps1" -Action Disable
goto done
:stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\manage-elevated-broker.ps1" -Action Stop
goto done
:start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\manage-elevated-broker.ps1" -Action Start
goto done
:install
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0service\install-elevated-broker.ps1"
:done
pause
