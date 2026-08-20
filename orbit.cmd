@echo off
setlocal
set "ORBIT_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ORBIT_ROOT%orbit.ps1" %*
exit /b %ERRORLEVEL%
