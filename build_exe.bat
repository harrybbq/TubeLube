@echo off
REM Build the standalone TubeLube.exe (Windows).
REM One-time: pip install --user pyinstaller
echo Installing/locating PyInstaller...
python -m pip install --user --quiet pyinstaller
echo Building TubeLube.exe (this takes a minute)...
python -m PyInstaller --noconfirm TubeLube.spec
echo.
echo Done. Your app is at:  dist\TubeLube.exe
pause
