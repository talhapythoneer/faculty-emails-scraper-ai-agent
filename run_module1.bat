@echo off
REM ---------------------------------------------------------------------------
REM  Module 1 - official website of every institution (Google search)
REM  Output: output\01_websites.xlsx
REM  Double-click to run, or from a terminal add options, e.g.:
REM      run_module1.bat --state KY --limit 5
REM ---------------------------------------------------------------------------
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
echo === Module 1: official websites ===
echo Output: output\01_websites.xlsx   (keep this file closed in Excel while it runs)
echo.
".venv\Scripts\python.exe" run.py m1 %*
echo.
echo Finished. Open output\01_websites.xlsx, check the "Review" sheet, fix any wrong
echo website in the url_override column, save, then run run_module2.bat
echo.
pause
