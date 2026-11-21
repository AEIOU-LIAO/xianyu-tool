@echo off
cd /d "%~dp0"
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not %errorlevel%==0 goto start
start "" http://localhost:8000
exit /b 0
:start
cd backend
where python >nul 2>nul
if %errorlevel%==0 (python -u app.py) else ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" -u app.py)
pause
