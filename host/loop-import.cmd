@echo off
setlocal
REM ---------------------------------------------------------------
REM  loop-import -- put a branch of an existing repository on the
REM  sandbox as a new project.
REM
REM    loop-import <project> <repo-url> <branch> [<base-branch>]
REM
REM    1. clone <repo-url> into %WORKROOT%\<project> (or reuse it)
REM    2. switch to <branch>; create it from <base-branch> if it
REM       exists nowhere yet (default: the remote's default branch)
REM    3. loop project init <project> --branch <branch>   (sandbox)
REM    4. push <branch> to the project's bare repo over loop-runner
REM    5. loop project use <project>                       (sandbox)
REM
REM  Only the host talks to GitHub. The sandbox gets the branch by a
REM  push over loop-runner and never holds a GitHub credential.
REM ---------------------------------------------------------------

set "WORKROOT=C:\dev\roop-engin\projects"
set "SSHHOST=loop-runner"

set "NAME=%~1"
set "URL=%~2"
set "BRANCH=%~3"
set "BASE=%~4"
if "%BRANCH%"=="" goto usage
if not "%~5"=="" goto usage

set "DEST=%WORKROOT%\%NAME%"
set "BARE=/srv/loop/projects/%NAME%/repo.git"

if exist "%DEST%\.git" goto reuse
if exist "%DEST%" (
  echo ERROR: %DEST% exists but is not a git clone.
  exit /b 1
)
if not exist "%WORKROOT%" mkdir "%WORKROOT%"
if not exist "%WORKROOT%" (
  echo ERROR: cannot create %WORKROOT%
  exit /b 1
)
echo [1/5] Cloning %URL% into %DEST% ...
git clone --quiet "%URL%" "%DEST%"
if errorlevel 1 (
  echo ERROR: git clone failed.
  exit /b 1
)
goto pick_branch


:reuse
REM A clone of some other repository under the same name would push
REM the wrong history into the project.
set "HAVE="
for /f "delims=" %%U in ('git -C "%DEST%" remote get-url origin 2^>nul') do set "HAVE=%%U"
if /i not "%HAVE%"=="%URL%" (
  echo ERROR: %DEST% is a clone of "%HAVE%", not "%URL%".
  exit /b 1
)
REM Switching branches carries uncommitted changes along or refuses;
REM neither belongs in what the sandbox receives.
set "DIRTY="
for /f "delims=" %%L in ('git -C "%DEST%" status --porcelain') do set "DIRTY=1"
if defined DIRTY (
  echo ERROR: %DEST% has uncommitted changes. Commit or discard them first.
  exit /b 1
)
echo [1/5] Reusing %DEST%, fetching origin ...
git -C "%DEST%" fetch --quiet origin
if errorlevel 1 (
  echo ERROR: git fetch failed.
  exit /b 1
)


:pick_branch
git -C "%DEST%" rev-parse -q --verify "refs/heads/%BRANCH%" >nul
if not errorlevel 1 goto switch_existing
git -C "%DEST%" rev-parse -q --verify "refs/remotes/origin/%BRANCH%" >nul
if not errorlevel 1 goto switch_existing

set "START=origin/HEAD"
if not "%BASE%"=="" set "START=origin/%BASE%"
echo [2/5] Creating branch %BRANCH% from %START% ...
REM --no-track: the new branch must not push to <base-branch>.
git -C "%DEST%" switch --quiet --no-track -c "%BRANCH%" "%START%"
if errorlevel 1 (
  echo ERROR: could not create %BRANCH% from %START%.
  exit /b 1
)
goto init

:switch_existing
echo [2/5] Switching to the existing branch %BRANCH% ...
if not "%BASE%"=="" echo       %BRANCH% already exists, so %BASE% is not used.
git -C "%DEST%" switch --quiet "%BRANCH%"
if errorlevel 1 (
  echo ERROR: could not switch to %BRANCH%.
  exit /b 1
)


:init
echo [3/5] Creating project %NAME% on the sandbox ...
call "%~dp0loop.cmd" project init "%NAME%" --branch "%BRANCH%"
if errorlevel 1 (
  echo ERROR: loop project init failed. Nothing was pushed.
  exit /b 1
)

echo [4/5] Pushing %BRANCH% to %SSHHOST%:%BARE% ...
git -C "%DEST%" push --quiet "%SSHHOST%:%BARE%" "%BRANCH%"
if errorlevel 1 (
  echo ERROR: git push failed. The empty project is left on the sandbox.
  echo        After fixing the cause, run these two:
  echo          git -C "%DEST%" push %SSHHOST%:%BARE% %BRANCH%
  echo          loop project use %NAME%
  exit /b 1
)

echo [5/5] Switching the sandbox to %NAME% ...
call "%~dp0loop.cmd" project use "%NAME%"
if errorlevel 1 (
  echo ERROR: loop project use failed. The branch is already on the sandbox.
  exit /b 1
)

echo.
echo OK. %NAME% is the current project. Host clone: %DEST%
echo Next:  loop go ^<requirements^>
exit /b 0


:usage
echo usage: loop-import ^<project^> ^<repo-url^> ^<branch^> [^<base-branch^>]
exit /b 2
