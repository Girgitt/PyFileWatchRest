@echo off
setlocal

REM Edit these paths after copying the cx_Freeze build output to its final directory.
set SERVICE_NAME=FileWatchRestPy
set APP_DIR=C:\opt\FileWatchRestPy
set APP_EXE=C:\opt\FileWatchRestPy\FileWatchRestPy.exe
set CONFIG=C:\opt\FileWatchRestPy\config.json

nssm install %SERVICE_NAME% "%APP_EXE%" "--config" "%CONFIG%"
nssm set %SERVICE_NAME% AppDirectory "%APP_DIR%"
nssm set %SERVICE_NAME% DisplayName "FileWatchRestPy"
nssm set %SERVICE_NAME% Description "Two-stage watch directory and POST new or modified files to REST endpoint"
nssm set %SERVICE_NAME% Start SERVICE_AUTO_START

REM Restart on crash/exit. Persistent data errors should not loop because bad files stay in processing.
nssm set %SERVICE_NAME% AppThrottle 1500
nssm set %SERVICE_NAME% AppExit Default Restart
nssm set %SERVICE_NAME% AppRestartDelay 5000

REM Optional NSSM stdout/stderr capture.
:: not enabled as python service has own log and nssm log does not purga rotated log files
::nssm set %SERVICE_NAME% AppStdout "%APP_DIR%\logs\nssm_stdout.log"
::nssm set %SERVICE_NAME% AppStderr "%APP_DIR%\logs\nssm_stderr.log"
::nssm set %SERVICE_NAME% AppRotateFiles 1
::nssm set %SERVICE_NAME% AppRotateOnline 1
::nssm set %SERVICE_NAME% AppRotateBytes 10485760

echo Installed %SERVICE_NAME%.
echo Start with:
echo   nssm start %SERVICE_NAME%

endlocal

