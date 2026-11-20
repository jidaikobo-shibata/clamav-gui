@echo off
setlocal
if not defined MSYS2_ROOT set "MSYS2_ROOT=C:\msys64"
if not exist "%MSYS2_ROOT%\usr\bin\bash.exe" (
  winget install --id MSYS2.MSYS2 --exact --source winget
  if errorlevel 1 goto failed
)
if not exist "%MSYS2_ROOT%\usr\bin\bash.exe" goto failed
"%MSYS2_ROOT%\usr\bin\bash.exe" -lc "pacman -S --needed --noconfirm mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-gtk4"
if errorlevel 1 goto failed
pushd "%~dp0"
"%MSYS2_ROOT%\ucrt64\bin\python.exe" -m clam_gui.platforms.windows_engine
if errorlevel 1 (
  popd
  goto failed
)
popd
call "%~dp0ClamAV-GUI.cmd"
exit /b %errorlevel%
:failed
echo Setup could not finish. Check the messages above and the Windows section in README.md.
pause
exit /b 1
