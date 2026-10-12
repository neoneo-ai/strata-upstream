@echo off
rem NEO-START.bat - start the Neo Strata desktop manager (Windows).
rem It picks a Python, makes sure pywebview is installed, then opens the Neo window.
title Neo Strata
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Neo needs Python 3. Run START-HERE.bat first - it installs Python and Strata.
  pause
  exit /b 1
)

%PY% -c "import importlib.util as u; raise SystemExit(0 if u.find_spec('webview') else 1)" >nul 2>nul
if errorlevel 1 (
  echo Neo: installing the desktop shell packages once ...
  %PY% -m neo.install_neo
)
rem -m neo.manager, not "neo\manager.py": the manager uses package-relative imports.
%PY% -m neo.manager %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Neo stopped - exit code %RC%.
  echo Last lines of neo\state\logs\manager.log:
  if exist "neo\state\logs\manager.log" (
    rem Strip control chars (a run of \r rewinds the console cursor and repaints the
    rem whole screen) and cap every line, so one huge line cannot flood this window.
    powershell -NoProfile -Command "Get-Content -LiteralPath 'neo\state\logs\manager.log' -Tail 15 -Encoding UTF8 | ForEach-Object { $t = ($_ -replace '[\x00-\x1F]', ' '); if ($t.Length -gt 200) { $t.Substring(0,200) + ' ...' } else { $t } }" 2>nul
  )
  echo.
  echo Fix the problem, then run this file again. Press any key to close.
  pause >nul
)
endlocal
