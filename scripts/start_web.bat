@echo off
REM Windows entry point for the React/Vite dashboard.
REM Add --prod to build and preview the production bundle.

setlocal
cd /d "%~dp0..\frontend"

if not exist node_modules (
  echo ==^> Installing front-end dependencies...
  call npm install
  if errorlevel 1 exit /b %errorlevel%
)

if "%~1"=="--prod" (
  echo ==^> Production preview: http://127.0.0.1:4174
  call npm run build
  if errorlevel 1 exit /b %errorlevel%
  call npm run preview
) else (
  echo ==^> Development server: http://127.0.0.1:5174
  call npm run dev
)

endlocal
