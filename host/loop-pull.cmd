@echo off
REM Refresh the host-side copies of EVERY repository the sandbox holds.
REM
REM Until this runs, everything the loop has produced lives only inside the
REM distro's VHDX. The runner pushes to /srv/loop/repo.git, but that bare repo
REM is in the same VHDX -- it protects a green step from `reset` and `clean`,
REM not from losing the disk. This is the step that puts it on another one.
REM
REM The host pulls; the sandbox never pushes outward. That is deliberate: the
REM sandbox is where generated code runs, so no credential that reaches
REM outside it may live in there.
REM
REM ONE DIRECTORY PER RUN, because a single mirror cannot hold more than one.
REM Every run creates step-S1..step-S11 -- the same tag names -- so a fetch into
REM a shared clone has to pass --force and overwrite the previous run's tags,
REM and moving `main` as well would leave the old run's commits with no ref
REM pointing at them. Unreachable objects are what `git gc` deletes, and gc runs
REM on its own inside ordinary commands. The backup would stop existing at a
REM moment nobody observed. So:
REM
REM     /srv/loop/repo.runN.git  ->  C:\dev\roop-engin\runs\run-NNN  (immutable)
REM     /srv/loop/repo.git       ->  C:\dev\roop-engin\project        (the live one)
REM
REM which is the shape the sandbox already uses for project.runN. A copy should
REM look like the thing it is a copy of.
REM
REM PROJECTS (loop project init/use) are copied one clone per project:
REM
REM     /srv/loop/projects/<name>/repo.git  ->  C:\dev\roop-engin\projects\<name>
REM
REM That is the clone loop-import.cmd made, so the sandbox's commits land next
REM to the GitHub origin as the remote `loop`. The working branch moves only
REM when it can fast-forward; anything else is left for a person to sort out.
REM Pushing the branch to GitHub is also left to a person.
REM
REM The runner's step-* tags go to refs/loop-tags/, not refs/tags/. They move on
REM every run and mean nothing on GitHub, and `git push --tags` must not carry
REM them there.

setlocal enabledelayedexpansion

set "MIRRORROOT=C:\dev\roop-engin"
set "ARCHIVEROOT=%MIRRORROOT%\runs"
set "PROJECTROOT=%MIRRORROOT%\projects"
set "SSHHOST=loop-runner"

REM The check has to sit INSIDE the `if not exist`. When the directory is
REM already there mkdir never runs, and errorlevel still holds whatever the
REM caller's last command left behind -- which would stop this script for a
REM failure that happened before it started.
if not exist "%ARCHIVEROOT%" (
  mkdir "%ARCHIVEROOT%"
  if errorlevel 1 (
    echo cannot create archive root %ARCHIVEROOT%
    exit /b 1
  )
)

REM One line, space separated, straight from the shell's own glob: no pipes, so
REM nothing here needs batch escaping.
set "REPOS="
for /f "delims=" %%L in ('ssh -o BatchMode^=yes %SSHHOST% "cd /srv/loop && echo repo*.git"') do set "REPOS=%%L"

if not defined REPOS (
  echo cannot reach %SSHHOST%, or /srv/loop holds no repository.
  echo   is the distro up?   loop-dev
  exit /b 1
)

echo repositories on the sandbox: %REPOS%
echo.

REM Projects first: once /srv/loop/repo.git is a link to the current project,
REM the live mirror below is replaced whenever the current project changes, and
REM that is only safe once every project has its own copy.
for /f "delims=" %%P in ('ssh -o BatchMode^=yes %SSHHOST% "cd /srv/loop/projects 2>/dev/null && ls -d */repo.git 2>/dev/null"') do (
  call :sync_project "%%P"
  if errorlevel 1 (
    echo FAILED: stopping before the live mirror is changed.
    exit /b 1
  )
)

REM Archives BEFORE the live one, and the order is load-bearing. `repo.git` is
REM reset to whatever run it now holds, and that is only safe once the run it
REM used to hold has been captured as project.runN. Left to the glob, repo.git
REM would come first ("g" sorts before "r").
for %%R in (%REPOS%) do (
  if /i not "%%R"=="repo.git" (
    call :sync "%%R" ff
    if errorlevel 1 (
      echo FAILED: stopping before the live mirror is changed.
      exit /b 1
    )
  )
)
for %%R in (%REPOS%) do (
  if /i "%%R"=="repo.git" (
    call :sync "%%R" live
    if errorlevel 1 (
      echo FAILED: the live mirror was not synchronized.
      exit /b 1
    )
  )
)

echo.
echo done.
exit /b 0


:sync
setlocal
set "NAME=%~1"
set "MODE=%~2"
if /i "%NAME%"=="repo.git" (
  set "DEST=%MIRRORROOT%\project"
) else (
  REM repo.run4.git -> 4 -> run-004. Delayed expansion is required because
  REM this subroutine is parsed before RUNNO and PAD are assigned.
  set "RUNNO=%NAME:~8,-4%"
  set "PAD=000!RUNNO!"
  set "PAD=!PAD:~-3!"
  set "DEST=%ARCHIVEROOT%\run-!PAD!"
)

