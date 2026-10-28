@echo off
setlocal enableextensions
title Al-Mahmoud ERP installer
echo ============================================================
echo   Al-Mahmoud ERP - Windows installer and control
echo ============================================================
echo.
echo   Double-click install.bat            install (or repair) the stack
echo   install.bat status                  show whether it is running
echo   install.bat logs                    show the last container logs
echo   install.bat stop                    stop the stack, keep the data
echo   install.bat start                   start it again
echo   install.bat check                   test this machine first
echo.

where pwsh >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "MAHMOUD_PS=pwsh"
) else (
    set "MAHMOUD_PS=powershell"
)

"%MAHMOUD_PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "MAHMOUD_RC=%ERRORLEVEL%"
echo.
if not "%MAHMOUD_RC%"=="0" (
    echo The installer stopped with the error shown above.
    echo Read the log path it printed; nothing was deleted.
) else (
    echo Finished.
)
echo.
pause
exit /b %MAHMOUD_RC%
