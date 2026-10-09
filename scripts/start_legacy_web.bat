@echo off
REM Start the preserved original compiled dashboard on Windows.
REM It serves frontend/legacy.html and reads the same local data snapshot.

setlocal
cd /d "%~dp0..\frontend"

REM The original compiled page reads ./data. Populate it from the current Vite
REM snapshot when it has not yet been created by the refresh workflow.
if not exist "data\meta.json" (
  echo ==^> Syncing local data snapshot for the original interface...
  robocopy "public\data" "data" /E /NFL /NDL /NJH /NJS /NP
  if errorlevel 8 (
    echo Failed to sync frontend data snapshot.
    exit /b 1
  )
)

echo ==^> Original interface: http://127.0.0.1:5175/legacy.html
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 -m http.server 5175 --bind 127.0.0.1
) else (
  python -m http.server 5175 --bind 127.0.0.1
)

endlocal
