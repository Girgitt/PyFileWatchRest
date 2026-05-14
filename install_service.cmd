@echo off
setlocal

REM Derive APP_DIR from the directory where this script is located.
REM %~dp0 includes trailing backslash, so remove it.
set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"

set "SERVICE_NAME=PyFileWatchRest"
set "APP_EXE=%APP_DIR%\PyFileWatchRest.exe"
set "CONFIG=%APP_DIR%\config.json"

nssm install "%SERVICE_NAME%" "%APP_EXE%" "--config" "%CONFIG%"
nssm set "%SERVICE_NAME%" AppDirectory "%APP_DIR%"
nssm set "%SERVICE_NAME%" DisplayName "PyFileWatchRest"
nssm set "%SERVICE_NAME%" Description "Two-stage watch directory and POST new or modified files to REST endpoint"
nssm set "%SERVICE_NAME%" Start SERVICE_AUTO_START

REM Restart on crash/exit. Persistent data errors should not loop because bad files stay in processing.
nssm set "%SERVICE_NAME%" AppThrottle 1500
nssm set "%SERVICE_NAME%" AppExit Default Restart
nssm set "%SERVICE_NAME%" AppRestartDelay 5000

REM Optional NSSM stdout/stderr capture.
:: not enabled as python service has own log and nssm log does not purge rotated log files
::nssm set "%SERVICE_NAME%" AppStdout "%APP_DIR%\logs\nssm_stdout.log"
::nssm set "%SERVICE_NAME%" AppStderr "%APP_DIR%\logs\nssm_stderr.log"
::nssm set "%SERVICE_NAME%" AppRotateFiles 1
::nssm set "%SERVICE_NAME%" AppRotateOnline 1
::nssm set "%SERVICE_NAME%" AppRotateBytes 10485760

echo Installed %SERVICE_NAME%.
echo APP_DIR=%APP_DIR%
echo Start with:
echo   nssm start %SERVICE_NAME%

endlocal