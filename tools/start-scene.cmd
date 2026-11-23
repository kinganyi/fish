@echo off
cd /d "%~dp0"
rem ---------------------------------------------------------------
rem fish scene - one click launcher. Double-click me, or use the
rem desktop shortcut (its name is Chinese, it points at this file).
rem
rem This file is pure ASCII on purpose: cmd.exe reads .cmd with the
rem OEM code page, so Chinese text here would show up as mojibake.
rem All Chinese prompts live in start-scene.ps1, which is saved as
rem UTF-8 WITH BOM - the only form PowerShell 5.1 decodes correctly.
rem 65001 keeps the console able to render those UTF-8 prompts.
rem
rem Ports: pass --api-port N / --ui-port N (translated to -ApiPort /
rem -UiPort below), or set FISH_SCENE_API_PORT / FISH_SCENE_UI_PORT.
rem ---------------------------------------------------------------
chcp 65001 >nul
title fish scene - start
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
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-scene.ps1" !PSARGS!
set "RC=%ERRORLEVEL%"
rem Exit codes 0/1/2/3/4 are ours and start-scene.ps1 already paused
rem on failure. Anything >= 10 means powershell itself never ran
rem (wrong execution policy, missing exe) - keep the window open.
if %RC% GEQ 10 pause
endlocal & exit /b %RC%
