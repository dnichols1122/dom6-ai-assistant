@echo off
rem Double-click this to start the Dominions 6 assistant.
rem
rem     start.bat                serve on port 8001 and open a browser
rem     start.bat --port 9000    serve somewhere else
rem     start.bat --no-browser   do not open a browser
rem
rem Close the window or press Ctrl-C to stop. While it runs it watches your
rem save folder, so finishing a turn in Dominions is the whole workflow.

setlocal
cd /d "%~dp0"

set PORT=8001
set OPEN=1

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="--port" (set PORT=%~2& shift & shift & goto parse)
if /i "%~1"=="--no-browser" (set OPEN=0& shift & goto parse)
echo Unknown option: %~1
echo Usage: start.bat [--port NNNN] [--no-browser]
pause
exit /b 2

:parsed
where uv >nul 2>nul
if errorlevel 1 (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "PATH=%USERPROFILE%\.local\bin;%PATH%"
    )
)

where uv >nul 2>nul
if errorlevel 1 (
    echo.
    echo uv is not installed. Double-click install.bat first.
    echo.
    pause
    exit /b 1
)

rem No virtualenv means the install never ran. Saying so beats letting uv build
rem one silently and then fail on a missing reference database.
if not exist ".venv" (
    echo.
    echo This does not look set up yet - there is no .venv folder here.
    echo Double-click install.bat first.
    echo.
    pause
    exit /b 1
)

if not exist "knowledge\reference\reference.sqlite3" (
    echo.
    echo Warning: the game reference database is missing, so the assistant
    echo cannot name units, spells or nations. Run install.bat to build it.
    echo Starting anyway.
    echo.
)

echo.
echo Starting the assistant on http://127.0.0.1:%PORT%/
echo   Close this window or press Ctrl-C to stop.
echo   Turns are ingested automatically while this runs.
echo.

if "%OPEN%"=="1" (
    rem Give the server a moment to bind before the browser asks for the page,
    rem otherwise it lands on a connection error and has to be reloaded.
    start "" /b cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:%PORT%/"
)

uv run uvicorn dom6_assistant.ui.app:app --port %PORT%

echo.
echo The assistant has stopped.
pause
