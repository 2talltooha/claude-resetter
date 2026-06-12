@echo off
REM ---------------------------------------------------------------------------
REM Wrapper that Windows Task Scheduler calls. Keeps the scheduler config
REM trivial: one action, no arguments to fuss with.
REM
REM It cd's to its own folder so queue/ archive/ logs/ resolve correctly no
REM matter what working dir the scheduler hands us.
REM ---------------------------------------------------------------------------

setlocal

REM Folder this .bat lives in (with trailing backslash).
set "HERE=%~dp0"

REM Use the launcher so it works whether python is python/py on this box.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

cd /d "%HERE%"
%PY% "%HERE%warm_dispatch.py"
set "RC=%ERRORLEVEL%"

REM Surface exit code to Task Scheduler "Last Run Result".
endlocal & exit /b %RC%
