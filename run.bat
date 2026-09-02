@echo off
cd /d "%~dp0"
py -m iphone_backup_reader
if errorlevel 1 pause
