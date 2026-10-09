@echo off
REM Windows entry point for the monthly data refresh workflow.
REM Examples:
REM   scripts\refresh_web_data.bat
REM   scripts\refresh_web_data.bat --skip-extract

setlocal
cd /d "%~dp0.."

where py >nul 2>&1
if %errorlevel%==0 (
  py -3 scripts\refresh_web_data.py %*
) else (
  python scripts\refresh_web_data.py %*
)

endlocal
