@echo off
cd /d "%~dp0backend" || exit /b 1
where python >nul 2>nul
if %errorlevel%==0 (python -u app.py) else ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -u app.py)
set "launch_result=%errorlevel%"
pause
exit /b %launch_result%
