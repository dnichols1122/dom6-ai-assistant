@echo off
rem Double-click this to set up the Dominions 6 assistant on Windows.
rem
rem It exists so nobody has to open PowerShell and type an execution-policy
rem incantation first. Windows blocks downloaded scripts by default; the
rem -ExecutionPolicy Bypass below applies to this one process and changes no
rem system setting.
rem
rem Anything you pass through is handed to the PowerShell script:
rem     install.bat -Minimal      just the assistant, about two minutes
rem     install.bat -Ask          choose each reference library
rem     install.bat -Start        start the server when it finishes

setlocal
cd /d "%~dp0"

where powershell >nul 2>nul
if errorlevel 1 (
    echo.
    echo PowerShell was not found, which is unusual on Windows.
    echo You can install manually instead - see README.md.
    echo.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set EXITCODE=%ERRORLEVEL%

echo.
if not "%EXITCODE%"=="0" (
    echo Setup did not finish. The messages above say why.
) else (
    echo Setup finished.
)

rem Without this the window vanishes on a double-click and takes every error
rem message with it.
pause
exit /b %EXITCODE%
