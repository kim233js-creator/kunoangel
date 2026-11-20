@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
) else (
  set "PY=python"
)
%PY% -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'" || goto :failed
%PY% -m pip install --upgrade pyinstaller || goto :failed
%PY% -m PyInstaller --noconfirm --clean --onefile --console --name KBO_UNIVERSE app.py || goto :failed
copy /Y "dist\KBO_UNIVERSE.exe" ".\KBO_UNIVERSE.exe" || goto :failed
echo.
echo SUCCESS: KBO_UNIVERSE.exe created. Double-click it to run.
pause
exit /b 0
:failed
echo.
echo BUILD FAILED. Check Python installation and messages above.
pause
exit /b 1
