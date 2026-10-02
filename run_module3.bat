@echo off
REM ---------------------------------------------------------------------------
REM  Module 3 - qualifying majors (Column E) and faculty emails (Column F)
REM  Needs: output\02_programs.xlsx (Module 2)
REM  Output: output\03_final.xlsx  (updated after every institution)
REM  Options from a terminal, e.g.:
REM      run_module3.bat --state KY --limit 10
REM      run_module3.bat --retry-blocked     (only sites that showed a CAPTCHA)
REM      run_module3.bat --export-only       (rebuild the spreadsheet in seconds)
REM ---------------------------------------------------------------------------
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Please run setup.bat first.
    pause
    exit /b 1
)
echo === Module 3: majors and faculty emails ===
echo Output: output\03_final.xlsx   (keep this file closed in Excel while it runs)
echo Keep the PC awake and unlocked: a Chrome window may open, and the mouse may click
echo a "Verify you are human" box by itself for about a second.
echo.
".venv\Scripts\python.exe" run.py m3 %*
echo.
echo Finished. The result is output\03_final.xlsx (see the "Review" sheet for rows to check).
echo.
pause
