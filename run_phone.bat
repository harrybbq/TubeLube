@echo off
REM Double-click to run TubeLube so you can use it FROM YOUR PHONE.
REM Your phone must be on the same Wi-Fi as this computer. The window below
REM prints the address to open (and a QR code to scan with your camera).
title TubeLube (phone / LAN)
echo Starting TubeLube in LAN mode... a browser tab opens here too.
echo Scan the QR code below with your phone, or type the address shown.
echo Keep this window open while you use it - close it to stop the app.
echo.
echo If Windows asks about the firewall, allow Python on PRIVATE networks.
echo.
python app.py --lan
echo.
echo TubeLube has stopped. If it never started, you likely need Python + ffmpeg
echo installed - see README.md ("Fresh setup on a new machine").
pause
