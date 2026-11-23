@echo off
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem fish scene - stopper.
rem
rem Kills ONLY the two python PIDs recorded in .scene-pids.json for
rem the SAME port pair, and re-checks each command line before
rem killing (PIDs get recycled; a bare Stop-Process on a stale
rem number is exactly how launchers kill unrelated processes).
rem There is no taskkill /IM anywhere in this launcher. A record
rem that was written for another port pair is refused, not reused.
rem
rem Ports: pass --api-port N / --ui-port N (translated to -ApiPort /
rem -UiPort below), or set FISH_SCENE_API_PORT / FISH_SCENE_UI_PORT.
rem Stopping a temporary pair never touches the default one.
rem
rem The stop logic lives in start-scene.ps1 -Stop, so the delivery
rem surface stays three scripts instead of four.
rem ---------------------------------------------------------------
chcp 65001 >nul
title fish scene - stop
setlocal EnableDelayedExpansion
set "PSARGS="
:parse
if "%~1"=="" goto run
if /i "%~1"=="--api-port" (
  if "%~2"=="" goto badport
  set "PSARGS=!PSARGS! -ApiPort %~2"
  shift
  shift
  goto parse
)
if /i "%~1"=="--ui-port" (
  if "%~2"=="" goto badport
  set "PSARGS=!PSARGS! -UiPort %~2"
  shift
  shift
  goto parse
)
set "PSARGS=!PSARGS! %~1"
shift
goto parse

:badport
echo.
echo   [X]    --api-port / --ui-port needs a number right after it.
echo.
pause
endlocal & exit /b 4

:run
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-scene.ps1" -Stop !PSARGS!
set "RC=%ERRORLEVEL%"
if %RC% GEQ 10 pause
endlocal & exit /b %RC%
