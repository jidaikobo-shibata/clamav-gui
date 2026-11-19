@echo off
setlocal
if not defined MSYS2_ROOT set "MSYS2_ROOT=C:\msys64"
set "RUNTIME=%MSYS2_ROOT%\ucrt64\bin"
if not exist "%RUNTIME%\pythonw.exe" (
  echo Install the MSYS2 UCRT64 runtime first. See README.md.
  pause
  exit /b 1
)
set "PATH=%RUNTIME%;%PATH%"
cd /d "%~dp0"
start "" "%RUNTIME%\pythonw.exe" -m clam_gui
