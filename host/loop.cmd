@echo off
setlocal
REM ---------------------------------------------------------------
REM  loop -- run the sandbox's `loop` command from the host.
REM
REM    loop go <requirements.md> [--language <lang>] [--framework <name>]
REM    loop continue | status | log | stop | update
REM    loop project <list|current|init|use|adopt> ...
REM    loop plan amend <step> <tasks.json>
REM    loop raw <loop.py arguments>
REM
REM  Starts the distro and waits for sshd first, the same way
REM  loop-dev.cmd does. A requirements file that exists on THIS
REM  machine is copied to the maintenance user's home before `go`.
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
if /i "%~1"=="plan" if /i "%~2"=="amend" if exist "%~4" goto amend_local

ssh -t %SSHHOST% loop %*
exit /b %errorlevel%


:amend_local
REM `loop plan amend <step> <tasks.json>` with a plan on THIS machine,
REM e.g. the mirror's plan\tasks.json after editing it.
scp -q "%~4" %SSHHOST%:loop-amended-tasks.json
if errorlevel 1 (
  echo ERROR: could not copy %~4 to the sandbox.
  exit /b 1
)
ssh -t %SSHHOST% loop plan amend %3 ~/loop-amended-tasks.json
exit /b %errorlevel%


:go_local
REM The remote side runs `loop go ~/loop-requirements.md`; the remote
REM shell expands ~ before `loop` changes directory.
scp -q "%~2" %SSHHOST%:loop-requirements.md
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
ssh -t %SSHHOST% loop go ~/loop-requirements.md%REST%
exit /b %errorlevel%
