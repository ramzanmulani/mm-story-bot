@echo off
REM  MM Story Bot - double-click this file to set the bot up.
REM  It finds Git Bash if you have it, and falls back to PowerShell.
setlocal
cd /d "%~dp0"

echo.
echo   MM Story Bot setup
echo   ==================
echo.

set "BASH_EXE="
if exist "%ProgramFiles%\Git\bin\bash.exe"      set "BASH_EXE=%ProgramFiles%\Git\bin\bash.exe"
if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "BASH_EXE=%ProgramFiles(x86)%\Git\bin\bash.exe"
if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "BASH_EXE=%LocalAppData%\Programs\Git\bin\bash.exe"

if defined BASH_EXE (
  echo   Using Git Bash: %BASH_EXE%
  echo.
  "%BASH_EXE%" setup.sh
) else (
  echo   Git Bash not found - using PowerShell instead.
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\setup.ps1"
)

echo.
echo   ------------------------------------------------------------
echo   Window stays open so you can read the result. Press a key to close.
pause >nul
endlocal
