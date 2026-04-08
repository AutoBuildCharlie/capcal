@echo off
title CapCal — AI Video Editor
cd /d "%~dp0"
echo.
echo  ====================================
echo   CapCal — AI Video Editor
echo  ====================================
echo.
echo  Installing dependencies...
pip install -r requirements.txt -q
echo.
echo  Starting server...
echo  Open: http://localhost:5001
echo.
python app.py
pause
