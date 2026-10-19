@echo off
REM Run the full test suite BEFORE any major change (per project rule)
set PY=C:\Users\varenya\AppData\Local\Python\pythoncore-3.14-64\python.exe
cd /d "%~dp0"
echo === unit tests ===
"%PY%" test_units.py || goto :fail
echo === region tests ===
"%PY%" test_region.py || goto :fail
echo === worker integration (real engine) ===
"%PY%" test_worker.py || goto :fail
echo === game mocks (full parties) ===
"%PY%" test_game.py || goto :fail
echo.
echo ALL SUITES GREEN
pause
exit /b 0
:fail
echo.
echo SOME TEST FAILED — DO NOT SHIP
pause
exit /b 1
