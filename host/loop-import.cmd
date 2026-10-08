@echo off
setlocal
REM ---------------------------------------------------------------
REM  loop-import -- put a branch of an existing repository on the
REM  sandbox as a new project.
REM
REM    loop-import <project> <repo-url> <branch> [<base-branch>]
REM                [--src <dir>] [--tests <dir>]
REM
REM  The steps live in host\dashboard\importer.py, which the
REM  dashboard's import tab runs as well:
REM
REM    1. clone <repo-url> into C:\dev\roop-engin\projects\<project>
REM       (or reuse it)
REM    2. switch to <branch>; create it from <base-branch> if it
REM       exists nowhere yet (default: the remote's default branch)
REM    3. loop project init <project> --branch <branch>   (sandbox)
REM    4. push <branch> to the project's bare repo over loop-runner
REM    5. loop project use <project>, and wait for it     (sandbox)
REM
REM  Steps 3 and 5 go through `sudo -n loop dash`, so no password is
REM  asked. Only the host talks to GitHub. The sandbox gets the branch
REM  by a push over loop-runner and never holds a GitHub credential.
REM
REM  This file starts the distro and waits for sshd first, the same
REM  way loop.cmd does.
REM ---------------------------------------------------------------

set "DISTRO=Ubuntu-24.04"
set "PORT=2222"

if "%~3"=="" goto usage

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

python "%~dp0dashboard\importer.py" %*
exit /b %errorlevel%


:usage
echo usage: loop-import ^<project^> ^<repo-url^> ^<branch^> [^<base-branch^>] [--src ^<dir^>] [--tests ^<dir^>]
exit /b 2
