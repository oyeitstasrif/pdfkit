@echo off
REM One-click start for Windows. Double-click this file.
cd /d "%~dp0"
where python >nul 2>nul || (echo Python is not installed. Get Python 3.12 from python.org and tick "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv\Scripts\python.exe (
  echo Creating virtual environment...
  python -m venv .venv || (pause & exit /b 1)
)
echo Installing/Checking dependencies...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || (echo Install failed - see the message above. & pause & exit /b 1)
echo.
echo Starting PDFKit at http://127.0.0.1:8000   (keep this window open; Ctrl+C to stop)
start "" http://127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
