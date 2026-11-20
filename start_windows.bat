@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "KBO_UNIVERSE.exe" (
  "KBO_UNIVERSE.exe"
  pause
  exit /b
)
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app.py
  pause
  exit /b
)
where python >nul 2>nul
if %errorlevel%==0 (
  python -c "import sys; print(sys.version)" | findstr /C:"3." >nul
  if not errorlevel 1 (
    python app.py
    pause
    exit /b
  )
)
echo Python 3 or KBO_UNIVERSE.exe not found.
echo Please use the EXE build instructions in README.
pause
