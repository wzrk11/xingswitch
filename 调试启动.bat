@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting XingSwitch in console mode...
echo Close this window to stop it.
echo.
py -3 xingswitch.py --console 2>nul || python xingswitch.py --console
echo.
echo ---- exited ----
pause
