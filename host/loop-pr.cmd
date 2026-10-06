@echo off
setlocal
REM ---------------------------------------------------------------
REM  loop-pr -- send the steps that are green so far as a draft
REM  pull request, before every step is green.
REM
REM    loop-pr [--project <mirror-dir>]
REM
REM  Nothing here marks a step green. The draft carries the mirror's
REM  HEAD, which is the commit of the last green step, and its body
REM  lists the steps that are still not green.
REM
REM  It goes from <branch>-draft, not <branch>-pr. Approving the
REM  review in the dashboard rebuilds <branch>-pr by a force push,
REM  and the two must not share a branch.
REM
REM  Run loop-pull first. The steps live in host\dashboard\pullrequest.py.
REM ---------------------------------------------------------------

python "%~dp0dashboard\pullrequest.py" %*
exit /b %errorlevel%
