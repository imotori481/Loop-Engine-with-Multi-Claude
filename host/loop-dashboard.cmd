@echo off
setlocal
REM Host-only operator console. It reads the pulled project mirror and never
REM places a credential or a listening socket inside the solver sandbox.
REM The live mirror host\loop-pull.cmd writes (MIRRORROOT there).
set "PROJECT=C:\dev\roop-engin\project"
if not exist "%PROJECT%\plan" (
  echo Project mirror not found: %PROJECT%
  echo Run host\loop-pull.cmd first.
  exit /b 1
)
echo Open http://127.0.0.1:8443 after the server starts.
python "%~dp0dashboard\server.py" --project "%PROJECT%"
