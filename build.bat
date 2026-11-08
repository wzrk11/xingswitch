@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   XingSwitch 打包脚本
echo ============================================
echo.

py -3 -c "import tkinter" 2>nul
if errorlevel 1 (
    echo [错误] 当前 Python 没有 tkinter，无法打包。
    echo        请用 python.org 的官方安装包重装 Python 并勾选 tcl/tk。
    pause
    exit /b 1
)

echo [1/2] 打包主程序 XingSwitch.exe ...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --icon icon.ico --name XingSwitch --version-file version_app.txt ^
    --add-data "ui.html;." xingswitch.py
if errorlevel 1 goto fail

echo.
echo [2/2] 打包安装程序 XingSwitch-Setup.exe ...
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin ^
    --icon icon.ico --name XingSwitch-Setup --version-file version_setup.txt ^
    --add-data "dist/XingSwitch.exe;." --add-data "ui.html;." --add-data "icon.ico;." ^
    installer.py
if errorlevel 1 goto fail

echo.
echo 完成，产物在 dist\
dir /b dist
pause
exit /b 0

:fail
echo.
echo [失败] 打包出错，看上面的日志。
pause
exit /b 1
