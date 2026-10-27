@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1" %*
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" (
  echo.
  echo Startup did not complete. Please review the message above.
)
echo.
echo The backend launcher has stopped. This terminal remains open for troubleshooting.
echo Backend logs are shown above. Close the window manually when finished.
cmd.exe /d /k
exit /b %APP_EXIT_CODE%
