@echo off
cd /d "%~dp0"
echo Installing connected-iPhone support...
py -m pip install -e ".[device]"
if errorlevel 1 (
    echo.
    echo Installation failed. Check the message above.
) else (
    echo.
    echo Installation complete. You can now run the app with run.bat.
)
pause
