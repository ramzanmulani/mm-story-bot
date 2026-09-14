@echo off
REM  Push updated mm-story-bot files to GitHub and run a dry run.
setlocal
cd /d "%~dp0"

echo.
echo   MM Story Bot - update and test
echo   ==============================
echo.

set "BASH_EXE="
if exist "%ProgramFiles%\Git\bin\bash.exe"      set "BASH_EXE=%ProgramFiles%\Git\bin\bash.exe"
if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "BASH_EXE=%ProgramFiles(x86)%\Git\bin\bash.exe"
if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "BASH_EXE=%LocalAppData%\Programs\Git\bin\bash.exe"

if defined BASH_EXE (
  "%BASH_EXE%" update.sh
) else (
  echo   Git Bash not found. Install Git for Windows from https://git-scm.com
)

echo.
echo   ------------------------------------------------------------
echo   Press a key to close.
pause >nul
endlocal
