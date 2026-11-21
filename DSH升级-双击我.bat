@echo off
title DSH 0.2.1 upgrade switch
cd /d "%~dp0"

:menu
cls
echo ============================================================
echo   DSH 0.2.0-rc.2  --^>  0.2.1-rc.5   switch menu
echo ============================================================
echo.
echo   [1] PREPARE  - preflight only, safe while DSH is running
echo   [2] APPLY    - do the switch  (CLOSE DSH FIRST!)
echo   [3] VERIFY   - health check after starting DSH
echo   [4] ROLLBACK - undo everything
echo   [5] open log folder
echo   [0] exit
echo.
set /p choice=Choose [0-5]: 

if "%choice%"=="1" goto prepare
if "%choice%"=="2" goto apply
if "%choice%"=="3" goto verify
if "%choice%"=="4" goto rollback
if "%choice%"=="5" goto logs
if "%choice%"=="0" exit /b 0
goto menu

:prepare
cls
echo Running PREPARE ...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\switch-0.2.1.ps1" -Mode Prepare
echo.
pause
goto menu

:apply
cls
echo ============================================================
echo   WARNING: close the DSH desktop app completely first
echo   (including the tray icon in the bottom-right corner)
echo ============================================================
echo.
set /p ok=DSH fully closed? type Y then Enter (anything else = back): 
if /i not "%ok%"=="Y" goto menu
echo.
echo Running APPLY ...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\switch-0.2.1.ps1" -Mode Apply
echo.
echo If you see APPLY DONE, start the desktop app now, then choose [3].
pause
goto menu

:verify
cls
echo Running VERIFY ...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\switch-0.2.1.ps1" -Mode Verify
echo.
pause
goto menu

:rollback
cls
echo Running ROLLBACK ...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\switch-0.2.1.ps1" -Mode Rollback
echo.
pause
goto menu

:logs
start "" "%TEMP%"
goto menu
