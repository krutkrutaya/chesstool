@echo off
REM Build ChessTool.exe — single file with engine + templates inside
set PY=C:\Users\varenya\AppData\Local\Python\pythoncore-3.14-64\python.exe
cd /d "%~dp0"
"%PY%" -m PyInstaller --onefile --noconsole --name ChessTool ^
  --add-data "engines\stockfish.exe;engines" ^
  --add-data "templates\*.png;templates" ^
  main.py
echo.
echo Done: dist\ChessTool.exe
pause
