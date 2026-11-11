@echo off
chcp 65001 >nul
setlocal
REM T06 判据 6 闭合：请在 **完全退出 DSH 桌面端** 后双击本文件。
REM 脚本只在隔离家目录里装/卸/重装 dsh-fish，并对真实 ~/.dsh 做只读 sha256 对比。

set "FISH_HOME=I:\app\DSH-Home\workspace\learn\大肥鱼生命系统\p0"
set "NODE=C:\Users\15596\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"

if not exist "%NODE%" (
  echo [ERROR] 找不到 node：%NODE%
  pause
  exit /b 1
)

echo.
echo ==================================================
echo  T06 判据 6 闭合（停机窗口）
echo  - 请确认 DSH 桌面端已完全退出
echo  - 本脚本不会修改你的真实 DSH 配置
echo ==================================================
echo.

"%NODE%" "%~dp0t06-close-loop.mjs" %*
set "RC=%ERRORLEVEL%"
echo.
echo 退出码：%RC%  （0 = 判据 6 PASS；非 0 = FAIL 或异常）
pause
exit /b %RC%