if not exist "%DEST%\.git" (
  echo   %NAME%  ^-^>  %DEST%   [clone]
  git clone --quiet "ssh://%SSHHOST%/srv/loop/%NAME%" "%DEST%"
  if errorlevel 1 exit /b 1
  exit /b 0
)

echo   %NAME%  ^-^>  %DEST%   [fetch]
git -C "%DEST%" fetch --prune --prune-tags --tags --force origin
if errorlevel 1 exit /b 1

if /i "%MODE%"=="live" (
  REM Not --ff-only. Each run starts a fresh repo.git with an unrelated root
  REM commit, so the live mirror can never fast-forward; it is replaced. Safe
  REM only because the loop above has already captured the previous run.
  REM
  REM `clean` as well, or files from a run that had them would sit here as
  REM untracked leftovers and a mirror would stop being a faithful copy.
  REM Consequence, and it is the reason this is spelled out: do not keep
  REM anything of your own in project\ -- it is rebuilt, not maintained.
  REM
  REM The bare's HEAD, not main: a project imported from another repository
  REM works on its own branch, and repo.git follows the current project.
  git -C "%DEST%" remote set-head origin --auto >nul
  if errorlevel 1 exit /b 1
  git -C "%DEST%" reset --hard --quiet origin/HEAD
  if errorlevel 1 exit /b 1
  git -C "%DEST%" clean -fdq
  if errorlevel 1 exit /b 1
) else (
  REM An archive never changes, so this can only ever fast-forward -- and if it
  REM somehow does not, something rewrote history that is supposed to be frozen,
  REM which should stop the script rather than be forced past.
  git -C "%DEST%" merge --ff-only --quiet origin/main
  if errorlevel 1 (
    echo      REFUSED: %DEST% diverged from an archive that cannot change.
    exit /b 1
  )
)
git -C "%DEST%" log --oneline -1
exit /b 0


:sync_project
setlocal
REM %1 is "<name>/repo.git", straight from ls.
set "REL=%~1"
set "NAME=%REL:/repo.git=%"
set "DEST=%PROJECTROOT%\%NAME%"
set "URL=ssh://%SSHHOST%/srv/loop/projects/%NAME%/repo.git"

if not exist "%DEST%\.git" (
  echo   projects/%NAME%  ^-^>  %DEST%   [clone]
  git clone --quiet --no-tags --origin loop "%URL%" "%DEST%"
  if errorlevel 1 exit /b 1
) else (
  echo   projects/%NAME%  ^-^>  %DEST%   [fetch]
)

REM A loop-import clone has GitHub as origin and no `loop` yet.
git -C "%DEST%" remote get-url loop >nul 2>&1
if errorlevel 1 (
  git -C "%DEST%" remote add loop "%URL%"
) else (
  git -C "%DEST%" remote set-url loop "%URL%"
)
if errorlevel 1 exit /b 1

git -C "%DEST%" fetch --quiet --prune --no-tags loop "+refs/heads/*:refs/remotes/loop/*" "+refs/tags/*:refs/loop-tags/*"
if errorlevel 1 exit /b 1

REM The branch the sandbox works on is the bare's HEAD.
set "HEADREF="
for /f "tokens=2" %%H in ('git -C "%DEST%" ls-remote --symref loop HEAD ^| findstr /b "ref:"') do set "HEADREF=%%H"
if not defined HEADREF (
  echo      cannot tell which branch the sandbox works on.
  exit /b 1
)
set "BRANCH=%HEADREF:refs/heads/=%"
git -C "%DEST%" rev-parse -q --verify "refs/remotes/loop/%BRANCH%" >nul
if errorlevel 1 (
  echo      %BRANCH% has no commits on the sandbox yet.
  exit /b 0
)

REM The copy is already safe in refs/remotes/loop/. Moving the working branch
REM is a convenience, so a branch that cannot fast-forward is reported, not
REM forced, and does not stop the other projects.
set "CUR="
for /f "delims=" %%C in ('git -C "%DEST%" symbolic-ref --short -q HEAD') do set "CUR=%%C"
if "%CUR%"=="%BRANCH%" (
  git -C "%DEST%" merge --ff-only --quiet "loop/%BRANCH%"
) else (
  git -C "%DEST%" fetch --quiet . "refs/remotes/loop/%BRANCH%:refs/heads/%BRANCH%"
)
if errorlevel 1 (
  echo      NOTE: %BRANCH% was not moved. It has diverged from loop/%BRANCH%,
  echo            or local changes are in the way. The copy is in loop/%BRANCH%.
)
git -C "%DEST%" log --oneline -1 "loop/%BRANCH%"
exit /b 0
