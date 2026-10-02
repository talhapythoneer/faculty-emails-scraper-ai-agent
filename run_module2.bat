@echo off
REM ---------------------------------------------------------------------------
REM  Module 2 - undergraduate programs page of every institution (Column D)
REM  Needs: output\01_websites.xlsx (Module 1)
REM  Output: output\02_programs.xlsx
REM  Options from a terminal, e.g.:  run_module2.bat --state KY
REM ---------------------------------------------------------------------------
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
echo === Module 2: undergraduate programs pages ===
echo Output: output\02_programs.xlsx   (keep this file closed in Excel while it runs)
echo.
".venv\Scripts\python.exe" run.py m2 %*
echo.
echo Finished. Open output\02_programs.xlsx, check the "Review" sheet, fix wrong pages in
echo programs_url_override (or write a client_note), save, then run run_module3.bat
echo.
pause
