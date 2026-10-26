@echo off
REM Build a standalone Windows executable with PyInstaller.
REM This is a developer-only step: end users can just use the release artifact.
REM
REM Output: dist\flightwatch.exe  (double-click to start the local web UI)

cd /d "%~dp0.."

python -m pip install --upgrade pyinstaller || goto :error

python -m PyInstaller --noconfirm --clean --onefile --name flightwatch ^
    --add-data "flightwatch/web;flightwatch/web" ^
    --paths . ^
    packaging\flightwatch_launcher.py || goto :error

echo.
echo Built: %CD%\dist\flightwatch.exe
echo Double-click it to start the local web UI at http://127.0.0.1:8765/
goto :eof

:error
echo Build failed.
exit /b 1
