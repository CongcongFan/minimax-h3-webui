@echo off
rem Launch H3 Studio. Start ComfyUI first.
rem Pass extra flags straight through, e.g.  start.bat --lang ja
cd /d "%~dp0"
python app.py %*
if errorlevel 1 pause
