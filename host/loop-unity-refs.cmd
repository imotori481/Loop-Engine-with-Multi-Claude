@echo off
REM loop-unity-refs <project> <unity-project-dir>
REM The work is in loop-unity-refs.ps1; this only lets it be typed by
REM name once host is on PATH, like loop and loop-import.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0loop-unity-refs.ps1" %*
exit /b %errorlevel%
