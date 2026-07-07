@echo off
REM Double-click to run TubeLube in your web browser (no desktop app needed).
REM Starts the local server and opens http://127.0.0.1:5001 automatically.
title TubeLube (web)
echo Starting TubeLube... a browser tab will open in a moment.
echo Keep this window open while you use it - close it to stop the app.
echo.
python app.py
echo.
echo TubeLube has stopped. If it never started, you likely need Python + ffmpeg
echo installed - see README.md ("Fresh setup on a new machine").
pause
