@echo off
chcp 65001 >nul
title RA2 Map Manager - dev mode
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [X] Python not found. Install Python 3.11+ and add it to PATH.
  echo.
  pause
  exit /b
)

echo  Installing / checking dependencies ...
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo  [X] Dependency installation failed. See the message above.
  pause
  exit /b
)

echo.
echo  ============================================
echo    RA2 Map Manager - starting native window
echo    Keep this console open; closing it quits.
echo  ============================================
echo.
python "%~dp0ra2map.py"
echo.
echo  Program exited.
pause
