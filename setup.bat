@echo off
REM ---------------------------------------------------------------------------
REM  Faculty Emails Extractor - one-time setup (Windows)
REM  Double-click this file once. It creates a private Python environment in
REM  .venv, installs the required packages, and creates the .env key file.
REM ---------------------------------------------------------------------------
cd /d "%~dp0"
echo.
echo === Faculty Emails Extractor - setup ===
echo.

where python >nul 2>nul
if errorlevel 1 goto :nopython
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto :oldpython

set "CHROME_OK="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME_OK=1"
if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME_OK=1"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME_OK=1"
if not defined CHROME_OK echo WARNING: Google Chrome was not found. Install it from https://www.google.com/chrome/ before running the modules.

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment in .venv ...
    python -m venv .venv
    if errorlevel 1 goto :failed
)
echo Installing packages (this can take a few minutes) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo.
    echo Created the file .env - it opens in Notepad now. Add your API keys, save, and close it.
    start "" notepad ".env"
)

echo.
echo Setup complete.
echo Next: make sure your API keys are in .env, then double-click run_module1.bat
echo.
pause
exit /b 0

:nopython
echo Python is not installed (or not on PATH).
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" in the installer, then run setup.bat again.
pause
exit /b 1

:oldpython
echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ and run setup.bat again.
pause
exit /b 1

:failed
echo.
echo Setup failed - see the messages above. Check your internet connection and run setup.bat again.
pause
exit /b 1
