@echo off
setlocal
REM ---------------------------------------------------------------
REM  loop -- run the sandbox's `loop` command from the host.
REM
REM    loop go <spec.md> [--language <lang>] [--framework <name>]
REM    loop approve | reject "<comment>" | reopen <module>...
REM    loop continue | status | log | stop | update
REM    loop project <list|current|init|use|adopt> ...
REM    loop refs dxlib <DxLib.h>
REM    loop raw <loop.py arguments>
REM
REM  Starts the distro and waits for sshd first, the same way
REM  loop-dev.cmd does. A spec file that exists on THIS machine is
REM  copied to the maintenance user's home before `go`.
REM  The run itself goes on inside the sandbox after this window
REM  closes; `loop log` picks it up again.
REM ---------------------------------------------------------------

set "DISTRO=Ubuntu-24.04"
set "SSHHOST=loop-dev"
set "PORT=2222"

wsl.exe -d %DISTRO% -u root --exec /usr/bin/true
if errorlevel 1 (
  echo ERROR: failed to start distro %DISTRO%
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "for($i=0;$i -lt 40;$i++){ if(Test-NetConnection -ComputerName 127.0.0.1 -Port %PORT% -InformationLevel Quiet -WarningAction SilentlyContinue){exit 0}; Start-Sleep -Milliseconds 500 }; exit 1"
if errorlevel 1 (
  echo ERROR: sshd did not come up within 20 seconds.
  exit /b 1
)

if /i "%~1"=="go" if exist "%~2" goto go_local
if /i "%~1"=="refs" if /i "%~2"=="dxlib" if exist "%~3" goto dxlib_local

ssh -t %SSHHOST% loop %*
exit /b %errorlevel%


:dxlib_local
REM `loop refs dxlib <DxLib.h>` with the header of the DX Library
REM installed on THIS machine.
scp -q "%~3" %SSHHOST%:loop-DxLib.h
if errorlevel 1 (
  echo ERROR: could not copy %~3 to the sandbox.
  exit /b 1
)
ssh -t %SSHHOST% loop refs dxlib ~/loop-DxLib.h
exit /b %errorlevel%


:go_local
REM The remote side runs `loop go ~/loop-spec.md`; the remote
REM shell expands ~ before `loop` changes directory.
scp -q "%~2" %SSHHOST%:loop-spec.md
if errorlevel 1 (
  echo ERROR: could not copy %~2 to the sandbox.
  exit /b 1
)
shift
shift
set "REST="
:collect
if "%~1"=="" goto run_go
set "REST=%REST% %1"
shift
goto collect

:run_go
ssh -t %SSHHOST% loop go ~/loop-spec.md%REST%
exit /b %errorlevel%
