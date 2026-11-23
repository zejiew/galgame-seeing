@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ============================================================
echo  为 OBS 27 安装 obs-websocket 4.9.1
echo ============================================================
echo.
echo 说明：OBS 28 及以上已经内建 WebSocket，不需要装这个。
echo       OBS 27 必须装插件，本程序才能控制录制。
echo.
echo 本脚本会把插件文件复制到 OBS 安装目录，需要管理员权限。
echo 装完后重启 OBS，在「工具 → WebSocket 服务器设置」里启用并记下密码。
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo [错误] 请右键本文件，选择「以管理员身份运行」。
    pause
    exit /b 1
)

set "OBSD=C:\Program Files\obs-studio"
if not exist "%OBSD%\bin\64bit\obs64.exe" (
    echo [错误] 没找到 OBS：%OBSD%\bin\64bit\obs64.exe
    echo 请修改本脚本里的 OBSD 变量，指向你的 OBS 安装目录。
    pause
    exit /b 1
)

echo OBS 目录：%OBSD%
echo.

rem ---- 查找插件包 ----
set "ZIP="
if exist "%~dp0obs-websocket-4.9.1-Windows.zip" set "ZIP=%~dp0obs-websocket-4.9.1-Windows.zip"
if exist "%~dp0_dl\obs-websocket-4.9.1.zip" set "ZIP=%~dp0_dl\obs-websocket-4.9.1.zip"

if "%ZIP%"=="" (
    echo [错误] 找不到插件包。
    echo 需要的文件：obs-websocket-4.9.1-Windows.zip
    echo 下载地址：
    echo https://github.com/obsproject/obs-websocket/releases/download/4.9.1/obs-websocket-4.9.1-Windows.zip
    echo 下好后放到本脚本同目录，再运行一次。
    pause
    exit /b 1
)
echo 插件包：%ZIP%
echo.

rem ---- 检查 OBS 是否在运行 ----
tasklist /fi "imagename eq obs64.exe" | find /i "obs64.exe" >nul
if not errorlevel 1 (
    echo [提示] OBS 正在运行，请先完全退出 OBS 再安装。
    pause
    exit /b 1
)

echo 正在解压...
set "TMPD=%TEMP%\obs-ws-install"
if exist "%TMPD%" rd /s /q "%TMPD%"
mkdir "%TMPD%"
powershell -NoProfile -Command "Expand-Archive -Path '%ZIP%' -DestinationPath '%TMPD%' -Force"
if errorlevel 1 (
    echo [错误] 解压失败。
    pause
    exit /b 1
)

echo 正在复制文件...
copy /y "%TMPD%\obs-plugins\64bit\obs-websocket.dll" "%OBSD%\obs-plugins\64bit\" >nul
if errorlevel 1 (
    echo [错误] 复制 obs-websocket.dll 失败。
    pause
    exit /b 1
)
if exist "%TMPD%\obs-plugins\32bit\obs-websocket.dll" (
    copy /y "%TMPD%\obs-plugins\32bit\obs-websocket.dll" "%OBSD%\obs-plugins\32bit\" >nul 2>&1
)

if exist "%TMPD%\data\obs-plugins\obs-websocket" (
    xcopy /e /i /y /q "%TMPD%\data\obs-plugins\obs-websocket" "%OBSD%\data\obs-plugins\obs-websocket\" >nul
)

rd /s /q "%TMPD%"

echo.
echo ============================================================
echo  安装完成！
echo ============================================================
echo.
echo 接下来：
echo   1. 启动 OBS
echo   2. 菜单 工具 → WebSocket 服务器设置
echo   3. 勾选「启用 WebSocket 服务器」，端口保持 4455
echo   4. 记下密码（或取消「启用身份验证」）
echo   5. 回到本程序，把密码填到「OBS」设置页，点「连接测试」
echo.
pause
